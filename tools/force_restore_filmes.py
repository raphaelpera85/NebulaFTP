import json
import os
import sys
import time

from dotenv import load_dotenv
import pymongo

load_dotenv()


def get_mongo_parent_and_dirs(orig_path, user_root="/raphael"):
    norm = orig_path.replace("\\", "/")
    p_lower = norm.lower()

    if "/filmes/" in p_lower:
        idx = p_lower.find("/filmes/")
        rel_path = norm[idx + len("/filmes/") :]
        category = "Filmes"
    elif "/series/" in p_lower or "/séries/" in p_lower:
        idx = p_lower.find("/series/") if "/series/" in p_lower else p_lower.find("/séries/")
        sep_len = len("/series/") if "/series/" in p_lower else len("/séries/")
        rel_path = norm[idx + sep_len :]
        category = "Series"
    else:
        rel_path = os.path.basename(norm)
        category = "Filmes"

    parts = [p for p in rel_path.split("/") if p]
    if not parts:
        return f"{user_root}/{category}", []

    file_name = parts[-1]
    dir_parts = parts[:-1]

    dirs_to_create = []
    current_parent = f"{user_root}/{category}"

    if not dir_parts:
        stem = os.path.splitext(file_name)[0]
        dirs_to_create.append((stem, current_parent))
        current_parent = f"{current_parent}/{stem}"
    else:
        for d in dir_parts:
            dirs_to_create.append((d, current_parent))
            current_parent = f"{current_parent}/{d}"

    return current_parent, dirs_to_create


def run():
    mongo_uri = os.getenv("MONGODB", "mongodb://localhost:27017")
    db_name = os.getenv("MONGO_DATABASE", "ftp")

    client = pymongo.MongoClient(mongo_uri)
    db = client[db_name]

    state_path = "feed_ftp_state.json"
    if not os.path.exists(state_path):
        print(f"[ERRO] {state_path} não encontrado!")
        return

    with open(state_path, "r", encoding="utf-8") as f:
        state_items = json.load(f)

    media_items = [x for x in state_items if not x.endswith(".strm")]

    now = int(time.time())
    user_root = "/raphael"

    # Ensure root category directories
    db.files.update_one({"name": "Filmes", "parent": user_root}, {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}}, upsert=True)
    db.files.update_one({"name": "Series", "parent": user_root}, {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}}, upsert=True)

    restored_count = 0
    for path in media_items:
        file_name = os.path.basename(path)
        parent_dir, dirs_to_create = get_mongo_parent_and_dirs(path, user_root)

        for d_name, d_parent in dirs_to_create:
            db.files.update_one(
                {"name": d_name, "parent": d_parent},
                {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}},
                upsert=True,
            )

        file_doc = {
            "type": "file",
            "name": file_name,
            "parent": parent_dir,
            "size": 1000000000,
            "status": "completed",
            "mtime": now,
            "ctime": now,
            "obfuscated_id": "restored_" + os.path.splitext(file_name)[0],
            "uploaded_at": now,
            "parts": [{"part_id": 0, "tg_file": "restored", "tg_message": 1, "file_size": 1000000000}],
        }
        db.files.replace_one({"name": file_name, "parent": parent_dir}, file_doc, upsert=True)
        restored_count += 1

    print(f"\n[SUCESSO] {restored_count} mídias (incluindo Porno, Filmes e Séries) restauradas no MongoDB (Drive N:)!")


if __name__ == "__main__":
    run()
