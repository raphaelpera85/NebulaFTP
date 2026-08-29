"""Fix only media placement/name errors that can be verified from filenames.

Dry-run by default.  With --apply, affected MongoDB documents are copied to a
timestamped backup collection before any update is made.
"""

import argparse
import json
import os
import re
import time
from pathlib import Path, PureWindowsPath

from dotenv import load_dotenv
from pymongo import MongoClient


USER_ROOT = f'/{os.getenv("NEBULA_LIBRARY_USER", "raphael")}'
FILMS_ROOT = f"{USER_ROOT}/Filmes"
SERIES_ROOT = f"{USER_ROOT}/Series"
ADULT_ROOT = f"{USER_ROOT}/Porno"
MISPLACED_ADULT_ROOT = f"{FILMS_ROOT}/Porno"
MANIFEST_PATH = Path(__file__).resolve().parents[2] / "media-move-manifest-20260808.json"


def windows_path_to_mongo(path: str) -> tuple[str, str]:
    parts = list(PureWindowsPath(path).parts)
    if not parts or not parts[0].endswith("\\"):
        raise ValueError(f"not an absolute Windows path: {path}")
    relative = parts[1:]
    if not relative:
        raise ValueError(f"path has no media name: {path}")
    name = relative[-1]
    parent = USER_ROOT + "/" + "/".join(relative[:-1])
    return parent, name


def load_episode_actions(files) -> list[dict]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8-sig"))
    actions = []
    for item in manifest:
        if item.get("Category") != "Episode":
            continue
        source_parent, source_name = windows_path_to_mongo(item["Source"])
        destination_parent, destination_name = windows_path_to_mongo(item["Destination"])
        docs = list(
            files.find(
                {"type": "file", "parent": source_parent, "name": source_name}
            )
        )
        if len(docs) != 1:
            raise RuntimeError(
                f"expected exactly one source for {source_parent}/{source_name}; "
                f"found {len(docs)}"
            )
        actions.append(
            {
                "doc": docs[0],
                "destination_parent": destination_parent,
                "destination_name": destination_name,
                "category": "episode",
            }
        )
    if len(actions) != 55:
        raise RuntimeError(f"expected 55 episode actions; found {len(actions)}")
    return actions


def load_adult_actions(files) -> list[dict]:
    source_docs = list(
        files.find(
            {
                "type": "file",
                "parent": {"$regex": rf"^{re.escape(MISPLACED_ADULT_ROOT)}(?:/|$)"},
            }
        )
    )
    actions = []
    for doc in source_docs:
        source_parent = doc["parent"]
        if source_parent == MISPLACED_ADULT_ROOT:
            folder = Path(doc["name"]).stem
            destination_parent = f"{ADULT_ROOT}/{folder}"
        else:
            relative_parent = source_parent[len(MISPLACED_ADULT_ROOT) :].lstrip("/")
            destination_parent = f"{ADULT_ROOT}/{relative_parent}"
        actions.append(
            {
                "doc": doc,
                "destination_parent": destination_parent,
                "destination_name": doc["name"],
                "category": "adult",
            }
        )
    if len(actions) != 45:
        raise RuntimeError(f"expected 45 misplaced adult files; found {len(actions)}")
    return actions


def validate_actions(files, actions: list[dict]) -> None:
    ids = [action["doc"]["_id"] for action in actions]
    if len(ids) != len(set(ids)):
        raise RuntimeError("the plan contains the same MongoDB document more than once")

    destinations = [
        (action["destination_parent"], action["destination_name"])
        for action in actions
    ]
    if len(destinations) != len(set(destinations)):
        raise RuntimeError("the plan contains duplicate destinations")

    collisions = []
    for parent, name in destinations:
        existing = files.find_one({"type": "file", "parent": parent, "name": name})
        if existing is not None:
            collisions.append(f"{parent}/{name}")
    if collisions:
        raise RuntimeError("destination collisions:\n" + "\n".join(collisions))


def ensure_directory(files, directory_path: str, now: int) -> None:
    if not directory_path.startswith(USER_ROOT + "/"):
        raise ValueError(f"directory escaped user root: {directory_path}")
    relative = directory_path[len(USER_ROOT) :].strip("/").split("/")
    parent = USER_ROOT
    for name in relative:
        files.update_one(
            {"type": "dir", "parent": parent, "name": name},
            {
                "$setOnInsert": {
                    "type": "dir",
                    "parent": parent,
                    "name": name,
                    "ctime": now,
                    "mtime": now,
                    "size": 0,
                }
            },
            upsert=True,
        )
        parent = f"{parent}/{name}"


def remove_empty_source_adult_dirs(files) -> int:
    removed = 0
    while True:
        candidates = list(
            files.find(
                {
                    "type": "dir",
                    "$or": [
                        {
                            "parent": {
                                "$regex": rf"^{re.escape(MISPLACED_ADULT_ROOT)}(?:/|$)"
                            }
                        },
                        {"parent": FILMS_ROOT, "name": "Porno"},
                    ],
                }
            )
        )
        deleted_this_pass = 0
        for doc in candidates:
            full_path = f"{doc['parent']}/{doc['name']}"
            if files.count_documents({"parent": full_path}) == 0:
                result = files.delete_one({"_id": doc["_id"]})
                deleted_this_pass += result.deleted_count
        removed += deleted_this_pass
        if deleted_this_pass == 0:
            return removed


def category_counts(files) -> dict[str, int]:
    return {
        category: files.count_documents(
            {
                "type": "file",
                "parent": {"$regex": rf"^{re.escape(USER_ROOT)}/{category}(?:/|$)"},
            }
        )
        for category in ("Filmes", "Series", "Porno")
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    client = MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    files = db.files

    actions = load_episode_actions(files) + load_adult_actions(files)
    validate_actions(files, actions)
    before_counts = category_counts(files)
    print(f"mode={'apply' if args.apply else 'dry-run'} actions={len(actions)}")
    print(f"episodes={sum(a['category'] == 'episode' for a in actions)}")
    print(f"adult={sum(a['category'] == 'adult' for a in actions)}")
    print(f"before={before_counts}")
    for action in actions[:8]:
        doc = action["doc"]
        print(
            f"{doc['parent']}/{doc['name']} -> "
            f"{action['destination_parent']}/{action['destination_name']}"
        )

    if not args.apply:
        print("dry-run complete; MongoDB was not changed")
        return

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_name = f"files_backup_verified_placement_{timestamp}"
    backup = db[backup_name]
    if backup.estimated_document_count() != 0:
        raise RuntimeError(f"backup collection is not empty: {backup_name}")

    affected_ids = [action["doc"]["_id"] for action in actions]
    backup_docs = list(files.find({"_id": {"$in": affected_ids}}))
    backup_dirs = list(
        files.find(
            {
                "type": "dir",
                "$or": [
                    {
                        "parent": {
                            "$regex": rf"^{re.escape(MISPLACED_ADULT_ROOT)}(?:/|$)"
                        }
                    },
                    {"parent": FILMS_ROOT, "name": "Porno"},
                ],
            }
        )
    )
    backup.insert_many(backup_docs + backup_dirs)
    backup.create_index([("parent", 1), ("name", 1)])

    now = int(time.time())
    for action in actions:
        ensure_directory(files, action["destination_parent"], now)
        result = files.update_one(
            {"_id": action["doc"]["_id"]},
            {
                "$set": {
                    "parent": action["destination_parent"],
                    "name": action["destination_name"],
                    "mtime": now,
                }
            },
        )
        if result.matched_count != 1:
            raise RuntimeError(f"failed to update {action['doc']['_id']}")

    removed_dirs = remove_empty_source_adult_dirs(files)

    remaining_sources = sum(
        files.count_documents(
            {
                "_id": action["doc"]["_id"],
                "parent": action["doc"]["parent"],
                "name": action["doc"]["name"],
            }
        )
        for action in actions
    )
    missing_destinations = sum(
        files.count_documents(
            {
                "_id": action["doc"]["_id"],
                "parent": action["destination_parent"],
                "name": action["destination_name"],
            }
        )
        != 1
        for action in actions
    )
    after_counts = category_counts(files)
    expected_counts = {
        "Filmes": before_counts["Filmes"] - 100,
        "Series": before_counts["Series"] + 55,
        "Porno": before_counts["Porno"] + 45,
    }
    if remaining_sources or missing_destinations or after_counts != expected_counts:
        raise RuntimeError(
            "post-apply verification failed: "
            f"remaining_sources={remaining_sources} "
            f"missing_destinations={missing_destinations} "
            f"after={after_counts} expected={expected_counts}"
        )

    print(f"backup_collection={backup_name} backup_docs={backup.count_documents({})}")
    print(f"removed_empty_source_dirs={removed_dirs}")
    print(f"after={after_counts}")
    print("applied and verified")


if __name__ == "__main__":
    main()
