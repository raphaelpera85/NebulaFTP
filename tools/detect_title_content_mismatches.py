import json
import os
import sys
import re
import unicodedata
import pymongo

sys.stdout.reconfigure(encoding='utf-8')

def normalize(val):
    val = unicodedata.normalize('NFKD', val or '')
    val = ''.join(ch for ch in val if not unicodedata.combining(ch))
    return re.sub(r'[^a-z0-9]+', ' ', val.casefold()).strip()

GENERIC_TITLES = re.compile(
    r"(?i)^(?:comando(?:\.la)?|lapumia(?:filmes\.com)?|by .+|acesse .+|"
    r"www\.[^ ]+|bludv(?:\.to)?|the pirate filmes|dual|dublado|legendado|portuguese|720p|1080p|4k|brrip|webrip)$"
)

def is_matching(internal_clean, path_norm):
    a = set(internal_clean.split())
    b = set(path_norm.split())
    if not a or not b: return False
    # If key words overlap substantially (e.g. bad sisters s01e09 vs bad sisters s01e09)
    overlap = len(a & b)
    if overlap >= max(1, min(len(a), len(b)) * 0.6):
        return True
    return False

def main():
    inventory_path = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/http_probe/ffprobe_inventory.json"
    with open(inventory_path, "r", encoding="utf-8") as f:
        items = json.load(f)

    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    docs = {str(d["_id"]): d for d in db.files.find({"type": "file"})}

    real_mismatches = []
    for item in items:
        mid = item["mongo_id"]
        if mid not in docs:
            continue
        doc = docs[mid]

        internal_titles = []
        for tags_key in ("format_tags", "video_tags"):
            tags = item.get(tags_key) or {}
            for k in ("title", "show", "movie_name", "description"):
                v = tags.get(k)
                if v and len(str(v).strip()) > 3:
                    internal_titles.append(str(v).strip())

        current_path_norm = normalize(f"{doc['parent']}/{doc['name']}")

        for ititle in internal_titles:
            inorm = normalize(ititle)
            if GENERIC_TITLES.match(inorm) or len(inorm) < 4:
                continue
            clean_inorm = re.sub(r"(?i)\b(1080p|720p|bluray|webrip|web-dl|x264|x265|hevc|aac|dual|dublado|bludv|yts|galaxy|tgx|rarbg)\b", "", inorm).strip()
            
            if not is_matching(clean_inorm, current_path_norm):
                real_mismatches.append({
                    "mongo_id": mid,
                    "current_parent": doc["parent"],
                    "current_name": doc["name"],
                    "internal_title": ititle,
                    "clean_internal": clean_inorm,
                    "duration_seconds": item.get("duration_seconds")
                })
                break

    print(f"REAL Mismatches Detected (stream content vs file name): {len(real_mismatches)}")
    for m in real_mismatches[:20]:
        print(f"Current: {m['current_parent']}/{m['current_name']}")
        print(f" -> Real Content Stream: '{m['internal_title']}' (Duration: {m['duration_seconds']}s)\n")

if __name__ == "__main__":
    main()
