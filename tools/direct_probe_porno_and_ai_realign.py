import json
import os
import sys
import re
import unicodedata
import time
import subprocess
import urllib.request
import concurrent.futures
import pymongo
from pathlib import Path
from bson import ObjectId

sys.stdout.reconfigure(encoding='utf-8')

LM_STUDIO_URL = "http://127.0.0.1:1234/v1/chat/completions"
MODEL_ID = "huihui-qwen3.5-9b-abliterated"
FFPROBE_PATH = r"C:\Users\rapha\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffprobe.exe"

EPISODE_RE = re.compile(r"(?i)\bS(\d{1,2})[ ._-]*E(\d{1,3})\b")
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
PREFIX_RE = re.compile(r"(?i)^(?:galaxyrg(?:265)?|galaxytv|rarbg|psa|yts(?:\.[a-z]+)?)\s*-\s*")

def safe_component(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]+', " - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value[:180]

def smart_title(value: str) -> str:
    value = re.sub(r"[._]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -")
    if not value: return value
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

def query_local_ai(probe_title: str, curr_name: str, duration_min: float) -> str:
    prompt = f"""Analise este arquivo de vídeo de {duration_min:.1f} minutos e identifique o nome do filme ou série em português:
- Tag do Stream: "{probe_title}"
- Nome Atual: "{curr_name}"

Responda APENAS com uma linha:
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
            return msg.get("content", "").strip() or msg.get("reasoning_content", "").strip()
    except Exception:
        return ""

def probe_and_realign_doc(doc):
    mid = str(doc["_id"])
    rel_path = f"{doc['parent']}/{doc['name']}".replace("/raphael/", "N:\\").replace("/", "\\")
    
    cmd = [FFPROBE_PATH, "-v", "error", "-show_format", "-show_streams", "-of", "json", rel_path]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(res.stdout)
        fmt = data.get("format", {})
        fmt_tags = fmt.get("tags", {})
        dur = float(fmt.get("duration", 0))
        dur_min = dur / 60.0

        v_tags = {}
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                v_tags = s.get("tags", {})
                break

        all_tags = {**fmt_tags, **v_tags}
        raw_titles = [str(val).strip() for k, val in all_tags.items() if k in ("title", "show", "movie_name", "comment") and val]
        title_text = " ".join(raw_titles)
        ext = os.path.splitext(doc["name"])[1] or ".mp4"

        # Check 1: SxxEyy
        ep_match = EPISODE_RE.search(title_text)
        if ep_match:
            show_raw = clean_release_prefix(title_text[: ep_match.start()])
            show_raw = re.sub(r"[._ -]+$", "", show_raw)
            show = safe_component(smart_title(show_raw))
            if show and len(show) >= 2:
                season, episode = int(ep_match.group(1)), int(ep_match.group(2))
                return {
                    "mongo_id": mid,
                    "curr_parent": doc["parent"],
                    "curr_name": doc["name"],
                    "desired_parent": f"/raphael/Series/{show}/Season {season:02d}",
                    "desired_name": f"{show} - S{season:02d}E{episode:02d}{ext}",
                    "reason": f"Stream Tag Series: {title_text}"
                }

        # Check 2: Movie (YYYY) and duration >= 35 min
        year_match = YEAR_RE.search(title_text)
        if year_match and dur_min >= 35.0:
            cleaned = clean_release_prefix(title_text)
            before = cleaned[: year_match.start()]
            before = re.sub(r"[.(\s]+$", "", before)
            movie = safe_component(smart_title(before))
            if movie and len(movie) >= 2:
                year = int(year_match.group(1))
                canonical = f"{movie} ({year})"
                return {
                    "mongo_id": mid,
                    "curr_parent": doc["parent"],
                    "curr_name": doc["name"],
                    "desired_parent": f"/raphael/Filmes/{canonical}",
                    "desired_name": f"{canonical}{ext}",
                    "reason": f"Stream Tag Movie: {title_text}"
                }

        # Check 3: Query LM Studio AI if duration >= 35 min
        if dur_min >= 35.0:
            ai_out = query_local_ai(title_text, doc["name"], dur_min)
            year_m = YEAR_RE.search(ai_out)
            if year_m:
                lines = [l for l in ai_out.splitlines() if "(" in l or ")" in l]
                t_str = lines[-1] if lines else ai_out
                movie_name = safe_component(re.sub(r"(?i)^(?:filme|title|nome)[:\s-]*", "", t_str[: year_m.start()]).strip(" -_.:("))
                year_val = int(year_m.group(1))
                if movie_name and len(movie_name) >= 2:
                    canonical = f"{movie_name} ({year_val})"
                    return {
                        "mongo_id": mid,
                        "curr_parent": doc["parent"],
                        "curr_name": doc["name"],
                        "desired_parent": f"/raphael/Filmes/{canonical}",
                        "desired_name": f"{canonical}{ext}",
                        "reason": f"Local AI Identification: {ai_out[:60]}"
                    }

    except Exception:
        pass

    return None

def ensure_parent_dirs(files_col, parent_path, now):
    user_root = "/raphael"
    if not parent_path.startswith(user_root): return
    rel = [p for p in parent_path[len(user_root):].strip("/").split("/") if p]
    curr = user_root
    for p in rel:
        files_col.update_one(
            {"type": "dir", "name": p, "parent": curr},
            {"$setOnInsert": {"type": "dir", "name": p, "parent": curr, "ctime": now, "mtime": now, "size": 0}},
            upsert=True
        )
        curr = f"{curr}/{p}"

def main():
    apply = "--apply" in sys.argv
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    files_col = db.files

    porno_pattern = re.compile(r"^/raphael/Porno")
    porno_docs = list(files_col.find({"type": "file", "parent": porno_pattern}))
    print(f"Direct probing {len(porno_docs)} files under /raphael/Porno on drive N:...")

    realignments = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(probe_and_realign_doc, d): d for d in porno_docs}
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                realignments.append(res)
                print(f"  [REALIGNED] {res['curr_name'][:40]} -> {res['desired_parent']}/{res['desired_name']}")

    print(f"\nMode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Total files identified to move out of Porno: {len(realignments)}")

    if not apply or not realignments:
        return

    now = int(time.time())
    applied = 0
    quarantined = 0

    for r in realignments:
        ensure_parent_dirs(files_col, r["desired_parent"], now)
        try:
            files_col.update_one(
                {"_id": ObjectId(r["mongo_id"])},
                {"$set": {
                    "parent": r["desired_parent"],
                    "name": r["desired_name"],
                    "mtime": now
                }}
            )
            applied += 1
        except pymongo.errors.DuplicateKeyError:
            quar_parent = f"/raphael/Auditoria/Duplicatas/{r['desired_parent'].replace('/raphael/', '')}"
            stem = os.path.splitext(r["desired_name"])[0]
            ext = os.path.splitext(r["desired_name"])[1]
            quar_name = f"{stem}__{r['mongo_id'][-8:]}{ext}"
            ensure_parent_dirs(files_col, quar_parent, now)
            try:
                files_col.update_one(
                    {"_id": ObjectId(r["mongo_id"])},
                    {"$set": {
                        "parent": quar_parent,
                        "name": quar_name,
                        "mtime": now
                    }}
                )
                quarantined += 1
            except pymongo.errors.DuplicateKeyError:
                pass

    # Clean empty directories
    deleted = 0
    dirs = list(files_col.find({"type": "dir", "parent": porno_pattern}))
    for d in dirs:
        dir_path = f"{d['parent']}/{d['name']}"
        if files_col.count_documents({"parent": dir_path}) == 0:
            files_col.delete_one({"_id": d["_id"]})
            deleted += 1

    print(f"\nSUCCESS: Applied {applied} realignments out of Porno, quarantined {quarantined} duplicates, deleted {deleted} empty folders!")

if __name__ == "__main__":
    main()
