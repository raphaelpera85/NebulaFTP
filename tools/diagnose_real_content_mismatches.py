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

GENERIC_TRACKS = re.compile(
    r"(?i)^(?:ingles|portugues|espanhol|english|spanish|french|german|italian|japanese|"
    r"audiotrack|videotrack|subtitle|subtitles|galaxytv.*|comando.*|lapumia.*|bludv.*|"
    r"the pirate.*|dual|dublado|legendado|480p|720p|1080p|2160p|4k|isom|iso2|avc1|mp41)$"
)

def main():
    inventory_path = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/http_probe/ffprobe_inventory.json"
    with open(inventory_path, "r", encoding="utf-8") as f:
        items = json.load(f)

    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    docs = {str(d["_id"]): d for d in db.files.find({"type": "file"})}

    genuine_mismatches = []

    for item in items:
        mid = item["mongo_id"]
        if mid not in docs:
            continue
        doc = docs[mid]

        current_path_norm = normalize(f"{doc['parent']}/{doc['name']}")

        # Extract title from format_tags or video_tags
        tags = item.get("format_tags") or {}
        vtags = item.get("video_tags") or {}
        all_tags = {**tags, **vtags}

        title_candidates = []
        for k in ("title", "show", "movie_name", "description"):
            val = all_tags.get(k)
            if val:
                val_str = str(val).strip()
                if len(val_str) > 3 and not GENERIC_TRACKS.match(val_str):
                    title_candidates.append(val_str)

        for cand in title_candidates:
            cand_norm = normalize(cand)
            # Remove technical terms
            clean_cand = re.sub(r"(?i)\b(1080p|720p|bluray|webrip|web-dl|x264|x265|hevc|aac|dual|dublado)\b", "", cand_norm).strip()
            
            # Check overlap with current path
            c_words = set(clean_cand.split())
            p_words = set(current_path_norm.split())

            if c_words and p_words:
                overlap = len(c_words & p_words)
                # If less than 30% overlap, flag as mismatch!
                if overlap < max(1, min(len(c_words), len(p_words)) * 0.3):
                    genuine_mismatches.append({
                        "mongo_id": mid,
                        "current_parent": doc["parent"],
                        "current_name": doc["name"],
                        "stream_title": cand,
                        "duration_seconds": item.get("duration_seconds")
                    })
                    break

    print(f"GENUINE Title/Content Mismatches Found: {len(genuine_mismatches)}")
    for m in genuine_mismatches:
        print(f"Path: {m['current_parent']}/{m['current_name']}")
        print(f" -> Real Video Content Title: '{m['stream_title']}' (Duration: {m['duration_seconds']}s)\n")

if __name__ == "__main__":
    main()
