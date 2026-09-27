"""Move legacy uploads back to their custom source category.

The old router treated every episode as ``/raphael/Series`` even when its
staging path was under Animações, Doramas or Novelas.  This repair uses the
recorded local staging path, changes only the Mongo parent, and creates any
missing directory nodes.
"""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import PurePosixPath

from pymongo import MongoClient

CATEGORIES = ("Animações", "Doramas", "Novelas")


def repair(mongo_uri: str, database: str, user: str, apply: bool) -> int:
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    files = client[database].files
    user_root = "/" + user.strip("/")
    moved = 0
    dirs: set[tuple[str, str]] = set()
    category_re = re.compile(r"[\\/]((?:Animações|Doramas|Novelas))[\\/](.+)$", re.IGNORECASE)

    for doc in files.find({"type": "file", "local_path": {"$type": "string"}}):
        match = category_re.search(doc.get("local_path", ""))
        if not match:
            continue
        category, relative = match.groups()
        canonical_category = next((item for item in CATEGORIES if item.casefold() == category.casefold()), category)
        relative_parts = [part for part in re.split(r"[\\/]", relative) if part]
        if len(relative_parts) < 2:
            continue
        parent = user_root + "/" + canonical_category + "/" + "/".join(relative_parts[:-1])
        if doc.get("parent") != parent:
            moved += 1
            if apply:
                files.update_one({"_id": doc["_id"]}, {"$set": {"parent": parent}})
        parts = parent.strip("/").split("/")
        for index in range(1, len(parts)):
            dirs.add(("/" + "/".join(parts[:index]), parts[index]))

    if apply:
        now = int(time.time())
        for parent, name in dirs:
            files.update_one(
                {"parent": parent, "name": name, "type": "dir"},
                {"$setOnInsert": {"parent": parent, "name": name, "type": "dir", "size": 0, "ctime": now, "mtime": now}},
                upsert=True,
            )
    client.close()
    print(f"[{'applied' if apply else 'dry-run'}] custom-category files={moved} directories_checked={len(dirs)}")
    return moved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--mongo", default=os.getenv("MONGODB", "mongodb://localhost:27017"))
    parser.add_argument("--database", default=os.getenv("MONGO_DATABASE", "ftp"))
    parser.add_argument("--user", default=os.getenv("NEBULA_LIBRARY_USER", "raphael"))
    args = parser.parse_args()
    repair(args.mongo, args.database, args.user, args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
