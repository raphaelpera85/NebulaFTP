import os
import sys
import json
import pymongo

sys.stdout.reconfigure(encoding='utf-8')

CHUNK_SIZE = 67108864 # 64 MB

def purge_incomplete_streams():
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    user_root = f'/{os.getenv("NEBULA_LIBRARY_USER", "raphael")}'
    files_col = db.files

    docs = list(files_col.find({"type": "file"}))
    print(f"Total file documents in MongoDB: {len(docs)}")

    to_delete = []

    for d in docs:
        parent = d.get("parent", "")
        name = d.get("name", "")
        size = d.get("size", 0)
        parts = d.get("parts", [])
        full_path = f"{parent}/{name}"

        # Rule 1: No parts or dummy parts
        if not parts:
            to_delete.append((d, "no_parts"))
            continue

        for p in parts:
            if p.get("tg_file") == "restored" or p.get("tg_message") == 1:
                to_delete.append((d, "dummy_parts"))
                break
        else:
            # Rule 2: Discontinuous part IDs
            part_ids = [p.get("part_id", 0) for p in parts]
            expected_ids = list(range(len(parts)))
            if part_ids != expected_ids:
                to_delete.append((d, f"discontinuous_part_ids_{part_ids}"))
                continue

            # Rule 3: Single-chunk 64MB movie in /Filmes
            if parent.startswith(f"{user_root}/Filmes"):
                if len(parts) == 1 and parts[0].get("file_size") == CHUNK_SIZE:
                    to_delete.append((d, "single_chunk_movie_64mb"))
                    continue
                # Cut-off upload (last part is full 64MB and size < 400MB)
                elif len(parts) > 0 and parts[-1].get("file_size") == CHUNK_SIZE and size < 400 * 1024 * 1024:
                    to_delete.append((d, f"cut_off_movie_upload_size_{size / (1024*1024):.1f}MB"))
                    continue

    print(f"\nTotal incomplete / corrupt file records to purge: {len(to_delete)}")
    
    deleted_files = 0
    deleted_empty_dirs = 0
    deleted_paths = set()

    for doc, reason in to_delete:
        parent = doc.get("parent", "")
        name = doc.get("name", "")
        full_path = f"{parent}/{name}"
        
        files_col.delete_one({"_id": doc["_id"]})
        deleted_files += 1
        deleted_paths.add(full_path)
        
        # Check if parent dir is now empty
        dir_name = parent.split("/")[-1]
        dir_parent = "/".join(parent.split("/")[:-1])
        
        remaining = files_col.count_documents({"parent": parent})
        if remaining == 0:
            files_col.delete_many({"name": dir_name, "parent": dir_parent})
            deleted_empty_dirs += 1

    print(f"Purged {deleted_files} broken/incomplete file documents.")
    print(f"Cleaned up {deleted_empty_dirs} empty parent directories.")

    # Clean feed_ftp_state.json
    if os.path.exists("feed_ftp_state.json"):
        try:
            with open("feed_ftp_state.json", "r", encoding="utf-8") as f:
                state_data = json.load(f)
            
            if isinstance(state_data, list):
                orig_len = len(state_data)
                state_data = [item for item in state_data if not any(p.lower() in str(item).lower() for p in deleted_paths)]
                with open("feed_ftp_state.json", "w", encoding="utf-8") as f:
                    json.dump(state_data, f, ensure_ascii=False, indent=2)
                print(f"Cleaned feed_ftp_state.json (removed {orig_len - len(state_data)} items)")
        except Exception as e:
            print(f"Error updating feed_ftp_state.json: {e}")

    print("\nPurge complete!")

if __name__ == "__main__":
    purge_incomplete_streams()
