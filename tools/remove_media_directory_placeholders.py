"""Remove directory documents accidentally created for media file names."""

from __future__ import annotations

import argparse
import os
from pymongo import MongoClient

MEDIA_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".webm",
    ".flv", ".mpeg", ".mpg", ".m2ts", ".3gp", ".srt", ".ass", ".ssa", ".vtt",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--mongo", default=os.getenv("MONGODB", "mongodb://localhost:27017"))
    parser.add_argument("--database", default=os.getenv("MONGO_DATABASE", "ftp"))
    args = parser.parse_args()
    client = MongoClient(args.mongo, serverSelectionTimeoutMS=5000)
    files = client[args.database].files
    ids = [
        doc["_id"] for doc in files.find(
            {"type": "dir", "name": {"$regex": r"(?i)\.(?:mkv|mp4|avi|mov|wmv|m4v|ts|webm|flv|mpeg|mpg|m2ts|3gp|srt|ass|ssa|vtt)$"}},
            {"_id": 1},
        )
    ]
    if args.apply and ids:
        result = files.delete_many({"_id": {"$in": ids}})
        removed = result.deleted_count
    else:
        removed = 0
    print(f"[{'applied' if args.apply else 'dry-run'}] placeholders={len(ids)} removed={removed}")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
