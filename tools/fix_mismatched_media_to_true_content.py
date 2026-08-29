import json
import os
import sys
import re
import unicodedata
import time
import pymongo

sys.stdout.reconfigure(encoding='utf-8')

EPISODE_RE = re.compile(r"(?i)\bS(\d{1,2})[ ._-]*E(\d{1,3})\b")
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")

GENERIC_TITLES = re.compile(
    r"(?i)^(?:comando(?:\.la)?|lapumia(?:filmes\.com)?|by .+|acesse .+|"
    r"www\.[^ ]+|bludv(?:\.to)?|the pirate filmes|dual|dublado|legendado|portuguese|720p|1080p|4k|brrip|webrip)$"
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

def identify_true_content(item):
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

    # 1. Check for Series Episode SxxEyy in tags
    ep_match = EPISODE_RE.search(title_text)
    if ep_match:
        show_raw = clean_release_prefix(title_text[: ep_match.start()])
        show_raw = re.sub(r"[._ -]+$", "", show_raw)
        show_raw = re.sub(r"(?:[._ -]+)(?:19|20)\d{2}$", "", show_raw)
        show = safe_component(smart_title(show_raw))
        if len(normalized(show)) >= 2:
            season, episode = int(ep_match.group(1)), int(ep_match.group(2))
            filename = safe_component(f"{show} - S{season:02d}E{episode:02d}") + ext
            return {
                "kind": "series_episode",
                "desired_parent": f"/raphael/Series/{show}/Season {season:02d}",
                "desired_name": filename,
                "confidence": 0.99,
                "evidence": f"embedded_tag={title_text}"
            }

    # 2. Check for Movie (Year) in tags
    year_match = YEAR_RE.search(title_text)
    if year_match and duration >= 40 * 60:
        cleaned_title = clean_release_prefix(title_text)
        before = cleaned_title[: year_match.start()]
        before = re.sub(r"(?i)\s+(?:season|temporada)\s*$", "", before)
        before = re.sub(r"[.(\s]+$", "", before)
        movie = safe_component(smart_title(before))
        if len(normalized(movie)) >= 2 and not GENERIC_TITLES.match(movie):
            year = int(year_match.group(1))
            canonical = safe_component(f"{movie} ({year})")
            return {
                "kind": "movie",
                "desired_parent": f"/raphael/Filmes/{canonical}",
                "desired_name": canonical + ext,
                "confidence": 0.98,
                "evidence": f"embedded_movie_tag={title_text}"
            }

    # 3. Check for Bleach Episode titles in tags
    if "---" in title_text or "Bleach" in title_text or "shinigami" in title_text.lower():
        ep_num_match = re.search(r"(?i)(?:epis[oó]dio|ep)\s*(\d{1,3})", title_text)
        if ep_num_match:
            ep_num = int(ep_num_match.group(1))
            season_num = 1
            if 21 <= ep_num <= 41: season_num = 2
            elif 42 <= ep_num <= 63: season_num = 3
            elif 64 <= ep_num <= 91: season_num = 4
            elif 92 <= ep_num <= 109: season_num = 5
            elif 110 <= ep_num <= 137: season_num = 6
            elif 138 <= ep_num <= 167: season_num = 7
            elif 168 <= ep_num <= 189: season_num = 8
            elif 190 <= ep_num <= 205: season_num = 9
            elif 206 <= ep_num <= 212: season_num = 10
            elif 213 <= ep_num <= 229: season_num = 11
            elif 230 <= ep_num <= 265: season_num = 12
            elif 266 <= ep_num <= 316: season_num = 13
            elif 317 <= ep_num <= 342: season_num = 14
            elif 343 <= ep_num <= 366: season_num = 15

            filename = f"Bleach S{season_num:02d}E{ep_num:02d}.mp4"
            return {
                "kind": "bleach_episode",
                "desired_parent": f"/raphael/Series/Bleach/Season {season_num:02d}",
                "desired_name": filename,
                "confidence": 0.95,
                "evidence": f"bleach_tag={title_text}"
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

    repaired_actions = []

    for item in items:
        mid = item["mongo_id"]
        if mid not in docs:
            continue
        doc = docs[mid]
        
        info = identify_true_content(item)
        if not info:
            continue

        current_parent = doc["parent"]
        current_name = doc["name"]

        if current_parent != info["desired_parent"] or current_name != info["desired_name"]:
            repaired_actions.append({
                "mongo_id": mid,
                "current_parent": current_parent,
                "current_name": current_name,
                "desired_parent": info["desired_parent"],
                "desired_name": info["desired_name"],
                "kind": info["kind"],
                "evidence": info["evidence"]
            })

    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Total stream-verified content corrections calculated: {len(repaired_actions)}")
    
    if not apply:
        for a in repaired_actions[:15]:
            print(f"[{a['kind']}] {a['current_parent']}/{a['current_name']} -> {a['desired_parent']}/{a['desired_name']}")
        return

    now = int(time.time())
    updated_count = 0
    quarantined_count = 0
    from bson import ObjectId

    for a in repaired_actions:
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
            updated_count += 1
        except pymongo.errors.DuplicateKeyError:
            # Move to Auditoria
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
                quarantined_count += 1
            except pymongo.errors.DuplicateKeyError:
                pass

    print(f"SUCCESS: Updated {updated_count} documents to true content paths, quarantined {quarantined_count} duplicates!")

if __name__ == "__main__":
    main()
