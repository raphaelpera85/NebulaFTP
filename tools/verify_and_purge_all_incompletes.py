import os
import sys
import json
import pymongo

sys.stdout.reconfigure(encoding='utf-8')

CHUNK_SIZE = 67108864 # 64 MB (64 * 1024 * 1024)

def purge_all_incomplete_media():
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
        status = d.get("status", "")
        full_path = f"{parent}/{name}"

        # Rule 1: Must be status completed
        if status != "completed":
            to_delete.append((d, f"status_{status}"))
            continue

        # Rule 2: Must have parts list
        if not parts:
            to_delete.append((d, "no_parts"))
            continue

        # Rule 3: No dummy parts
        has_dummy = False
        for p in parts:
            if p.get("tg_file") == "restored" or p.get("tg_message") == 1:
                has_dummy = True
                break
        if has_dummy:
            to_delete.append((d, "dummy_parts"))
            continue

        # Rule 4: Part IDs must be strictly 0..N-1 continuous
        part_ids = [p.get("part_id", 0) for p in parts]
        expected_ids = list(range(len(parts)))
        if part_ids != expected_ids:
            to_delete.append((d, f"discontinuous_part_ids_{part_ids}"))
            continue

        # Rule 5: Chunk size validation
        # All parts except last must be exactly CHUNK_SIZE
        chunk_error = False
        for idx, p in enumerate(parts[:-1]):
            if p.get("file_size", 0) != CHUNK_SIZE:
                chunk_error = True
                break
        if chunk_error:
            to_delete.append((d, "intermediate_chunk_size_mismatch"))
            continue

        # Rule 6: Movies under 300MB or single 64MB part in /Filmes
        if parent.startswith(f"{user_root}/Filmes"):
            if len(parts) == 1 and parts[0].get("file_size") == CHUNK_SIZE:
                to_delete.append((d, "single_chunk_movie_64mb"))
                continue
            if size < 300 * 1024 * 1024 and not name.lower().endswith(('.srt', '.sub', '.ass', '.vtt', '.txt')):
                # Movie video file under 300MB is incomplete
                to_delete.append((d, f"movie_video_size_too_small_{size / (1024*1024):.1f}MB"))
                continue

    print(f"\nTotal incomplete/invalid files identified for deletion: {len(to_delete)}")
    
    deleted_files = 0
    deleted_paths = set()

    for doc, reason in to_delete:
        parent = doc.get("parent", "")
        name = doc.get("name", "")
        full_path = f"{parent}/{name}"
        
        files_col.delete_one({"_id": doc["_id"]})
        deleted_files += 1
        deleted_paths.add(full_path)
        print(f"  [PURGED] [{reason}] {full_path}")

    # Recursive directory cleanup (remove all empty directories)
    deleted_dirs = 0
    while True:
        dirs = list(files_col.find({"type": "dir"}))
        cleaned_in_pass = 0
        for d in dirs:
            parent = d.get("parent", "")
            name = d.get("name", "")
            dir_path = f"{parent}/{name}"
            
            # Don't delete root categories (/raphael/Filmes, /raphael/Series, /raphael/Porno)
            if parent == user_root and name in ["Filmes", "Series", "Porno"]:
                continue
                
            children_count = files_col.count_documents({"parent": dir_path})
            if children_count == 0:
                files_col.delete_one({"_id": d["_id"]})
                cleaned_in_pass += 1
                deleted_dirs += 1
                
        if cleaned_in_pass == 0:
            break

    print(f"\nPurged {deleted_files} incomplete files.")
    print(f"Cleaned up {deleted_dirs} empty directories.")

    remaining_files = files_col.count_documents({"type": "file"})
    remaining_dirs = files_col.count_documents({"type": "dir"})
    print(f"FINAL DATABASE STATE: {remaining_files} complete files, {remaining_dirs} directories.")

if __name__ == "__main__":
    purge_all_incomplete_media()
