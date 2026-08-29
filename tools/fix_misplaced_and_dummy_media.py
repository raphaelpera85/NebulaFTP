import os
import sys
import json
import pymongo
from pymongo.errors import DuplicateKeyError
import re

sys.stdout.reconfigure(encoding='utf-8')

def fix_misplaced_and_dummy():
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    user_root = f'/{os.getenv("NEBULA_LIBRARY_USER", "raphael")}'
    files_col = db.files

    print("=== STEP 1: Moving misplaced adult content from /Filmes to /Porno ===")
    porno_words = re.compile(r"(?i)\b(porno|porn|xxx|hentai|adulto|brazzers|bangbros|naughty|stepbro|stepsis|stepmom|creampie|pussy|cock)\b")
    
    filmes_docs = list(files_col.find({"parent": f"{user_root}/Filmes"}))
    moved_folders = 0
    moved_files = 0
    
    for d in filmes_docs:
        name = d["name"]
        dtype = d.get("type", "dir")
        
        if name == "Porno" or porno_words.search(name):
            if dtype == "dir":
                old_parent = f"{user_root}/Filmes/{name}"
                new_parent = f"{user_root}/Porno/{name}"
                
                try:
                    files_col.update_one(
                        {"_id": d["_id"]},
                        {"$set": {"parent": f"{user_root}/Porno"}}
                    )
                    moved_folders += 1
                except DuplicateKeyError:
                    files_col.delete_one({"_id": d["_id"]})
                
                # Move all children individually with try/except
                children = list(files_col.find({"parent": old_parent}))
                for c in children:
                    try:
                        files_col.update_one(
                            {"_id": c["_id"]},
                            {"$set": {"parent": new_parent}}
                        )
                        moved_files += 1
                    except DuplicateKeyError:
                        files_col.delete_one({"_id": c["_id"]})
                print(f"  Moved folder '{name}' from Filmes to Porno ({len(children)} children)")
            elif dtype == "file":
                stem = os.path.splitext(name)[0]
                target_parent = f"{user_root}/Porno/{stem}"
                
                files_col.update_one(
                    {"name": stem, "parent": f"{user_root}/Porno"},
                    {"$setOnInsert": {"type": "dir", "size": 0}},
                    upsert=True
                )
                try:
                    files_col.update_one(
                        {"_id": d["_id"]},
                        {"$set": {"parent": target_parent}}
                    )
                    moved_files += 1
                    print(f"  Moved loose file '{name}' from Filmes to Porno/{stem}")
                except DuplicateKeyError:
                    files_col.delete_one({"_id": d["_id"]})

    # Also check files inside /Filmes/Porno sub-subfolder
    sub_porno_children = list(files_col.find({"parent": f"{user_root}/Filmes/Porno"}))
    for c in sub_porno_children:
        c_name = c["name"]
        if c.get("type") == "file":
            stem = os.path.splitext(c_name)[0]
            target_parent = f"{user_root}/Porno/{stem}"
            files_col.update_one(
                {"name": stem, "parent": f"{user_root}/Porno"},
                {"$setOnInsert": {"type": "dir", "size": 0}},
                upsert=True
            )
            try:
                files_col.update_one(
                    {"_id": c["_id"]},
                    {"$set": {"parent": target_parent}}
                )
                moved_files += 1
                print(f"  Moved nested file '{c_name}' from Filmes/Porno to Porno/{stem}")
            except DuplicateKeyError:
                files_col.delete_one({"_id": c["_id"]})

    files_col.delete_one({"name": "Porno", "parent": f"{user_root}/Filmes"})

    print(f"Total adult folders moved: {moved_folders}, files moved: {moved_files}")

    print("\n=== STEP 2: Removing fake/dummy 'restored' documents from MongoDB ===")
    dummy_docs = list(files_col.find({
        "type": "file",
        "$or": [
            {"parts": {"$size": 0}},
            {"parts.0.tg_file": "restored"},
            {"parts.0.tg_message": 1}
        ]
    }))
    
    deleted_dummies = 0
    deleted_empty_dirs = 0
    
    for doc in dummy_docs:
        parent = doc.get("parent", "")
        name = doc.get("name", "")
        
        files_col.delete_one({"_id": doc["_id"]})
        deleted_dummies += 1
        
        dir_name = parent.split("/")[-1]
        dir_parent = "/".join(parent.split("/")[:-1])
        
        remaining_in_dir = files_col.count_documents({"parent": parent})
        if remaining_in_dir == 0:
            files_col.delete_many({"name": dir_name, "parent": dir_parent})
            deleted_empty_dirs += 1

    print(f"Deleted {deleted_dummies} dummy file records and {deleted_empty_dirs} empty parent directories from MongoDB.")

    print("\n=== STEP 3: Cleanup completed ===")

if __name__ == "__main__":
    fix_misplaced_and_dummy()
