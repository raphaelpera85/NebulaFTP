import json
import os
import sys
import pymongo
from pathlib import Path

def main():
    frames_dir = Path("d:/Users/rapha/Documents/Projetos/nebula/media_audit/porno_frames")
    if not frames_dir.exists():
        print("Frames dir does not exist.")
        return

    frames = list(frames_dir.glob("*.jpg"))
    print(f"Total extracted frame images found: {len(frames)}")

    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    
    for f in frames:
        mid = f.stem
        doc = db.files.find_one({"_id": pymongo.has_hash if False else None})
        try:
            from bson import ObjectId
            doc = db.files.find_one({"_id": ObjectId(mid)})
            if doc:
                print(f"  Frame {f.name} ({f.stat().st_size} bytes) -> Mongo Name: {doc['name'][:50]}")
        except Exception:
            pass

if __name__ == "__main__":
    main()
