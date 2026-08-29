import json
import os
import sys
import re
import time
import urllib.request
import concurrent.futures
import pymongo
from bson import ObjectId

sys.stdout.reconfigure(encoding='utf-8')

LM_STUDIO_URL = "http://127.0.0.1:1234/v1/chat/completions"
MODEL_ID = "huihui-qwen3.5-9b-abliterated"

def safe_component(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]+', " - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value[:180]

def parse_ai_json_or_text(ai_out: str, curr_name: str, dur_min: float) -> dict:
    if not ai_out:
        return {}

    # Extract SxxEyy
    ep_match = re.search(r"(?i)\bS(\d{1,2})[ ._-]*E(\d{1,3})\b", ai_out)
    if ep_match:
        # Find show name before SxxEyy
        idx = ep_match.start()
        raw = ai_out[:idx].strip(" -_.:\"'`\n")
        lines = [l for l in raw.splitlines() if l.strip()]
        show_str = lines[-1] if lines else raw
        show = safe_component(re.sub(r"(?i)^(?:filme|série|title|nome)[:\s-]*", "", show_str).strip())
        season, episode = int(ep_match.group(1)), int(ep_match.group(2))
        if show and len(show) >= 2:
            ext = os.path.splitext(curr_name)[1] or ".mp4"
            return {
                "category": "Series",
                "desired_parent": f"/raphael/Series/{show}/Season {season:02d}",
                "desired_name": f"{show} - S{season:02d}E{episode:02d}{ext}"
            }

    # Extract Movie (YYYY)
    year_match = re.search(r"\b((?:19|20)\d{2})\b", ai_out)
    if year_match and dur_min >= 35.0:
        idx = year_match.start()
        raw = ai_out[:idx].strip(" -_.:\"'`(\n")
        lines = [l for l in raw.splitlines() if l.strip()]
        movie_str = lines[-1] if lines else raw
        movie = safe_component(re.sub(r"(?i)^(?:filme|série|title|nome)[:\s-]*", "", movie_str).strip())
        year = int(year_match.group(1))
        if movie and len(movie) >= 2:
            ext = os.path.splitext(curr_name)[1] or ".mkv"
            canonical = f"{movie} ({year})"
            return {
                "category": "Filmes",
                "desired_parent": f"/raphael/Filmes/{canonical}",
                "desired_name": f"{canonical}{ext}"
            }

    return {}

def process_one(item, doc):
    tags = item.get("format_tags") or {}
    vtags = item.get("video_tags") or {}
    all_tags = {**tags, **vtags}
    raw_titles = [str(val).strip() for k, val in all_tags.items() if k in ("title", "show", "movie_name", "comment") and val]

    title_text = " ".join(raw_titles)
    dur_min = (item.get("duration_seconds") or 0) / 60.0

    if not title_text.strip():
        return None

    prompt = f"""Dado o título extraído do stream do arquivo: "{title_text}" (Nome atual: "{doc['name']}", Duração: {dur_min:.1f} minutos).
Identifique o título limpo em português.
Formato para filme: "Nome do Filme (Ano)"
Formato para série: "Nome da Série - SXXEYY"
"""

    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 150
    }

    req = urllib.request.Request(
        LM_STUDIO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            ai_out = msg.get("content", "").strip() or msg.get("reasoning_content", "").strip()
            
            parsed = parse_ai_json_or_text(ai_out, doc["name"], dur_min)
            if parsed and (doc["parent"] != parsed["desired_parent"] or doc["name"] != parsed["desired_name"]):
                return {
                    "mongo_id": item["mongo_id"],
                    "curr": f"{doc['parent']}/{doc['name']}",
                    "desired_parent": parsed["desired_parent"],
                    "desired_name": parsed["desired_name"],
                    "ai_out": ai_out[:80]
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
    inventory_path = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/http_probe/ffprobe_inventory.json"
    with open(inventory_path, "r", encoding="utf-8") as f:
        items = json.load(f)

    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    files_col = db.files
    docs = {str(d["_id"]): d for d in files_col.find({"type": "file"})}

    print(f"Loaded {len(items)} items. Querying LM Studio Local AI ({MODEL_ID})...")

    valid_items = [it for it in items if it["mongo_id"] in docs][:50]
    ai_realignments = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(process_one, it, docs[it["mongo_id"]]): it for it in valid_items}
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                ai_realignments.append(res)
                print(f"  [LM STUDIO AI MATCH] {res['curr']} -> {res['desired_parent']}/{res['desired_name']}")

    print(f"\nMode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Total Local AI Realignments: {len(ai_realignments)}")

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

    print(f"\nSUCCESS: Applied {applied} Local AI realignments to MongoDB!")

if __name__ == "__main__":
    main()
