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
ALT_EPISODE_RE = re.compile(r"(?i)\b(\d{1,2})x(\d{1,3})\b")
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

def purge_temp_and_empty_dirs(files_col):
    print("--- STEP 1: Purging all __TEMP__ junk folders and empty directories ---")
    user_root = "/raphael"
    categories_to_keep = {"Filmes", "Series", "Porno", "Auditoria"}

    # 1. Delete all __TEMP__ directories
    temp_dirs = list(files_col.find({"type": "dir", "name": {"$regex": "^__TEMP__"}}))
    deleted_temp = 0
    for d in temp_dirs:
        # Move any child files inside temp dir to Auditoria
        dir_path = f"{d['parent']}/{d['name']}"
        child_files = list(files_col.find({"type": "file", "parent": dir_path}))
        for cf in child_files:
            quar_parent = "/raphael/Auditoria/Duplicatas"
            ensure_parent_dirs(files_col, quar_parent, int(time.time()))
            files_col.update_one({"_id": cf["_id"]}, {"$set": {"parent": quar_parent, "name": f"{cf['name']}__{str(cf['_id'])[-8:]}"}})
        files_col.delete_one({"_id": d["_id"]})
        deleted_temp += 1
    print(f"Deleted {deleted_temp} __TEMP__ junk folder records.")

    # 2. Recursively delete empty directories
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
    print(f"Deleted {deleted_empty} empty directory records.")

def main():
    apply = "--apply" in sys.argv
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    files_col = db.files

    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")

    if apply:
        purge_temp_and_empty_dirs(files_col)
    else:
        temp_count = files_col.count_documents({"type": "dir", "name": {"$regex": "^__TEMP__"}})
        print(f"Found {temp_count} __TEMP__ junk folders to delete in APPLY mode.")

    # Check inventory
    inventory_path = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/http_probe/ffprobe_inventory.json"
    with open(inventory_path, "r", encoding="utf-8") as f:
        items = json.load(f)
    inv_map = {x["mongo_id"]: x for x in items}

    all_files = list(files_col.find({"type": "file"}))
    print(f"Total live files in MongoDB: {len(all_files)}")

    realignments = []

    for idx, doc in enumerate(all_files, 1):
        mid = str(doc["_id"])
        item = inv_map.get(mid, {})

        curr_parent = doc["parent"]
        curr_name = doc["name"]

        fmt_tags = item.get("format_tags") or {}
        v_tags = item.get("video_tags") or {}
        dur = item.get("duration_seconds") or 0

        all_tags = {**fmt_tags, **v_tags}
        raw_titles = [str(val).strip() for k, val in all_tags.items() if k in ("title", "show", "movie_name", "comment") and val]
        title_text = " ".join(raw_titles)
        ext = os.path.splitext(curr_name)[1] or ".mp4"

        is_adult_name = bool(re.search(r"(?i)\b(porno|porn|xxx|hentai|adulto|brazzers|bangbros|stepbro|stepsis|stepmom|creampie|pussy|cock)\b", curr_name + " " + curr_parent))

        # Check SxxEyy
        ep_match = EPISODE_RE.search(title_text) or EPISODE_RE.search(curr_name)
        if ep_match and not is_adult_name:
            search_str = title_text if EPISODE_RE.search(title_text) else curr_name
            m = EPISODE_RE.search(search_str)
            show_raw = clean_release_prefix(search_str[: m.start()])
            show_raw = re.sub(r"[._ -]+$", "", show_raw)
            show_raw = re.sub(r"(?:[._ -]+)(?:19|20)\d{2}$", "", show_raw)
            show = safe_component(smart_title(show_raw))
            if show and len(show) >= 2:
                season, episode = int(m.group(1)), int(m.group(2))
                desired_parent = f"/raphael/Series/{show}/Season {season:02d}"
                desired_name = safe_component(f"{show} - S{season:02d}E{episode:02d}") + ext
                if curr_parent != desired_parent or curr_name != desired_name:
                    realignments.append({
                        "mongo_id": mid,
                        "curr": f"{curr_parent}/{curr_name}",
                        "desired_parent": desired_parent,
                        "desired_name": desired_name,
                        "reason": "Series SxxEyy"
                    })
                    continue

        # Check Movie (YYYY)
        year_match = YEAR_RE.search(title_text) or YEAR_RE.search(curr_name)
        if year_match and dur >= 35 * 60 and not is_adult_name:
            search_str = title_text if YEAR_RE.search(title_text) else curr_name
            ym = YEAR_RE.search(search_str)
            cleaned = clean_release_prefix(search_str)
            before = cleaned[: ym.start()]
            before = re.sub(r"(?i)\s+(?:season|temporada)\s*$", "", before)
            before = re.sub(r"[.(\s]+$", "", before)
            movie = safe_component(smart_title(before))
            if movie and len(movie) >= 2:
                year = int(ym.group(1))
                canonical = safe_component(f"{movie} ({year})")
                desired_parent = f"/raphael/Filmes/{canonical}"
                desired_name = canonical + ext
                if curr_parent != desired_parent or curr_name != desired_name:
                    realignments.append({
                        "mongo_id": mid,
                        "curr": f"{curr_parent}/{curr_name}",
                        "desired_parent": desired_parent,
                        "desired_name": desired_name,
                        "reason": "Movie (YYYY)"
                    })
                    continue

    print(f"\nTotal planned realignments: {len(realignments)}")
    for r in realignments[:15]:
        print(f"  [{r['reason']}] {r['curr']} -> {r['desired_parent']}/{r['desired_name']}")

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

    # Final cleanup of empty directories
    purge_temp_and_empty_dirs(files_col)
    print(f"\nSUCCESS: Applied {applied} realignments, quarantined {quarantined} duplicates!")

if __name__ == "__main__":
    main()
