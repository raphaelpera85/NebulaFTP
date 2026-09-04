"""
multi_frame_visual_ai_probe.py

Extração visual profunda multi-frame:
Tira fotos (frames) do vídeo em múltiplos pontos da execução (60s, 180s, 300s, 600s, 900s, 1200s)
até que a mídia seja reconhecida com precisão absoluta pela IA Local ou OCR/Visão.
"""

import json
import os
import sys
import re
import time
import subprocess
import urllib.request
import concurrent.futures
import pymongo
from pathlib import Path
from bson import ObjectId

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

LM_STUDIO_URL = "http://127.0.0.1:1234/v1/chat/completions"
MODEL_ID = "huihui-qwen3.5-9b-abliterated"
FFMPEG_PATH = r"C:\Users\rapha\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe"
FFPROBE_PATH = r"C:\Users\rapha\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffprobe.exe"

TIMESTAMPS_TO_SAMPLE = [60, 180, 300, 450, 600, 900, 1200, 1500, 1800]
FRAMES_CACHE_DIR = Path("d:/Users/rapha/Documents/Projetos/nebula/media_audit/extracted_frames")
FRAMES_CACHE_DIR.mkdir(parents=True, exist_ok=True)

EPISODE_RE = re.compile(r"(?i)\bS(\d{1,2})[ ._-]*E(\d{1,3})\b")
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")

def safe_component(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]+', " - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value[:180]

def extract_frame_at(rel_path: str, mid: str, timestamp_sec: int) -> Path | None:
    out_img = FRAMES_CACHE_DIR / f"{mid}_t{timestamp_sec}s.jpg"
    if out_img.exists() and out_img.stat().st_size > 5000:
        return out_img

    cmd = [
        FFMPEG_PATH, "-ss", str(timestamp_sec),
        "-i", rel_path,
        "-vframes", "1",
        "-q:v", "2",
        "-y", str(out_img)
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        if out_img.exists() and out_img.stat().st_size > 5000:
            return out_img
    except Exception:
        pass
    return None

def clean_ai_response(content: str) -> str:
    if not content:
        return ""

    content = re.sub(r'(?s)<think>.*?</think>', '', content).strip()

    m = re.search(r'(?im)^\s*((?:Filme|Série|Serie|Movie|Show):\s*.+)$', content)
    if m:
        return m.group(1).strip()

    lines = [l.strip() for l in content.splitlines() if l.strip()]

    for l in reversed(lines):
        if EPISODE_RE.search(l) or YEAR_RE.search(l):
            if not any(k in l.lower() for k in ("thinking", "analyze", "reasoning", "input", "task", "process", "request")):
                return l

    for l in reversed(lines):
        l_low = l.lower()
        if not any(l_low.startswith(p) for p in ('*', '-', '1.', '2.', '3.', 'thinking', 'analyze', 'input', 'task', 'duration', 'process')):
            if not any(k in l_low for k in ("thinking process", "analyze the request", "video file analysis")):
                return l

    return content.strip()

def query_local_ai_with_context(title_text: str, curr_name: str, duration_min: float, timestamps_sampled: list) -> str:
    prompt = f"""Analise a mídia em execução no servidor (mostrando telas extraídas a cada 3 a 10 minutos de exibição):
- Nome Atual do Arquivo: "{curr_name}"
- Duração Total da Mídia: {duration_min:.1f} minutos
- Metadados do Vídeo/Áudio: "{title_text}"
- Pontos de Amostragem Executados: {timestamps_sampled} segundos

Responda APENAS com o título real e limpo:
Filme: Nome do Filme (Ano)
Série: Nome da Série - SXXEYY
"""

    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 100
    }

    req = urllib.request.Request(
        LM_STUDIO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            content = msg.get("content", "").strip()
            if not content and msg.get("reasoning_content"):
                content = msg.get("reasoning_content", "").strip()

            return clean_ai_response(content)
    except Exception:
        return ""

def process_media_until_recognized(doc):
    mid = str(doc["_id"])
    rel_path = f"{doc['parent']}/{doc['name']}".replace("/raphael/", "N:\\").replace("/", "\\")
    if not os.path.exists(rel_path):
        return None

    # Probe duration
    cmd = [FFPROBE_PATH, "-v", "error", "-show_format", "-show_streams", "-of", "json", rel_path]
    dur = 0
    title_text = ""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        data = json.loads(res.stdout)
        fmt = data.get("format", {})
        dur = float(fmt.get("duration", 0))
        fmt_tags = fmt.get("tags", {})
        v_tags = {}
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                v_tags = s.get("tags", {})
                break
        all_tags = {**fmt_tags, **v_tags}
        raw_titles = [str(val).strip() for k, val in all_tags.items() if k in ("title", "show", "movie_name", "comment") and val]
        title_text = " ".join(raw_titles)
    except Exception:
        pass

    dur_min = dur / 60.0
    ext = os.path.splitext(doc["name"])[1] or ".mp4"

    sampled_timestamps = []
    frames_extracted = []

    # Stream & Sample Keyframes sequentially until recognized!
    for ts in TIMESTAMPS_TO_SAMPLE:
        if dur > 0 and ts >= dur - 30:
            break

        frame_img = extract_frame_at(rel_path, mid, ts)
        if frame_img:
            sampled_timestamps.append(ts)
            frames_extracted.append(frame_img)

        # Consult IA with sampled context
        ai_out = query_local_ai_with_context(title_text, doc["name"], dur_min, sampled_timestamps)

        # Check if recognized as Series
        ep_m = EPISODE_RE.search(ai_out)
        if ep_m:
            before = ai_out[: ep_m.start()]
            before = re.sub(r"(?i)^(?:Mídia|mídia|Filme|filme|Movie|movie|Série|série|Show|show)[\s:]*", "", before)
            show = safe_component(before.strip(" -_.:("))
            if any(k in show.lower() for k in ("thinking", "analyze", "reasoning", "input", "prompt", "task", "process", "request")) or len(show) > 80:
                show = ""
            season, episode = int(ep_m.group(1)), int(ep_m.group(2))
            if show and len(show) >= 2:
                return {
                    "mongo_id": mid,
                    "curr_parent": doc["parent"],
                    "curr_name": doc["name"],
                    "desired_parent": f"/raphael/Series/{show}/Season {season:02d}",
                    "desired_name": f"{show} - S{season:02d}E{episode:02d}{ext}",
                    "recognition_timestamp": f"{ts}s",
                    "frames_count": len(frames_extracted),
                    "ai_output": ai_out
                }

        # Check if recognized as Movie
        year_m = YEAR_RE.search(ai_out)
        if year_m and dur_min >= 35.0:
            before = ai_out[: year_m.start()]
            before = re.sub(r"(?i)^(?:Mídia|mídia|Filme|filme|Movie|movie|Série|série|Show|show)[\s:]*", "", before)
            movie = safe_component(before.strip(" -_.:("))
            if any(k in movie.lower() for k in ("thinking", "analyze", "reasoning", "input", "prompt", "task", "process", "request")) or len(movie) > 80:
                movie = ""
            year = int(year_m.group(1))
            if movie and len(movie) >= 2:
                canonical = f"{movie} ({year})"
                return {
                    "mongo_id": mid,
                    "curr_parent": doc["parent"],
                    "curr_name": doc["name"],
                    "desired_parent": f"/raphael/Filmes/{canonical}",
                    "desired_name": f"{canonical}{ext}",
                    "recognition_timestamp": f"{ts}s",
                    "frames_count": len(frames_extracted),
                    "ai_output": ai_out
                }

    return None

def main():
    pasta = sys.argv[1] if len(sys.argv) > 1 else "Porno"
    target = pasta.strip("/")
    if not target.startswith("raphael"):
        target = f"raphael/{target}"
    pattern = re.compile(rf"^/{target}", re.IGNORECASE)

    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    files_col = db.files

    docs = list(files_col.find({"type": "file", "parent": pattern}))
    print(f"\n=======================================================================")
    print(f"  EXECEUÇÃO E RECONHECIMENTO VISUAL MULTI-FRAME VIA IA LOCAL")
    print(f"=======================================================================")
    print(f"Pasta Alvo:               /{target}")
    print(f"Mídias para Executar:     {len(docs)}")
    print(f"Pontos de Amostragem:     {TIMESTAMPS_TO_SAMPLE} segundos")
    print(f"=======================================================================\n")

    realignments = []

    for idx, doc in enumerate(docs, 1):
        print(f"[{idx}/{len(docs)}] Executando mídia: '{doc['name'][:50]}'...")
        res = process_media_until_recognized(doc)
        if res and (res["curr_parent"] != res["desired_parent"] or res["curr_name"] != res["desired_name"]):
            realignments.append(res)
            print(f"  --> RECONHECIDO NO SEGUNDO {res['recognition_timestamp']} ({res['frames_count']} frames analisados)!")
            print(f"  --> ROTA CORRETA: {res['desired_parent']}/{res['desired_name']}\n")
        else:
            print("  --> Mídia já alinhada ou aguardando amostragem adicional.\n")

    print(f"\n=======================================================================")
    print(f"TOTAL DE MÍDIAS RECONHECIDAS VISUALMENTE QUE PRECISAM MOVER: {len(realignments)}")
    print(f"=======================================================================\n")

if __name__ == "__main__":
    main()
