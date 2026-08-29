"""
validar_midias_ia.py  v3.0

Ferramenta CLI com auditoria de stream em tempo real via ffprobe.exe e IA Local (LM Studio).

NOVIDADES v3.0:
  - Modo --interactive: para cada midia com nova rota detectada, exibe a analise da IA e
    pede confirmacao manual. Permite editar nome/pasta antes de aplicar.
  - Modelo padrao atualizado para qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive
    (modelo com Vision + Reasoning visivel no LM Studio).
  - Bloco duplicado em process_single_file removido (bug critico).
  - inv_map indexado por str(mongo_id) para lookup correto.
  - SEASON_ONLY_RE restrito (S01..S30) evitando falso-positivos com anos.
  - Heuristicas de nome de arquivo como fonte secundaria de identificacao.
  - Frames listados no terminal para auxiliar revisao manual.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.request
import concurrent.futures
import subprocess
import pymongo
from pathlib import Path
from bson import ObjectId

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

DEFAULT_LM_STUDIO_URL = "http://127.0.0.1:1234/v1/chat/completions"
DEFAULT_MODEL_ID = "qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive"
FFMPEG_PATH = r"C:\Users\rapha\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe"
FFPROBE_PATH = r"C:\Users\rapha\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffprobe.exe"

TIMESTAMPS_TO_SAMPLE = [60, 180, 300, 600, 900, 1200]
FRAMES_CACHE_DIR = Path("d:/Users/rapha/Documents/Projetos/nebula/media_audit/extracted_frames")
FRAMES_CACHE_DIR.mkdir(parents=True, exist_ok=True)

EPISODE_RE = re.compile(r"(?i)\bS(\d{1,2})[ ._-]*E(\d{1,3})\b")
SEASON_ONLY_RE = re.compile(r"(?i)\bS(0?[1-9]|[12]\d|30)\b(?![\d])")
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
PREFIX_RE = re.compile(r"(?i)^(?:galaxyrg(?:265)?|galaxytv|rarbg|psa|yts(?:\.[a-z]+)?)\s*-\s*")


def safe_component(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]+', " - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value[:180]


def smart_title(value: str) -> str:
    value = re.sub(r"[._]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -")
    if not value:
        return value
    letters = [ch for ch in value if ch.isalpha()]
    if letters and sum(ch.isupper() for ch in letters) / len(letters) > 0.65:
        return value.title()
    if value == value.lower():
        return value.title()
    return value


def clean_release_prefix(value: str) -> str:
    value = PREFIX_RE.sub("", value.strip())
    value = re.sub(r"(?i)^[^|]*(?:rip|\.com)[^|]*\|\s*", "", value)
    value = re.sub(r"(?i)^encoded by [^-]+-\s*", "", value)
    return value.strip()


PORN_KEYWORDS_RE = re.compile(
    r"(?i)\b(porno|porn|xxx|hentai|adulto|brazzers|bangbros|stepbro|stepsis|stepmom|creampie|pussy|cock|milf|lingerie|masturbating|pervmom|brattysis|sislovesme|princesscum)\b"
)


def is_porn_by_keyword(doc, title_text: str) -> bool:
    if PORN_KEYWORDS_RE.search(doc.get("name", "")) or PORN_KEYWORDS_RE.search(title_text):
        return True
    return False


def sanitize_title_for_ai(raw: str) -> str:
    cleaned = PORN_KEYWORDS_RE.sub("", raw)
    return re.sub(r"\s+", " ", cleaned).strip()


def probe_live_metadata(doc):
    rel_path = f"{doc['parent']}/{doc['name']}".replace("/raphael/", "N:\\").replace("/", "\\")
    if not os.path.exists(rel_path):
        return 0, ""
    cmd = [FFPROBE_PATH, "-v", "error", "-show_format", "-show_streams", "-of", "json", rel_path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        data = json.loads(res.stdout)
        fmt = data.get("format", {})
        fmt_tags = fmt.get("tags", {})
        dur = float(fmt.get("duration", 0))
        v_tags = {}
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                v_tags = s.get("tags", {})
                break
        all_tags = {**fmt_tags, **v_tags}
        raw_titles = [
            str(val).strip()
            for k, val in all_tags.items()
            if k in ("title", "show", "movie_name", "comment") and val
        ]
        return dur, " ".join(raw_titles)
    except Exception:
        return 0, ""


def extract_title_from_filename(filename: str) -> tuple:
    stem = os.path.splitext(filename)[0]
    ep_m = EPISODE_RE.search(stem)
    if ep_m:
        before = stem[: ep_m.start()]
        show = safe_component(smart_title(clean_release_prefix(before)))
        show = re.sub(r"[._ -]+$", "", show).strip()
        if show and len(show) >= 2:
            return ("series", show, int(ep_m.group(1)), int(ep_m.group(2)))
    year_m = YEAR_RE.search(stem)
    if year_m:
        before = stem[: year_m.start()]
        movie = safe_component(smart_title(clean_release_prefix(before)))
        movie = re.sub(r"[._ -]+$", "", movie).strip()
        if movie and len(movie) >= 2:
            return ("movie", movie, int(year_m.group(1)), None)
    return (None, None, None, None)


def encode_image_to_base64(image_path: Path) -> str:
    with open(image_path, "rb") as img_file:
        return base64.b64encode(img_file.read()).decode("utf-8")


def clean_ai_response(content: str) -> str:
    if not content:
        return ""
    content = re.sub(r"(?s)<think>.*?</think>", "", content).strip()
    m = re.search(r"(?im)^\s*((?:Filme|Serie|Movie|Show):\s*.+)$", content)
    if m:
        return m.group(1).strip()
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    for l in reversed(lines):
        if EPISODE_RE.search(l) or YEAR_RE.search(l):
            if not any(k in l.lower() for k in ("thinking", "analyze", "reasoning", "input", "task", "process", "request")):
                return l
    for l in reversed(lines):
        l_low = l.lower()
        if not any(l_low.startswith(p) for p in ("*", "-", "1.", "2.", "3.", "thinking", "analyze", "input", "task", "duration", "process")):
            if not any(k in l_low for k in ("thinking process", "analyze the request", "video file analysis")):
                return l
    return content.strip()


def query_local_ai_vision(ai_url, model_id, title_text, curr_name, duration_min, frame_paths):
    san_name = sanitize_title_for_ai(curr_name)
    san_title = sanitize_title_for_ai(title_text)
    prompt_text = (
        f"Analise este video (observando as imagens extraidas):\n"
        f"- Nome do Arquivo: \"{san_name}\"\n"
        f"- Tag do Stream: \"{san_title}\"\n"
        f"- Duracao: {duration_min:.1f} minutos\n\n"
        f"Identifique o filme ou serie exibido. Responda APENAS com:\n"
        f"Filme: Nome do Filme (Ano)\n"
        f"Serie: Nome da Serie - SXXEYY\n"
    )
    content_list = [{"type": "text", "text": prompt_text}]
    for fpath in frame_paths:
        try:
            b64_img = encode_image_to_base64(fpath)
            content_list.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}
            })
        except Exception:
            pass
    payload = {"model": model_id, "messages": [{"role": "user", "content": content_list}],
                "temperature": 0.1, "max_tokens": 150}
    req = urllib.request.Request(ai_url, data=json.dumps(payload).encode("utf-8"),
                                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            content = msg.get("content", "").strip() or msg.get("reasoning_content", "").strip()
            return clean_ai_response(content)
    except Exception:
        return query_local_ai(ai_url, model_id, title_text, curr_name, "", duration_min)


def query_local_ai(ai_url, model_id, title_text, curr_name, curr_parent, duration_min):
    san_name = sanitize_title_for_ai(curr_name)
    san_title = sanitize_title_for_ai(title_text)
    prompt = (
        f"Analise este video de {duration_min:.1f} minutos e identifique o filme ou serie:\n"
        f"- Tag do Stream: \"{san_title}\"\n"
        f"- Nome do Arquivo: \"{san_name}\"\n\n"
        f"Responda APENAS com:\nFilme: Nome do Filme (Ano)\nSerie: Nome da Serie - SXXEYY\n"
    )
    payload = {"model": model_id, "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1, "max_tokens": 150}
    req = urllib.request.Request(ai_url, data=json.dumps(payload).encode("utf-8"),
                                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            content = msg.get("content", "").strip() or msg.get("reasoning_content", "").strip()
            return clean_ai_response(content)
    except Exception:
        return ""


def _build_series_result(mid, doc, show, season, episode, ext, reason):
    return {
        "mongo_id": mid, "curr_parent": doc["parent"], "curr_name": doc["name"],
        "desired_parent": f"/raphael/Series/{show}/Season {season:02d}",
        "desired_name": f"{show} - S{season:02d}E{episode:02d}{ext}",
        "reason": reason, "recognized": True,
        "recognized_as": f"{show} - S{season:02d}E{episode:02d}", "frames": []
    }


def _build_movie_result(mid, doc, canonical, ext, reason):
    return {
        "mongo_id": mid, "curr_parent": doc["parent"], "curr_name": doc["name"],
        "desired_parent": f"/raphael/Filmes/{canonical}",
        "desired_name": f"{canonical}{ext}",
        "reason": reason, "recognized": True, "recognized_as": canonical, "frames": []
    }


def _parse_ai_as_series(ai_out, ext, mid, doc, ts, dur_min):
    ep_m = EPISODE_RE.search(ai_out)
    if not ep_m:
        return None
    before = re.sub(r"(?i)^(?:Filme|Movie|Serie|Show)[\s:]*", "", ai_out[: ep_m.start()])
    show = safe_component(before.strip(" -_.:(" ))
    if any(k in show.lower() for k in ("thinking", "analyze", "reasoning", "input", "prompt", "task", "process", "request")) or len(show) > 80:
        show = ""
    season, episode = int(ep_m.group(1)), int(ep_m.group(2))
    if show and len(show) >= 2:
        return _build_series_result(mid, doc, show, season, episode, ext,
                                    f"IA aos {ts}s ({ts//60}min): {ai_out[:60]}")
    return None


def _parse_ai_as_movie(ai_out, ext, mid, doc, ts, dur_min):
    if dur_min < 35.0:
        return None
    year_m = YEAR_RE.search(ai_out)
    if not year_m:
        return None
    before = re.sub(r"(?i)^(?:Filme|Movie|Serie|Show)[\s:]*", "", ai_out[: year_m.start()])
    movie = safe_component(before.strip(" -_.:(" ))
    if any(k in movie.lower() for k in ("thinking", "analyze", "reasoning", "input", "prompt", "task", "process", "request")) or len(movie) > 80:
        movie = ""
    year = int(year_m.group(1))
    if movie and len(movie) >= 2:
        canonical = f"{movie} ({year})"
        return _build_movie_result(mid, doc, canonical, ext,
                                   f"IA aos {ts}s ({ts//60}min): {ai_out[:60]}")
    return None


def process_single_file(doc, item, ai_url, model_id, file_index=1):
    mid = str(doc["_id"])
    ext = os.path.splitext(doc["name"])[1] or ".mp4"

    fmt_tags = item.get("format_tags") or {}
    v_tags = item.get("video_tags") or {}
    dur = float(item.get("duration_seconds") or 0)
    all_tags = {**fmt_tags, **v_tags}
    raw_titles = [str(val).strip() for k, val in all_tags.items()
                  if k in ("title", "show", "movie_name", "comment") and val]
    title_text = " ".join(raw_titles)
    inv_mongo_name = item.get("mongo_name", "")

    rel_path = f"{doc['parent']}/{doc['name']}".replace("/raphael/", "N:\\").replace("/", "\\")

    if dur == 0 or not title_text:
        live_dur, live_title = probe_live_metadata(doc)
        if live_dur > 0:
            dur = live_dur
        if live_title:
            title_text = live_title

    dur_min = dur / 60.0

    # Regra A: SxxEyy nas stream tags
    ep_match = EPISODE_RE.search(title_text)
    if ep_match:
        show_raw = re.sub(r"[._ -]+$", "", clean_release_prefix(title_text[: ep_match.start()]))
        show = safe_component(smart_title(show_raw))
        if show and len(show) >= 2:
            season, episode = int(ep_match.group(1)), int(ep_match.group(2))
            return _build_series_result(mid, doc, show, season, episode, ext,
                                        f"Tag de Stream Serie: {title_text[:80]}")

    # Regra B: Season-Only nas tags
    s_only_match = SEASON_ONLY_RE.search(title_text)
    if s_only_match:
        show_raw = re.sub(r"[._ -]+$", "", clean_release_prefix(title_text[: s_only_match.start()]))
        show = safe_component(smart_title(show_raw))
        season = int(s_only_match.group(1))
        if show and len(show) >= 2:
            return _build_series_result(mid, doc, show, season, file_index, ext,
                                        f"Tag de Stream Serie Temporada: {title_text[:80]}")

    # Regra C: Filme (YYYY) nas tags
    year_match = YEAR_RE.search(title_text)
    if year_match and dur_min >= 35.0:
        before = re.sub(r"[.(\s]+$", "", clean_release_prefix(title_text)[: year_match.start()])
        movie = safe_component(smart_title(before))
        if movie and len(movie) >= 2:
            canonical = f"{movie} ({int(year_match.group(1))})"
            return _build_movie_result(mid, doc, canonical, ext,
                                       f"Tag de Stream Filme: {title_text[:80]}")

    # Fonte 2: Heuristicas no nome de arquivo
    for candidate_name in filter(None, [inv_mongo_name, doc["name"]]):
        ftype, fval1, fval2, fval3 = extract_title_from_filename(candidate_name)
        if ftype == "series":
            return _build_series_result(mid, doc, fval1, fval2, fval3, ext,
                                        f"Heuristica de Nome: {candidate_name[:80]}")
        if ftype == "movie" and dur_min >= 35.0:
            canonical = f"{fval1} ({fval2})"
            return _build_movie_result(mid, doc, canonical, ext,
                                       f"Heuristica de Nome: {candidate_name[:80]}")

    # Fonte 3: IA Visual com frames progressivos
    frames_accumulated = []
    context_for_ai = title_text or inv_mongo_name or doc["name"]

    for ts in TIMESTAMPS_TO_SAMPLE:
        if dur > 0 and ts >= dur - 30:
            break
        out_img = FRAMES_CACHE_DIR / f"{mid}_t{ts}s.jpg"
        if not (out_img.exists() and out_img.stat().st_size > 5000):
            if os.path.exists(rel_path):
                cmd = [FFMPEG_PATH, "-ss", str(ts), "-i", rel_path, "-vframes", "1",
                       "-q:v", "2", "-y", str(out_img)]
                try:
                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                except Exception:
                    pass
        if out_img.exists() and out_img.stat().st_size > 5000:
            frames_accumulated.append(out_img)

        ai_out = query_local_ai_vision(ai_url, model_id, context_for_ai, doc["name"], dur_min, frames_accumulated)
        if ai_out:
            result = _parse_ai_as_series(ai_out, ext, mid, doc, ts, dur_min)
            if result:
                result["frames"] = list(frames_accumulated)
                return result
            result = _parse_ai_as_movie(ai_out, ext, mid, doc, ts, dur_min)
            if result:
                result["frames"] = list(frames_accumulated)
                return result

    # Fallback: palavras-chave
    if is_porn_by_keyword(doc, title_text):
        return {"mongo_id": mid, "curr_parent": doc["parent"], "curr_name": doc["name"],
                "desired_parent": "/raphael/Porno", "desired_name": doc["name"],
                "reason": "Midia Adulta por Palavra-Chave", "recognized": True,
                "recognized_as": "Video Porno", "frames": []}

    if doc.get("parent", "").startswith("/raphael/Porno"):
        return {"mongo_id": mid, "curr_parent": doc["parent"], "curr_name": doc["name"],
                "desired_parent": "/raphael/Porno", "desired_name": doc["name"],
                "reason": "Execucao sem ID -> /raphael/Porno", "recognized": True,
                "recognized_as": "Video Porno", "frames": []}

    return {"mongo_id": mid, "curr_parent": doc["parent"], "curr_name": doc["name"],
            "desired_parent": doc["parent"], "desired_name": doc["name"],
            "reason": "Nao reconhecido (permanece no local atual)", "recognized": False,
            "recognized_as": None, "frames": []}


def interactive_review(res: dict, idx: int, total: int):
    """
    Exibe a analise da IA e pede confirmacao manual.
    Retorna dict confirmado/editado ou None se pulado.
    """
    SEP = "=" * 65
    curr_path = f"{res['curr_parent']}/{res['curr_name']}"
    desired_path = f"{res['desired_parent']}/{res['desired_name']}"

    print(f"\n{SEP}")
    print(f"  REVISAO MANUAL [{idx}/{total}]")
    print(SEP)
    print(f"  Arquivo Atual  : {curr_path}")
    print(f"  Reconhecida IA : {res.get('recognized_as') or '[Nao reconhecida]'}")
    print(f"  Motivo         : {res['reason']}")
    print(f"  Destino Proposto: {desired_path}")

    frames = res.get("frames") or []
    if frames:
        print(f"\n  Frames disponiveis para visualizacao:")
        for i, f in enumerate(frames, 1):
            print(f"    [{i}] {f}")

    print(f"\n  Opcoes:")
    print(f"    [Enter/y] Confirmar destino da IA")
    print(f"    [n]       Pular esta midia")
    print(f"    [e]       Editar destino manualmente")
    print(f"    [p]       Mover para /raphael/Porno")
    print(f"    [s]       Manter no local atual")

    try:
        choice = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Interrompido pelo usuario.")
        sys.exit(0)

    if choice in ("", "y"):
        return res

    if choice == "n":
        print("  [PULADO]")
        return None

    if choice == "p":
        res["desired_parent"] = "/raphael/Porno"
        res["desired_name"] = res["curr_name"]
        res["reason"] += " [Corrigido manualmente -> Porno]"
        print(f"  [OK] -> /raphael/Porno/{res['desired_name']}")
        return res

    if choice == "s":
        print("  [MANTIDO] Permanece no local atual.")
        return None

    if choice == "e":
        print(f"  Destino atual: {desired_path}")
        print(f"  Exemplos:")
        print(f"    Serie  -> parent: /raphael/Series/Breaking Bad/Season 01")
        print(f"               nome : Breaking Bad - S01E01.mkv")
        print(f"    Filme  -> parent: /raphael/Filmes/Matrix (1999)")
        print(f"               nome : Matrix (1999).mkv")
        try:
            new_parent = input("  Novo parent: ").strip()
            new_name   = input("  Novo nome  : ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Interrompido.")
            sys.exit(0)
        if new_parent and new_name:
            res["desired_parent"] = new_parent
            res["desired_name"] = new_name
            res["reason"] += " [Editado manualmente]"
            print(f"  [OK] Destino: {new_parent}/{new_name}")
            return res
        print("  [PULADO] Entrada vazia.")
        return None

    # Qualquer outra tecla = confirma
    return res


def ensure_parent_dirs(files_col, parent_path, now):
    user_root = "/raphael"
    if not parent_path.startswith(user_root):
        return
    rel = [p for p in parent_path[len(user_root):].strip("/").split("/") if p]
    curr = user_root
    for p in rel:
        files_col.update_one(
            {"type": "dir", "name": p, "parent": curr},
            {"$setOnInsert": {"type": "dir", "name": p, "parent": curr, "ctime": now, "mtime": now, "size": 0}},
            upsert=True
        )
        curr = f"{curr}/{p}"


def apply_realignments(files_col, realignments):
    now = int(time.time())
    applied = 0
    quarantined = 0
    for r in realignments:
        ensure_parent_dirs(files_col, r["desired_parent"], now)
        try:
            files_col.update_one(
                {"_id": ObjectId(r["mongo_id"])},
                {"$set": {"parent": r["desired_parent"], "name": r["desired_name"], "mtime": now}}
            )
            applied += 1
        except pymongo.errors.DuplicateKeyError:
            quar_parent = f"/raphael/Auditoria/Duplicatas/{r['desired_parent'].replace('/raphael/', '')}"
            stem = os.path.splitext(r["desired_name"])[0]
            ext_q = os.path.splitext(r["desired_name"])[1]
            quar_name = f"{stem}__{r['mongo_id'][-8:]}{ext_q}"
            ensure_parent_dirs(files_col, quar_parent, now)
            try:
                files_col.update_one(
                    {"_id": ObjectId(r["mongo_id"])},
                    {"$set": {"parent": quar_parent, "name": quar_name, "mtime": now}}
                )
                quarantined += 1
            except pymongo.errors.DuplicateKeyError:
                pass

    # Limpar dirs vazios
    categories_to_keep = {"Filmes", "Series", "Porno", "Auditoria"}
    user_root = "/raphael"
    deleted_empty = 0
    while True:
        dirs = list(files_col.find({"type": "dir"}))
        cleaned = 0
        for d in dirs:
            if d.get("name") in categories_to_keep and d.get("parent") == user_root:
                continue
            dir_path = f"{d.get('parent','')}/{d.get('name','')}"
            if files_col.count_documents({"parent": dir_path}) == 0:
                files_col.delete_one({"_id": d["_id"]})
                cleaned += 1
                deleted_empty += 1
        if cleaned == 0:
            break
    return applied, quarantined, deleted_empty


def main():
    parser = argparse.ArgumentParser(description="Validador de Midias com IA Local + Revisao Manual")
    parser.add_argument("--pasta", required=True,
                        help="Pasta alvo (ex: Filmes, Series, /raphael/Filmes/Batman)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulacao sem alterar o banco")
    parser.add_argument("--interactive", action="store_true",
                        help="Pede confirmacao manual para cada nova rota detectada")
    parser.add_argument("--ai-url", default=DEFAULT_LM_STUDIO_URL)
    parser.add_argument("--ai-model", default=DEFAULT_MODEL_ID)
    parser.add_argument("--workers", type=int, default=4,
                        help="Threads paralelas (automaticamente 1 em modo --interactive)")
    args = parser.parse_args()

    if args.interactive and args.workers > 1:
        print("[INFO] Modo --interactive: forcando --workers=1 para saida ordenada.")
        args.workers = 1

    apply = not args.dry_run
    target = args.pasta.strip("/")
    if not target.startswith("raphael"):
        target = f"raphael/{target}"
    pattern = re.compile(rf"^/{target}", re.IGNORECASE)

    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    files_col = db.files

    docs = list(files_col.find({"type": "file", "parent": pattern}))

    modo_str = "SIMULACAO (DRY-RUN)" if args.dry_run else "APLICACAO AUTOMATICA"
    if args.interactive:
        modo_str += " + REVISAO MANUAL INTERATIVA"

    print(f"\n{'='*55}")
    print(f"  VALIDACAO DE MIDIAS VIA IA LOCAL (LM Studio)")
    print(f"{'='*55}")
    print(f"Pasta Alvo     : /{target}")
    print(f"Arquivos       : {len(docs)}")
    print(f"Modo           : {modo_str}")
    print(f"Modelo IA      : {args.ai_model}")
    print(f"{'='*55}\n")

    if not docs:
        print("Nenhum arquivo encontrado.")
        return

    inventory_path = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/http_probe/ffprobe_inventory.json"
    items = []
    if os.path.exists(inventory_path):
        with open(inventory_path, "r", encoding="utf-8") as f:
            items = json.load(f)
    inv_map = {str(x["mongo_id"]): x for x in items if x.get("mongo_id")}
    print(f"Inventario ffprobe: {len(inv_map)} entradas\n")

    all_results = []
    print("Consultando IA Local para cada midia...\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                process_single_file,
                doc,
                inv_map.get(str(doc["_id"]), {}),
                args.ai_url,
                args.ai_model,
                idx
            ): (idx, doc)
            for idx, doc in enumerate(docs, 1)
        }
        for future in concurrent.futures.as_completed(futures):
            idx, doc = futures[future]
            try:
                res = future.result()
            except Exception as exc:
                print(f"[ERRO] [{idx}] {doc['name']}: {exc}", flush=True)
                continue
            all_results.append((idx, res))

    all_results.sort(key=lambda x: x[0])

    realignments = []
    total_divergent = sum(
        1 for _, r in all_results
        if r["curr_parent"] != r["desired_parent"] or r["curr_name"] != r["desired_name"]
    )
    reviewed_count = 0

    for idx, res in all_results:
        curr_path = f"{res['curr_parent']}/{res['curr_name']}"
        desired_path = f"{res['desired_parent']}/{res['desired_name']}"
        is_divergent = (res["curr_parent"] != res["desired_parent"] or
                        res["curr_name"] != res["desired_name"])

        status_str = (
            "[NOVA ROTA DETECTADA]" if is_divergent
            else "[OK - Ja no local correto]" if res.get("recognized")
            else "[MANTIDO - Nao reconhecido]"
        )
        recognized_str = res.get("recognized_as") or "[Nao reconhecida]"
        frames = res.get("frames") or []

        print(f"[{idx}/{len(docs)}] {curr_path}", flush=True)
        print(f" |- Reconhecida: {recognized_str} ({res['reason']})", flush=True)
        print(f" |- Destino    : {desired_path} {status_str}", flush=True)
        if frames:
            print(f" |- Frames     : {', '.join(str(f.name) for f in frames[:2])}", flush=True)
        print("", flush=True)

        if is_divergent:
            reviewed_count += 1
            if args.interactive:
                confirmed = interactive_review(res, reviewed_count, total_divergent)
                if confirmed is not None:
                    still_divergent = (confirmed["curr_parent"] != confirmed["desired_parent"] or
                                       confirmed["curr_name"] != confirmed["desired_name"])
                    if still_divergent:
                        realignments.append(confirmed)
            else:
                realignments.append(res)

    print(f"\nTotal divergentes encontrados : {total_divergent}")
    print(f"Total confirmados para aplicar: {len(realignments)}")

    if not apply:
        print("\n[SIMULACAO] Nenhuma alteracao gravada. Execute sem --dry-run para aplicar.")
        return

    if not realignments:
        print("\nNenhuma correcao necessaria ou nenhuma confirmada!")
        return

    print("\nAplicando correcoes no MongoDB...")
    applied, quarantined, deleted_empty = apply_realignments(files_col, realignments)

    print("\nRemontando unidade N: via rclone...")
    try:
        subprocess.run(
            ["powershell", "-Command",
             "Stop-Process -Name rclone -Force -ErrorAction SilentlyContinue; "
             "Remove-Item -Path \"$env:LOCALAPPDATA\\rclone\\vfs\\*\" -Recurse -Force -ErrorAction SilentlyContinue; "
             "Remove-Item -Path \"$env:LOCALAPPDATA\\rclone\\vfsMeta\\*\" -Recurse -Force -ErrorAction SilentlyContinue"],
            check=False
        )
        rclone_script = "d:/Users/rapha/Documents/Projetos/nebula/NebulaFTP-master/tools/start_rclone_z.ps1"
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", rclone_script])
        print("Unidade N: atualizada!")
    except Exception as e:
        print(f"Aviso ao remontar N:: {e}")

    print(f"\n{'='*55}")
    print(f"  SUCESSO: {applied} midias re-alinhadas!")
    print(f"  Quarentena  : {quarantined} duplicatas.")
    print(f"  Dirs vazios : {deleted_empty} removidos.")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
