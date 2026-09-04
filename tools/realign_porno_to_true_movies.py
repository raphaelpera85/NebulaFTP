import json
import os
import sys
import re
import unicodedata
import time
import pymongo
from bson import ObjectId

sys.stdout.reconfigure(encoding='utf-8')

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

def main():
    apply = "--apply" in sys.argv
    inventory_path = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/http_probe/ffprobe_inventory.json"
    with open(inventory_path, "r", encoding="utf-8") as f:
        items = json.load(f)

    inv_map = {x["mongo_id"]: x for x in items}

    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    files_col = db.files

    porno_pattern = re.compile(r"^/raphael/Porno")
    porno_docs = list(files_col.find({"type": "file", "parent": porno_pattern}))
    print(f"Total documents under /raphael/Porno to inspect: {len(porno_docs)}")

    realignments = []

    for idx, doc in enumerate(porno_docs, 1):
        mid = str(doc["_id"])
        item = inv_map.get(mid, {})
        if not item:
            continue

        fmt_tags = item.get("format_tags") or {}
        v_tags = item.get("video_tags") or {}
        dur = item.get("duration_seconds") or 0

        all_tags = {**fmt_tags, **v_tags}
        raw_titles = [str(val).strip() for k, val in all_tags.items() if k in ("title", "show", "movie_name", "comment") and val]
        title_text = " ".join(raw_titles)
        ext = os.path.splitext(doc["name"])[1] or ".mp4"

        # Check 1: Stream contains SxxEyy
        ep_match = EPISODE_RE.search(title_text)
        if ep_match:
            show_raw = clean_release_prefix(title_text[: ep_match.start()])
            show_raw = re.sub(r"[._ -]+$", "", show_raw)
            show_raw = re.sub(r"(?:[._ -]+)(?:19|20)\d{2}$", "", show_raw)
            show = safe_component(smart_title(show_raw))
            if show and len(show) >= 2:
                season, episode = int(ep_match.group(1)), int(ep_match.group(2))
                desired_parent = f"/raphael/Series/{show}/Season {season:02d}"
                desired_name = safe_component(f"{show} - S{season:02d}E{episode:02d}") + ext
                realignments.append({
                    "mongo_id": mid,
                    "curr_parent": doc["parent"],
                    "curr_name": doc["name"],
                    "desired_parent": desired_parent,
                    "desired_name": desired_name,
                    "reason": f"Series Stream Tag: {title_text}"
                })
                print(f"[{idx}/{len(porno_docs)}] Found Series: {doc['name'][:40]} -> {desired_parent}/{desired_name}")
                continue

        # Check 2: Stream contains Movie (YYYY) and duration >= 35 min
        year_match = YEAR_RE.search(title_text)
        if year_match and dur >= 35 * 60:
            cleaned_title = clean_release_prefix(title_text)
            before = cleaned_title[: year_match.start()]
            before = re.sub(r"(?i)\s+(?:season|temporada)\s*$", "", before)
            before = re.sub(r"[.(\s]+$", "", before)
            movie = safe_component(smart_title(before))
            if movie and len(movie) >= 2:
                year = int(year_match.group(1))
                canonical = safe_component(f"{movie} ({year})")
                desired_parent = f"/raphael/Filmes/{canonical}"
                desired_name = canonical + ext
                realignments.append({
                    "mongo_id": mid,
                    "curr_parent": doc["parent"],
                    "curr_name": doc["name"],
                    "desired_parent": desired_parent,
                    "desired_name": desired_name,
                    "reason": f"Movie Stream Tag: {title_text}"
                })
                print(f"[{idx}/{len(porno_docs)}] Found Movie: {doc['name'][:40]} -> {desired_parent}/{desired_name}")
                continue

    print(f"\nMode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Total files in Porno identified as real movies/series: {len(realignments)}")

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

    # Delete empty directories in Porno
    deleted = 0
    dirs = list(files_col.find({"type": "dir", "parent": porno_pattern}))
    for d in dirs:
        dir_path = f"{d['parent']}/{d['name']}"
        if files_col.count_documents({"parent": dir_path}) == 0:
            files_col.delete_one({"_id": d["_id"]})
            deleted += 1

    print(f"\nSUCCESS: Realigned {applied} files out of Porno to true movies/series, quarantined {quarantined} duplicates, deleted {deleted} empty folders!")

if __name__ == "__main__":
    main()
