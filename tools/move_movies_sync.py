import os
import shutil
import pymongo

def run():
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    filmes_dir = 'N:/Filmes'
    films_root = f'/{os.getenv("NEBULA_LIBRARY_USER", "raphael")}/Filmes'

    mismatches = []
    if os.path.exists(filmes_dir):
        for folder in os.listdir(filmes_dir):
            fpath = os.path.join(filmes_dir, folder)
            if os.path.isdir(fpath):
                for file in os.listdir(fpath):
                    if file.endswith(('.mkv', '.mp4', '.avi', '.mov', '.m4v')):
                        stem = os.path.splitext(file)[0]
                        # Ignore standard suffix like "- 720p" or "- 1080p"
                        if stem != folder and not stem.startswith(folder):
                            mismatches.append((folder, file, stem))

    print(f"Found {len(mismatches)} files to move:")
    for src_folder, file_name, target_folder_name in mismatches:
        src_path = os.path.join(filmes_dir, src_folder, file_name)
        target_dir = os.path.join(filmes_dir, target_folder_name)
        target_path = os.path.join(target_dir, file_name)

        if os.path.exists(src_path):
            os.makedirs(target_dir, exist_ok=True)
            shutil.move(src_path, target_path)
            print(f"Moved: {file_name} -> {target_folder_name}")
        else:
            print(f"Source file not found (already moved?): {src_path}")

        # Ensure target folder doc in Mongo
        db.files.update_one(
            {'name': target_folder_name, 'parent': films_root},
            {'$setOnInsert': {'type': 'dir', 'size': 0}},
            upsert=True
        )
        # Update file doc in Mongo
        db.files.update_many(
            {'name': file_name},
            {'$set': {'parent': f"{films_root}/{target_folder_name}"}}
        )

        # Remove source directory if empty
        src_dir = os.path.join(filmes_dir, src_folder)
        if os.path.exists(src_dir) and len(os.listdir(src_dir)) == 0:
            os.rmdir(src_dir)
            print(f"Removed empty source directory: {src_folder}")
            db.files.delete_many({'name': src_folder, 'parent': films_root})

if __name__ == '__main__':
    run()
