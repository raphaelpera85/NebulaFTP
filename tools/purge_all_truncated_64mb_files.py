import os
import sys
import json
import pymongo

sys.stdout.reconfigure(encoding='utf-8')

CHUNK_SIZE = 67108864 # 64 MB

def purge_truncated_files():
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    user_root = f'/{os.getenv("NEBULA_LIBRARY_USER", "raphael")}'
    files_col = db.files

    docs = list(files_col.find({"type": "file"}))
    print(f"Initial total file documents in MongoDB: {len(docs)}")

    to_delete = []

    for d in docs:
        parent = d.get("parent", "")
        name = d.get("name", "")
        size = d.get("size", 0)
        parts = d.get("parts", [])
        full_path = f"{parent}/{name}"

        if not parts:
            to_delete.append((d, "no_parts"))
            continue

        last_part = parts[-1]
        last_part_size = last_part.get("file_size", 0)

        # If the last chunk is EXACTLY 64 MB, the upload was cut off before finishing!
        if last_part_size == CHUNK_SIZE:
            to_delete.append((d, f"truncated_last_chunk_64mb (parts={len(parts)}, size={size / (1024*1024):.1f}MB)"))

    print(f"\nTotal truncated / cut-off files identified for deletion: {len(to_delete)}")

    deleted_files = 0
    deleted_paths = set()

    for doc, reason in to_delete:
        parent = doc.get("parent", "")
        name = doc.get("name", "")
        full_path = f"{parent}/{name}"

        files_col.delete_one({"_id": doc["_id"]})
        deleted_files += 1
        deleted_paths.add(full_path)
        print(f"  [PURGED TRUNCATED] [{reason}] {full_path}")

    # Recursive directory cleanup (remove empty folders)
    deleted_dirs = 0
    while True:
        dirs = list(files_col.find({"type": "dir"}))
        cleaned_in_pass = 0
        for d in dirs:
            parent = d.get("parent", "")
            name = d.get("name", "")
            dir_path = f"{parent}/{name}"

            if parent == user_root and name in ["Filmes", "Series", "Porno"]:
                continue

            children_count = files_col.count_documents({"parent": dir_path})
            if children_count == 0:
                files_col.delete_one({"_id": d["_id"]})
                cleaned_in_pass += 1
                deleted_dirs += 1

        if cleaned_in_pass == 0:
            break

    print(f"\nPurged {deleted_files} truncated files.")
    print(f"Cleaned up {deleted_dirs} empty directories.")

    # Clean state files
    for state_file in ["feed_ftp_state.json", "feed_ftp_state_materialized_links.json"]:
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state_data = json.load(f)

                if isinstance(state_data, list):
                    orig_len = len(state_data)
                    state_data = [item for item in state_data if not any(p.lower() in str(item).lower() for p in deleted_paths)]
                    with open(state_file, "w", encoding="utf-8") as f:
                        json.dump(state_data, f, ensure_ascii=False, indent=2)
                    print(f"Cleaned {state_file} (removed {orig_len - len(state_data)} items)")
                elif isinstance(state_data, dict):
                    orig_len = len(state_data)
                    state_data = {k: v for k, v in state_data.items() if not any(p.lower() in k.lower() or p.lower() in str(v).lower() for p in deleted_paths)}
                    with open(state_file, "w", encoding="utf-8") as f:
                        json.dump(state_data, f, ensure_ascii=False, indent=2)
                    print(f"Cleaned {state_file} (removed {orig_len - len(state_data)} items)")
            except Exception as e:
                print(f"Error updating {state_file}: {e}")

    remaining_files = files_col.count_documents({"type": "file"})
    remaining_dirs = files_col.count_documents({"type": "dir"})
    print(f"\nFINAL DATABASE STATE: {remaining_files} 100% complete files, {remaining_dirs} directories.")

if __name__ == "__main__":
    purge_truncated_files()
