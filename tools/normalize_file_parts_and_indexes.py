from __future__ import annotations

from os import environ

from pymongo import MongoClient, UpdateOne


def _sort_parts(parts):
    return sorted(parts or [], key=lambda part: int(part.get("part_id", 0)))


def main():
    client = MongoClient(environ.get("MONGODB", "mongodb://localhost:27017"))
    db = client[environ.get("MONGO_DATABASE", "ftp")]
    files = db.files

    scanned = 0
    reordered = 0
    bulk_ops = []

    query = {"type": "file", "parts.1": {"$exists": True}}
    projection = {"parts": 1}

    for doc in files.find(query, projection):
        scanned += 1
        parts = doc.get("parts") or []
        ordered = _sort_parts(parts)
        if parts != ordered:
            reordered += 1
            bulk_ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"parts": ordered}}))

    if bulk_ops:
        files.bulk_write(bulk_ops, ordered=False)

    files.create_index([("type", 1), ("status", 1), ("uploaded_at", -1)])
    files.create_index("status")
    files.create_index("uploaded_at")

    print(f"parts_scan={scanned} reordered={reordered} index_backfill=done")


if __name__ == "__main__":
    main()
