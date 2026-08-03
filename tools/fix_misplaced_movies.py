import os
import shutil

import pymongo

def fix_movies():
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]

    filmes_dir = "N:/Filmes"
    user_root = f'/{os.getenv("NEBULA_LIBRARY_USER", "raphael")}'
    films_root = f"{user_root}/Filmes"

    print("=== STEP 1: Moving misplaced video files in N:/Filmes to correct movie folders ===")
    moved_count = 0
    if os.path.exists(filmes_dir):
        for folder in os.listdir(filmes_dir):
            fpath = os.path.join(filmes_dir, folder)
            if not os.path.isdir(fpath):
                continue
            for file in os.listdir(fpath):
                if not file.endswith(('.mkv', '.mp4', '.avi', '.mov', '.m4v')):
                    continue
                stem = os.path.splitext(file)[0]
                if stem != folder:
                    src_file = os.path.join(fpath, file)
                    target_folder_name = stem
                    target_dir = os.path.join(filmes_dir, target_folder_name)
                    target_file = os.path.join(target_dir, file)

                    print(f"Moving file:\n  FROM: {src_file}\n    TO: {target_file}")
                    os.makedirs(target_dir, exist_ok=True)
                    shutil.move(src_file, target_file)
                    moved_count += 1

                    # Ensure target directory doc in MongoDB
                    db.files.update_one(
                        {"name": target_folder_name, "parent": films_root},
                        {"$setOnInsert": {"type": "dir", "size": 0}},
                        upsert=True
                    )

                    # Update file doc in MongoDB
                    db.files.update_many(
                        {"name": file},
                        {"$set": {"parent": f"{films_root}/{target_folder_name}"}}
                    )

                    # Remove old empty folder if it became empty
                    if os.path.exists(fpath) and len(os.listdir(fpath)) == 0:
                        os.rmdir(fpath)
                        print(f"Removed empty source folder: {fpath}")
                        db.files.delete_many({"name": folder, "parent": films_root})

    print(f"Moved {moved_count} video files to correct folders and updated MongoDB.")

    print("\n=== STEP 2: Cleaning up empty misplaced folders on N:/ root ===")
    root_items = [e.name for e in os.scandir("N:/") if e.is_dir() and e.name not in ["Filmes", "Series"]]
    deleted_folders = 0

    for folder in root_items:
        root_folder_path = os.path.join("N:/", folder)
        contents = os.listdir(root_folder_path)
        if len(contents) == 0:
            os.rmdir(root_folder_path)
            print(f"Deleted empty root folder on N:/: {folder}")
            deleted_folders += 1
        else:
            print(f"WARNING: Root folder {folder} is NOT empty: {contents}")

        # Clean up MongoDB documents that point to /raphael directly or have corrupt names
        db.files.delete_many({"parent": user_root, "name": folder})
        db.files.delete_many({"parent": user_root, "name": f"Filmes/{folder}"})

    print(f"Deleted {deleted_folders} empty root folders from N:/ and cleaned up MongoDB entries.")

    print("\n=== STEP 3: Verifying final state ===")
    remaining_root = [e.name for e in os.scandir("N:/") if e.is_dir() and e.name not in ["Filmes", "Series"]]
    print(f"Remaining misplaced folders on N:/: {len(remaining_root)}")
    bad_mongo = list(db.files.find({"parent": user_root, "name": {"$nin": ["Filmes", "Series"]}}))
    print(f"Remaining misplaced docs in Mongo under /raphael: {len(bad_mongo)}")

if __name__ == "__main__":
    fix_movies()
