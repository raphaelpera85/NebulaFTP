import json
import os
import sys
import re
import unicodedata
import time
import urllib.request
from pathlib import Path
import pymongo
from bson import ObjectId

sys.stdout.reconfigure(encoding='utf-8')

LM_STUDIO_URL = "http://127.0.0.1:1234/v1/chat/completions"
MODEL_ID = "huihui-qwen3.5-9b-abliterated"

def safe_component(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]+', " - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value[:180]

def query_local_ai(probe_title: str, curr_name: str, duration_min: float) -> str:
    prompt = f"""Analise estas informações do vídeo e identifique o nome limpo do filme ou série em português:

- Tag do Stream: "{probe_title}"
- Nome Atual do Arquivo: "{curr_name}"
- Duração: {duration_min:.1f} minutos

Responda APENAS com o formato final:
Para filme: Nome do Filme (Ano)
Para série/anime: Nome da Série - SXXEYY
"""

    payload = {
        "model": MODEL_ID,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 200
    }

    req = urllib.request.Request(
        LM_STUDIO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            content = msg.get("content", "").strip()
            reasoning = msg.get("reasoning_content", "").strip()
            return content if content else reasoning
    except Exception as e:
        print(f"Error querying local AI: {e}")
        return ""

def parse_ai_suggestion(ai_text: str, duration_min: float, curr_name: str) -> dict:
    if not ai_text:
        return {}

    lines = [l.strip() for l in ai_text.splitlines() if l.strip()]
    last_line = lines[-1] if lines else ai_text

    # Extract SxxEyy
    ep_match = re.search(r"(?i)\bS(\d{1,2})[ ._-]*E(\d{1,3})\b", last_line)
    if ep_match:
        show = safe_component(last_line[: ep_match.start()].strip(" -_."))
        season, episode = int(ep_match.group(1)), int(ep_match.group(2))
        if show:
            ext = os.path.splitext(curr_name)[1] or ".mp4"
            return {
                "category": "Series",
                "desired_parent": f"/raphael/Series/{show}/Season {season:02d}",
                "desired_name": f"{show} - S{season:02d}E{episode:02d}{ext}"
            }

    # Extract Movie (YYYY)
    year_match = re.search(r"\b((?:19|20)\d{2})\b", last_line)
    if year_match and duration_min >= 35.0:
        movie = safe_component(last_line[: year_match.start()].strip(" -_.( "))
        year = int(year_match.group(1))
        if movie:
            ext = os.path.splitext(curr_name)[1] or ".mkv"
            canonical = f"{movie} ({year})"
            return {
                "category": "Filmes",
                "desired_parent": f"/raphael/Filmes/{canonical}",
                "desired_name": f"{canonical}{ext}"
            }

    return {}

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
    inventory_path = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/http_probe/ffprobe_inventory.json"
    with open(inventory_path, "r", encoding="utf-8") as f:
        items = json.load(f)

    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    files_col = db.files
    docs = {str(d["_id"]): d for d in files_col.find({"type": "file"})}

    print(f"Loaded {len(items)} items from inventory, {len(docs)} live docs in Mongo.")
    print(f"Connecting to Local LM Studio AI at {LM_STUDIO_URL} ({MODEL_ID})...")

    ai_realignments = []

    for item in items[:50]:  # Batch test first 50 items
        mid = item["mongo_id"]
        if mid not in docs:
            continue
        doc = docs[mid]

        tags = item.get("format_tags") or {}
        vtags = item.get("video_tags") or {}
        all_tags = {**tags, **vtags}
        raw_titles = [str(val).strip() for k, val in all_tags.items() if k in ("title", "show", "movie_name", "comment") and val]

        title_text = " ".join(raw_titles)
        dur_min = (item.get("duration_seconds") or 0) / 60.0

        if not title_text.strip():
            continue

        ai_out = query_local_ai(title_text, doc["name"], dur_min)
        parsed = parse_ai_suggestion(ai_out, dur_min, doc["name"])

        if parsed and (doc["parent"] != parsed["desired_parent"] or doc["name"] != parsed["desired_name"]):
            ai_realignments.append({
                "mongo_id": mid,
                "curr": f"{doc['parent']}/{doc['name']}",
                "desired_parent": parsed["desired_parent"],
                "desired_name": parsed["desired_name"],
                "ai_output": ai_out[:80]
            })

    print(f"\nMode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Total Local AI Realignments Identified: {len(ai_realignments)}")
    for r in ai_realignments:
        print(f"  [AI] {r['curr']} -> {r['desired_parent']}/{r['desired_name']}")

    if not apply or not ai_realignments:
        return

    now = int(time.time())
    applied = 0
    quarantined = 0

    for r in ai_realignments:
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

    print(f"\nSUCCESS: Applied {applied} Local AI realignments, quarantined {quarantined} duplicates!")

if __name__ == "__main__":
    main()
