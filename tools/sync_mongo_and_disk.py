import os
import re

import pymongo

def run():
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    user_root = f'/{os.getenv("NEBULA_LIBRARY_USER", "raphael")}'
    films_root = f'{user_root}/Filmes'

    print("Step 1: Fixing movie file documents in MongoDB...")

    # Find files whose parent is /raphael or starts with /raphael/Filmes or has parent under /raphael (not Series)
    movie_files = list(db.files.find({
        'type': 'file',
        'parent': {'$regex': rf'^{re.escape(user_root)}(/Filmes)?($|/)'}
    }))
    print(f"Total movie files found: {len(movie_files)}")

    updated_parents = 0
    for f in movie_files:
        parent = f.get('parent', '')
        name = f.get('name', '')
        if not name.endswith(('.mkv', '.mp4', '.avi', '.mov', '.m4v')):
            continue

        stem = os.path.splitext(name)[0]
        # Movie files belong in /raphael/Filmes/<stem>
        target_parent = f"{films_root}/{stem}"

        # Ensure directory document exists in Mongo
        db.files.update_one(
            {'name': stem, 'parent': films_root},
            {'$setOnInsert': {'type': 'dir', 'size': 0}},
            upsert=True
        )

        if parent != target_parent:
            # Delete any duplicate existing record at target_parent to avoid index collision
            db.files.delete_many({'parent': target_parent, 'name': name, '_id': {'$ne': f['_id']}})
            try:
                db.files.update_one(
                    {'_id': f['_id']},
                    {'$set': {'parent': target_parent}}
                )
                updated_parents += 1
            except Exception as e:
                print(f"Error updating {name}: {e}")

    print(f"Updated {updated_parents} movie file parent paths in MongoDB.")

    print("\nStep 2: Cleaning up misplaced directory documents in Mongo...")
    # Delete directory documents under /raphael directly (except Filmes and Series)
    db.files.delete_many({'parent': user_root, 'name': {'$nin': ['Filmes', 'Series']}})

    # Delete directory documents whose name has 'Filmes/'
    db.files.delete_many({'parent': user_root, 'name': {'$regex': '^Filmes/'}})

    print("\nStep 3: Cleaning up empty folders on N:/ root...")
    if os.path.exists("N:/"):
        root_items = [e.name for e in os.scandir("N:/") if e.is_dir() and e.name not in ["Filmes", "Series"]]
        deleted = 0
        for folder in root_items:
            path = os.path.join("N:/", folder)
            try:
                if len(os.listdir(path)) == 0:
                    os.rmdir(path)
                    deleted += 1
            except Exception as exc:
                print(f"Could not delete N:/{folder}: {exc}")
        print(f"Deleted {deleted} empty folders from N:/ root.")

    print("\nDone!")

if __name__ == '__main__':
    run()
