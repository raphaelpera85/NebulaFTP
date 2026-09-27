"""Normalize legacy MongoDB parents into the path format used by the mount.

Older restore/import jobs stored ``parent`` as an ObjectId pointing to a
directory document.  The current FTP path backend indexes children by a
string parent path, so those documents become invisible below the first
directory.  This migration resolves the legacy references and makes every
document use ``/<library-user>/...``.  It is deliberately idempotent.

Usage:
    python tools/normalize_library_paths.py --apply
    python tools/normalize_library_paths.py              # dry run
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import PurePosixPath
from typing import Any

from bson import ObjectId
from pymongo import MongoClient


def normalize_parent(value: Any, user_root: str, category_names: set[str]) -> str | None:
    if not isinstance(value, str):
        return None
    value = "/" + value.strip("/") if value.strip("/") else user_root
    if value == "/":
        return user_root
    if value == user_root or value.startswith(user_root + "/"):
        return value
    first = value.split("/", 2)[1].casefold()
    if first in {name.casefold() for name in category_names}:
        return user_root + value
    # Legacy imports sometimes used /Series or /Filmes and sometimes stored
    # arbitrary top-level paths. Keep the data reachable under the user root.
    return user_root + value


def migrate(*, mongo_uri: str, database: str, user: str, apply: bool) -> dict[str, int]:
    user_root = "/" + user.strip("/")
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    files = client[database].files
    docs = list(files.find({}))
    by_id = {doc["_id"]: doc for doc in docs}
    category_names = {str(doc["name"]) for doc in docs if doc.get("parent") == user_root and doc.get("type") == "dir"}
    resolving: set[ObjectId] = set()
    path_cache: dict[Any, str | None] = {}
    unresolved = 0
    changed = 0
    created_dirs = 0

    def full_path(doc: dict[str, Any]) -> str | None:
        doc_id = doc["_id"]
        if doc_id in path_cache:
            return path_cache[doc_id]
        if doc_id in resolving:
            return None
        resolving.add(doc_id)
        raw_parent = doc.get("parent")
        if isinstance(raw_parent, ObjectId):
            parent_doc = by_id.get(raw_parent)
            parent_path = full_path(parent_doc) if parent_doc else None
        else:
            parent_path = normalize_parent(raw_parent, user_root, category_names)
        name = str(doc.get("name", "")).strip("/")
        result = f"{parent_path}/{name}" if parent_path and name else None
        resolving.discard(doc_id)
        path_cache[doc_id] = result
        return result

    def recover_orphan_path(doc: dict[str, Any]) -> str | None:
        """Recover a file whose old ObjectId parent was deleted."""
        if doc.get("type") != "file":
            return None
        name = str(doc.get("name", ""))
        match = re.search(r"(?i)\bS(\d{1,2})[ ._-]*E\d{1,3}\b", name)
        if not match:
            return None
        series = name[: match.start()].strip(" ._-")
        if not series:
            return None
        season = int(match.group(1))
        return f"{user_root}/Series/{series}/Season {season:02d}/{name}"

    updates: list[tuple[dict[str, Any], str]] = []
    for doc in docs:
        path = full_path(doc) or recover_orphan_path(doc)
        if not path:
            unresolved += 1
            continue
        parent = str(PurePosixPath(path).parent)
        if doc.get("parent") != parent:
            updates.append((doc, parent))
            changed += 1

    # Ensure every directory on every path exists. This also repairs category
    # trees where the intermediate directory was lost during a legacy restore.
    existing_dirs: set[tuple[str, str]] = set()
    for doc in docs:
        if doc.get("type") != "dir":
            continue
        path = full_path(doc) or recover_orphan_path(doc)
        if path:
            existing_dirs.add((str(PurePosixPath(path).parent), str(doc.get("name", ""))))
    desired_dirs: set[tuple[str, str]] = set()
    for doc in docs:
        path = full_path(doc)
        if not path:
            continue
        parts = path.strip("/").split("/")
        # The last component is the file name, never a directory.
        for index in range(1, len(parts) - 1):
            parent = "/" + "/".join(parts[:index])
            desired_dirs.add((parent, parts[index]))
    missing_dirs = sorted(desired_dirs - existing_dirs)
    created_dirs = len(missing_dirs)

    if apply:
        for doc, parent in updates:
            files.update_one({"_id": doc["_id"]}, {"$set": {"parent": parent}})
        now = int(__import__("time").time())
        for parent, name in missing_dirs:
            files.update_one(
                {"parent": parent, "name": name, "type": "dir"},
                {"$setOnInsert": {"parent": parent, "name": name, "type": "dir", "size": 0, "ctime": now, "mtime": now}},
                upsert=True,
            )

    client.close()
    return {"documents": len(docs), "parent_updates": changed, "missing_dirs": created_dirs, "unresolved": unresolved}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the normalized paths")
    parser.add_argument("--mongo", default=os.getenv("MONGODB", "mongodb://localhost:27017"))
    parser.add_argument("--database", default=os.getenv("MONGO_DATABASE", "ftp"))
    parser.add_argument("--user", default=os.getenv("NEBULA_LIBRARY_USER", "raphael"))
    args = parser.parse_args()
    stats = migrate(mongo_uri=args.mongo, database=args.database, user=args.user, apply=args.apply)
    mode = "applied" if args.apply else "dry-run"
    print(f"[{mode}] database={args.database} user=/{args.user}: {stats}")
    return 0 if stats["unresolved"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
