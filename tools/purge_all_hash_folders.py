import os
import sys
import json
import pymongo
import re

sys.stdout.reconfigure(encoding='utf-8')

HEX_HASH = re.compile(r"^[0-9a-fA-F]{24}$")

def purge_hash_folders():
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    user_root = f'/{os.getenv("NEBULA_LIBRARY_USER", "raphael")}'
    files_col = db.files

    # Find all directory docs whose name is a 24-char hex hash
    dirs = list(files_col.find({"type": "dir"}))
    hash_dirs = [d for d in dirs if HEX_HASH.match(d["name"])]
    print(f"Total 24-char hex hash folders found in MongoDB: {len(hash_dirs)}")

    deleted_dirs = 0
    deleted_files = 0
    deleted_paths = set()

    for dir_doc in hash_dirs:
        dir_name = dir_doc["name"]
        dir_parent = dir_doc["parent"]
        dir_full_path = f"{dir_parent}/{dir_name}"

        # Delete all files inside this hash directory
        children = list(files_col.find({"parent": dir_full_path}))
        for c in children:
            files_col.delete_one({"_id": c["_id"]})
            deleted_files += 1
            deleted_paths.add(f"{dir_full_path}/{c['name']}")

        # Delete the directory doc
        files_col.delete_one({"_id": dir_doc["_id"]})
        deleted_dirs += 1
        deleted_paths.add(dir_full_path)
        print(f"  [PURGED HASH FOLDER] {dir_full_path} ({len(children)} files)")

    # Also delete any file doc whose name is a 24-char hex hash .mkv/.mp4
    files = list(files_col.find({"type": "file"}))
    for f in files:
        stem = os.path.splitext(f["name"])[0]
        if HEX_HASH.match(stem):
            files_col.delete_one({"_id": f["_id"]})
            deleted_files += 1
            print(f"  [PURGED HASH FILE] {f['parent']}/{f['name']}")

    print(f"\nPurged {deleted_dirs} 24-char hex hash folders and {deleted_files} child files from MongoDB.")

    # Clean feed_ftp_state.json and feed_ftp_state_materialized_links.json
    for state_file in ["feed_ftp_state.json", "feed_ftp_state_materialized_links.json"]:
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state_data = json.load(f)

                if isinstance(state_data, list):
                    orig_len = len(state_data)
                    state_data = [item for item in state_data if not (HEX_HASH.search(str(item)) or any(p in str(item) for p in deleted_paths))]
                    with open(state_file, "w", encoding="utf-8") as f:
                        json.dump(state_data, f, ensure_ascii=False, indent=2)
                    print(f"Cleaned {state_file} (removed {orig_len - len(state_data)} items)")
                elif isinstance(state_data, dict):
                    orig_len = len(state_data)
                    state_data = {k: v for k, v in state_data.items() if not (HEX_HASH.search(k) or HEX_HASH.search(str(v)))}
                    with open(state_file, "w", encoding="utf-8") as f:
                        json.dump(state_data, f, ensure_ascii=False, indent=2)
                    print(f"Cleaned {state_file} (removed {orig_len - len(state_data)} items)")
            except Exception as e:
                print(f"Error updating {state_file}: {e}")

    remaining_dirs = files_col.count_documents({"parent": f"{user_root}/Filmes", "type": "dir"})
    remaining_files = files_col.count_documents({"type": "file"})
    print(f"\nFINAL STATE: {remaining_dirs} clean named movie folders in /Filmes, {remaining_files} total files in DB.")

if __name__ == "__main__":
    purge_hash_folders()
