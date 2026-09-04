import json
import os
import sys
import pymongo

sys.stdout.reconfigure(encoding='utf-8')

def main():
    state_path = "d:/Users/rapha/Documents/Projetos/nebula/NebulaFTP-master/feed_ftp_state.json"
    with open(state_path, "r", encoding="utf-8") as f:
        raw_paths = json.load(f)

    print(f"Total raw items in feed_ftp_state.json: {len(raw_paths)}")

    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    docs = list(db.files.find({"type": "file"}))

    print(f"Total live file docs in MongoDB: {len(docs)}")

    # Check how doc size matches raw_paths file sizes if files exist on D: or if sizes are stored
    # Also inspect parts total size
    size_map = {}
    for d in docs:
        sz = d.get("size", 0)
        parts = d.get("parts", [])
        computed_sz = sum(p.get("size", 0) for p in parts)
        if sz not in size_map:
            size_map[sz] = []
        size_map[sz].append(d)

    print(f"Unique size count in MongoDB docs: {len(size_map)}")
    
    sample_items = list(size_map.items())[:10]
    for sz, doc_list in sample_items:
        print(f"  Size {sz} bytes -> {len(doc_list)} doc(s)")
        for d in doc_list[:2]:
            print(f"    ID: {d['_id']} | Parent: {d['parent']} | Name: {d['name']}")

if __name__ == "__main__":
    main()
