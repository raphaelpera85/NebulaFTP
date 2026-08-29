import os
import sys
import json
import pymongo
import re

sys.stdout.reconfigure(encoding='utf-8')

ROOT = f'/{os.getenv("NEBULA_LIBRARY_USER", "raphael")}/Filmes'

def fix_all_inner_filenames(apply=False):
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    files_col = db.files

    dirs = list(files_col.find({"parent": ROOT, "type": "dir"}))
    renamed_files = 0

    for d in dirs:
        folder_name = d["name"]
        children = list(files_col.find({"parent": f"{ROOT}/{folder_name}"}))
        video_children = [
            c for c in children
            if os.path.splitext(c["name"])[1].lower() in {".avi", ".m4v", ".mkv", ".mp4", ".mov", ".ts", ".wmv"}
        ]

        if len(video_children) == 1:
            child = video_children[0]
            ext = os.path.splitext(child["name"])[1].lower()
            expected_name = f"{folder_name}{ext}"
            if child["name"] != expected_name:
                print(f"  Fixing inner file: '{child['name']}' -> '{expected_name}' (folder: '{folder_name}')")
                if apply:
                    files_col.update_one(
                        {"_id": child["_id"]},
                        {"$set": {"name": expected_name}}
                    )
                renamed_files += 1

    print(f"\nTotal inner filenames matched to folder name: {renamed_files} (apply={apply})")

if __name__ == "__main__":
    apply_flag = "--apply" in sys.argv
    fix_all_inner_filenames(apply=apply_flag)
