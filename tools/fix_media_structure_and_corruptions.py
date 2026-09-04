import os
import sys
import json
import pymongo
import re

sys.stdout.reconfigure(encoding='utf-8')

def fix_all():
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    user_root = f'/{os.getenv("NEBULA_LIBRARY_USER", "raphael")}'
    files_col = db.files

    print("=== STEP 1: Fixing corrupt #Alive (2020) movie entry in MongoDB ===")
    alive_docs = list(files_col.find({"name": {"$regex": "alive", "$options": "i"}}))
    deleted_alive = 0
    for d in alive_docs:
        # Check if parent or name is #Alive
        if "#alive" in d.get("name", "").lower() or "#alive" in d.get("parent", "").lower():
            files_col.delete_one({"_id": d["_id"]})
            deleted_alive += 1
            print(f"  Deleted corrupt doc: {d.get('parent')}/{d.get('name')}")
            
    # Also delete child docs under parent containing #Alive
    alive_children = list(files_col.find({"parent": {"$regex": "alive", "$options": "i"}}))
    for d in alive_children:
        files_col.delete_one({"_id": d["_id"]})
        deleted_alive += 1
        print(f"  Deleted corrupt child doc: {d.get('parent')}/{d.get('name')}")

    print(f"Total deleted corrupt #Alive documents: {deleted_alive}")

    # Remove #Alive references from feed_ftp_state.json and materialized links if present
    for state_file in ["feed_ftp_state.json", "feed_ftp_state_materialized_links.json"]:
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state_data = json.load(f)
                
                modified = False
                if isinstance(state_data, list):
                    new_list = [item for item in state_data if "alive" not in str(item).lower()]
                    if len(new_list) != len(state_data):
                        state_data = new_list
                        modified = True
                elif isinstance(state_data, dict):
                    new_dict = {k: v for k, v in state_data.items() if "alive" not in k.lower() and "alive" not in str(v).lower()}
                    if len(new_dict) != len(state_data):
                        state_data = new_dict
                        modified = True
                        
                if modified:
                    with open(state_file, "w", encoding="utf-8") as f:
                        json.dump(state_data, f, ensure_ascii=False, indent=2)
                    print(f"  Cleaned #Alive from {state_file}")
            except Exception as e:
                print(f"  Error cleaning {state_file}: {e}")

    print("\n=== STEP 2: Reorganizing loose files in /Porno into proper subfolders ===")
    loose_porno_files = list(files_col.find({
        "parent": f"{user_root}/Porno",
        "type": "file"
    }))
    print(f"Found {len(loose_porno_files)} loose files in {user_root}/Porno")

    reorganized_porno = 0
    for doc in loose_porno_files:
        file_name = doc.get("name", "")
        stem = os.path.splitext(file_name)[0]
        # Clean folder name
        folder_name = re.sub(r'[<>:"/\\|?*]', '_', stem).strip()
        
        target_parent = f"{user_root}/Porno/{folder_name}"
        
        # Ensure dir doc exists in MongoDB
        files_col.update_one(
            {"name": folder_name, "parent": f"{user_root}/Porno"},
            {"$setOnInsert": {"type": "dir", "size": 0}},
            upsert=True
        )

        # Update file doc parent
        files_col.update_one(
            {"_id": doc["_id"]},
            {"$set": {"parent": target_parent}}
        )
        reorganized_porno += 1

    print(f"Reorganized {reorganized_porno} adult files into dedicated subfolders.")

    print("\n=== STEP 3: Cleaning up orphaned/empty directory documents in Mongo ===")
    # Delete directory documents directly under user_root except Filmes, Series, Porno
    deleted_root = files_col.delete_many({"parent": user_root, "name": {"$nin": ["Filmes", "Series", "Porno"]}})
    print(f"Deleted misplaced root dirs in MongoDB: {deleted_root.deleted_count}")

    print("\nDone!")

if __name__ == "__main__":
    fix_all()
