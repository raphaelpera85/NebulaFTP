import json
import os
import sys
import re
import time
import pymongo
from bson import ObjectId

sys.stdout.reconfigure(encoding='utf-8')

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
    rec_path = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/visual_probe/anime_recognition.json"
    if not os.path.exists(rec_path):
        print(f"Error: {rec_path} not found.")
        return

    with open(rec_path, "r", encoding="utf-8") as f:
        items = json.load(f)

    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    files_col = db.files
    docs = {str(d["_id"]): d for d in files_col.find({"type": "file"})}

    visual_actions = []

    for item in items:
        mid = item["mongo_id"]
        if mid not in docs:
            continue
        doc = docs[mid]
        
        matches = item.get("matches") or []
        if not matches:
            continue

        top = matches[0]
        similarity = float(top.get("similarity") or 0)
        
        # Require >= 90% confidence for visual classification
        if similarity < 0.90:
            continue

        title = top.get("title_english") or top.get("title_romaji") or top.get("matched_filename")
        if not title:
            continue
        show = re.sub(r'[<>:"/\\|?*]+', "", str(title)).strip()
        if not show:
            continue

        ep_raw = top.get("episode") or 1
        try:
            if isinstance(ep_raw, str):
                ep_int = int(re.search(r'\d+', ep_raw).group())
            else:
                ep_int = int(ep_raw)
        except Exception:
            ep_int = 1

        ext = os.path.splitext(doc["name"])[1] or ".mp4"
        season = 1
        desired_parent = f"/raphael/Series/{show}/Season {season:02d}"
        desired_name = f"{show} - S{season:02d}E{ep_int:02d}{ext}"

        if doc["parent"] != desired_parent or doc["name"] != desired_name:
            visual_actions.append({
                "mongo_id": mid,
                "curr_parent": doc["parent"],
                "curr_name": doc["name"],
                "desired_parent": desired_parent,
                "desired_name": desired_name,
                "title": show,
                "episode": ep_int,
                "similarity": similarity
            })

    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"High-confidence visual scene recognitions (>90%): {len(visual_actions)}")

    for a in visual_actions:
        print(f"[{a['similarity']*100:.1f}% Match] '{a['curr_name']}'")
        print(f"  --> VISUALLY RECOGNIZED AS: {a['desired_parent']}/{a['desired_name']}\n")

    if not apply:
        return

    now = int(time.time())
    updated = 0
    quarantined = 0

    for a in visual_actions:
        ensure_parent_dirs(files_col, a["desired_parent"], now)
        try:
            files_col.update_one(
                {"_id": ObjectId(a["mongo_id"])},
                {"$set": {
                    "parent": a["desired_parent"],
                    "name": a["desired_name"],
                    "mtime": now
                }}
            )
            updated += 1
        except pymongo.errors.DuplicateKeyError:
            quar_parent = f"/raphael/Auditoria/Duplicatas/{a['desired_parent'].replace('/raphael/', '')}"
            stem = os.path.splitext(a["desired_name"])[0]
            ext = os.path.splitext(a["desired_name"])[1]
            quar_name = f"{stem}__{a['mongo_id'][-8:]}{ext}"
            ensure_parent_dirs(files_col, quar_parent, now)
            try:
                files_col.update_one(
                    {"_id": ObjectId(a["mongo_id"])},
                    {"$set": {
                        "parent": quar_parent,
                        "name": quar_name,
                        "mtime": now
                    }}
                )
                quarantined += 1
            except pymongo.errors.DuplicateKeyError:
                pass

    print(f"SUCCESS: Realigned {updated} visual matches in MongoDB, quarantined {quarantined} duplicates!")

if __name__ == "__main__":
    main()
