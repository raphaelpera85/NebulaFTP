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

GENERIC_TITLES = re.compile(
    r"(?i)^(?:comando(?:\.la)?|lapumia(?:filmes\.com)?|by .+|acesse .+|"
    r"www\.[^ ]+|bludv(?:\.to)?|the pirate filmes|dual|dublado|legendado|portuguese|720p|1080p|4k|brrip|webrip|videotrack|audiotrack|ingles|espanhol)$"
)

PREFIX_RE = re.compile(r"(?i)^(?:galaxyrg(?:265)?|galaxytv|rarbg|psa|yts(?:\.[a-z]+)?)\s*-\s*")

def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

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

def get_actual_ext(item, default_name=""):
    fmt = (item.get("format_name") or "").casefold()
    if "matroska" in fmt: return ".mkv"
    if any(n in fmt for n in ("mov", "mp4", "m4a")): return ".mp4"
    if "avi" in fmt: return ".avi"
    ext = os.path.splitext(default_name)[1].lower()
    return ext if ext else ".mkv"

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

    print(f"Total inventory items: {len(items)}, Live MongoDB file docs: {len(docs)}")

    planned_moves = []
    quarantined = []

    for item in items:
        mid = item["mongo_id"]
        if mid not in docs:
            continue
        doc = docs[mid]

        curr_parent = doc["parent"]
        curr_name = doc["name"]
        duration = item.get("duration_seconds") or 0

        tags = item.get("format_tags") or {}
        vtags = item.get("video_tags") or {}
        all_tags = {**tags, **vtags}
        raw_titles = []
        for k in ("title", "show", "movie_name", "description", "comment"):
            val = all_tags.get(k)
            if val and str(val).strip():
                raw_titles.append(str(val).strip())

        title_text = " ".join(raw_titles)
        ext = get_actual_ext(item, curr_name)

        # Check adult keywords in name or tags
        is_adult = bool(re.search(r"(?i)\b(porno|porn|xxx|hentai|adulto|brazzers|bangbros|stepbro|stepsis|stepmom|creampie|pussy|cock)\b", curr_name + " " + curr_parent + " " + title_text))
        if is_adult and not curr_parent.startswith("/raphael/Porno"):
            folder_name = safe_component(os.path.splitext(curr_name)[0])
            desired_parent = f"/raphael/Porno/{folder_name}"
            desired_name = curr_name
            planned_moves.append({
                "mongo_id": mid,
                "curr": f"{curr_parent}/{curr_name}",
                "desired_parent": desired_parent,
                "desired_name": desired_name,
                "reason": "adult_content"
            })
            continue

        # Check SxxEyy episode pattern
        ep_match = EPISODE_RE.search(title_text) or EPISODE_RE.search(curr_name)
        if ep_match and not is_adult:
            search_str = title_text if EPISODE_RE.search(title_text) else curr_name
            m = EPISODE_RE.search(search_str)
            show_raw = clean_release_prefix(search_str[: m.start()])
            show_raw = re.sub(r"[._ -]+$", "", show_raw)
            show_raw = re.sub(r"(?:[._ -]+)(?:19|20)\d{2}$", "", show_raw)
            show = safe_component(smart_title(show_raw))
            if len(normalized(show)) >= 2 and not GENERIC_TITLES.match(normalized(show)):
                season, episode = int(m.group(1)), int(m.group(2))
                desired_parent = f"/raphael/Series/{show}/Season {season:02d}"
                desired_name = safe_component(f"{show} - S{season:02d}E{episode:02d}") + ext
                if curr_parent != desired_parent or curr_name != desired_name:
                    planned_moves.append({
                        "mongo_id": mid,
                        "curr": f"{curr_parent}/{curr_name}",
                        "desired_parent": desired_parent,
                        "desired_name": desired_name,
                        "reason": "series_episode"
                    })
                    continue

        # Check Movie (YYYY) pattern
        year_match = YEAR_RE.search(title_text) or YEAR_RE.search(curr_name)
        if year_match and duration >= 40 * 60 and not is_adult and not curr_parent.startswith("/raphael/Series"):
            search_str = title_text if YEAR_RE.search(title_text) else curr_name
            ym = YEAR_RE.search(search_str)
            cleaned_title = clean_release_prefix(search_str)
            before = cleaned_title[: ym.start()]
            before = re.sub(r"(?i)\s+(?:season|temporada)\s*$", "", before)
            before = re.sub(r"[.(\s]+$", "", before)
            movie = safe_component(smart_title(before))
            if len(normalized(movie)) >= 2 and not GENERIC_TITLES.match(normalized(movie)):
                year = int(ym.group(1))
                canonical = safe_component(f"{movie} ({year})")
                desired_parent = f"/raphael/Filmes/{canonical}"
                desired_name = canonical + ext
                if curr_parent != desired_parent or curr_name != desired_name:
                    planned_moves.append({
                        "mongo_id": mid,
                        "curr": f"{curr_parent}/{curr_name}",
                        "desired_parent": desired_parent,
                        "desired_name": desired_name,
                        "reason": "movie"
                    })
                    continue

    print(f"\nMode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Total planned media moves: {len(planned_moves)}")
    for m in planned_moves[:20]:
        print(f"[{m['reason']}] {m['curr']} -> {m['desired_parent']}/{m['desired_name']}")

    if not apply:
        return

    now = int(time.time())
    applied_count = 0
    quarantine_count = 0

    for m in planned_moves:
        ensure_parent_dirs(files_col, m["desired_parent"], now)
        try:
            files_col.update_one(
                {"_id": ObjectId(m["mongo_id"])},
                {"$set": {
                    "parent": m["desired_parent"],
                    "name": m["desired_name"],
                    "mtime": now
                }}
            )
            applied_count += 1
        except pymongo.errors.DuplicateKeyError:
            quar_parent = f"/raphael/Auditoria/Duplicatas/{m['desired_parent'].replace('/raphael/', '')}"
            stem = os.path.splitext(m["desired_name"])[0]
            ext = os.path.splitext(m["desired_name"])[1]
            quar_name = f"{stem}__{m['mongo_id'][-8:]}{ext}"
            ensure_parent_dirs(files_col, quar_parent, now)
            try:
                files_col.update_one(
                    {"_id": ObjectId(m["mongo_id"])},
                    {"$set": {
                        "parent": quar_parent,
                        "name": quar_name,
                        "mtime": now
                    }}
                )
                quarantine_count += 1
            except pymongo.errors.DuplicateKeyError:
                pass

    # Clean empty directories
    deleted_dirs = 0
    user_root = "/raphael"
    categories_to_keep = {"Filmes", "Series", "Porno", "Auditoria"}
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
                deleted_dirs += 1
        if cleaned == 0:
            break

    print(f"\nSUCCESS: Applied {applied_count} moves, quarantined {quarantine_count} duplicates, cleaned {deleted_dirs} empty directories!")

if __name__ == "__main__":
    main()
