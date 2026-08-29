import json
import os
import sys
import re
import unicodedata
import time
import pymongo

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

def get_actual_ext(item):
    fmt = (item.get("format_name") or "").casefold()
    if "matroska" in fmt: return ".mkv"
    if any(n in fmt for n in ("mov", "mp4", "m4a")): return ".mp4"
    if "avi" in fmt: return ".avi"
    return ".mkv"

def extract_true_identity(item):
    duration = item.get("duration_seconds") or 0
    format_tags = item.get("format_tags") or {}
    video_tags = item.get("video_tags") or {}

    all_tags = {**format_tags, **video_tags}
    raw_titles = []
    for k in ("title", "show", "movie_name", "description", "comment"):
        val = all_tags.get(k)
        if val and str(val).strip():
            raw_titles.append(str(val).strip())

    title_text = " ".join(raw_titles)
    ext = get_actual_ext(item)

    if not title_text.strip():
        return None

    # Check 1: SxxEyy episode pattern
    ep_match = EPISODE_RE.search(title_text)
    if ep_match:
        show_raw = clean_release_prefix(title_text[: ep_match.start()])
        show_raw = re.sub(r"[._ -]+$", "", show_raw)
        show_raw = re.sub(r"(?:[._ -]+)(?:19|20)\d{2}$", "", show_raw)
        show = safe_component(smart_title(show_raw))
        if len(normalized(show)) >= 2 and not GENERIC_TITLES.match(normalized(show)):
            season, episode = int(ep_match.group(1)), int(ep_match.group(2))
            filename = safe_component(f"{show} - S{season:02d}E{episode:02d}") + ext
            return {
                "kind": "series_episode",
                "desired_parent": f"/raphael/Series/{show}/Season {season:02d}",
                "desired_name": filename,
                "evidence": f"SxxEyy: {title_text}"
            }

    # Check 2: XxYY episode pattern (e.g. 12x01)
    alt_ep_match = ALT_EPISODE_RE.search(title_text)
    if alt_ep_match:
        season, episode = int(alt_ep_match.group(1)), int(alt_ep_match.group(2))
        show_raw = clean_release_prefix(title_text[: alt_ep_match.start()])
        show_raw = re.sub(r"[._ -]+$", "", show_raw)
        show = safe_component(smart_title(show_raw))
        if len(normalized(show)) >= 2 and not GENERIC_TITLES.match(normalized(show)):
            filename = safe_component(f"{show} - S{season:02d}E{episode:02d}") + ext
            return {
                "kind": "series_episode",
                "desired_parent": f"/raphael/Series/{show}/Season {season:02d}",
                "desired_name": filename,
                "evidence": f"XxYY: {title_text}"
            }

    # Check 3: Movie (YYYY) pattern
    year_match = YEAR_RE.search(title_text)
    if year_match and duration >= 40 * 60:
        cleaned_title = clean_release_prefix(title_text)
        before = cleaned_title[: year_match.start()]
        before = re.sub(r"(?i)\s+(?:season|temporada)\s*$", "", before)
        before = re.sub(r"[.(\s]+$", "", before)
        movie = safe_component(smart_title(before))
        if len(normalized(movie)) >= 2 and not GENERIC_TITLES.match(normalized(movie)):
            year = int(year_match.group(1))
            canonical = safe_component(f"{movie} ({year})")
            return {
                "kind": "movie",
                "desired_parent": f"/raphael/Filmes/{canonical}",
                "desired_name": canonical + ext,
                "evidence": f"Movie (YYYY): {title_text}"
            }

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

    realignments = []

    for item in items:
        mid = item["mongo_id"]
        if mid not in docs:
            continue
        doc = docs[mid]

        identity = extract_true_identity(item)
        if not identity:
            continue

        curr_parent = doc["parent"]
        curr_name = doc["name"]

        if curr_parent != identity["desired_parent"] or curr_name != identity["desired_name"]:
            realignments.append({
                "mongo_id": mid,
                "curr_parent": curr_parent,
                "curr_name": curr_name,
                "desired_parent": identity["desired_parent"],
                "desired_name": identity["desired_name"],
                "kind": identity["kind"],
                "evidence": identity["evidence"]
            })

    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Total stream-verified realignments ready to apply: {len(realignments)}")

    if not apply:
        for r in realignments[:20]:
            print(f"[{r['kind']}] {r['curr_parent']}/{r['curr_name']} -> {r['desired_parent']}/{r['desired_name']}")
        return

    now = int(time.time())
    updated_count = 0
    quarantined_count = 0
    from bson import ObjectId

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
            updated_count += 1
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
                quarantined_count += 1
            except pymongo.errors.DuplicateKeyError:
                pass

    print(f"SUCCESS: Realigned {updated_count} files to their true stream content, quarantined {quarantined_count} duplicates!")

if __name__ == "__main__":
    main()
