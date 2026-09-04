import json
import os
import re
import sys
import time
import unicodedata

from dotenv import load_dotenv
import pymongo

load_dotenv()


def normalize_str(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").lower()


def is_hash_name(name: str) -> bool:
    return bool(re.match(r"^[0-9a-fA-F]{20,}$", name))


def categorize_path(orig_path: str):
    norm = orig_path.replace("\\\\", "/").replace("\\", "/")
    p_norm = normalize_str(norm)
    parts_norm = [p for p in p_norm.split("/") if p]
    orig_parts = [p for p in norm.split("/") if p]

    if any(x in parts_norm for x in ["porno", "porn", "xxx", "adulto"]):
        category = "Porno"
        rel_parts = []
        for i, p in enumerate(parts_norm):
            if p in ("porno", "porn", "xxx", "adulto"):
                rel_parts = orig_parts[i + 1 :]
                break
        rel = "/".join(rel_parts) if rel_parts else os.path.basename(norm)
    elif "series" in parts_norm:
        category = "Series"
        rel_parts = []
        for i, p in enumerate(parts_norm):
            if p == "series":
                rel_parts = orig_parts[i + 1 :]
                break
        rel = "/".join(rel_parts) if rel_parts else os.path.basename(norm)
    elif "filmes" in parts_norm:
        category = "Filmes"
        rel_parts = []
        for i, p in enumerate(parts_norm):
            if p == "filmes":
                rel_parts = orig_parts[i + 1 :]
                break
        rel = "/".join(rel_parts) if rel_parts else os.path.basename(norm)
    else:
        fname = os.path.basename(norm)
        fn_norm = normalize_str(fname)
        if re.search(r"\b(porno|porn|xxx|hentai|adulto)\b", fn_norm):
            category = "Porno"
            rel = fname
        elif re.search(r"\bs\d{1,2}e\d{1,2}\b|season|\.s\d{2}", fn_norm):
            category = "Series"
            rel = fname
        else:
            category = "Filmes"
            rel = fname

    return category, rel


def build_nodes_for_item(orig_path: str, user_root="/raphael"):
    category, rel_path = categorize_path(orig_path)
    parts = [p for p in rel_path.split("/") if p]
    if not parts:
        return f"{user_root}/{category}", [], os.path.basename(orig_path), category

    file_name = parts[-1]
    dir_parts = parts[:-1]

    dirs_to_create = []
    current_parent = f"{user_root}/{category}"

    if not dir_parts:
        if category == "Filmes":
            stem = os.path.splitext(file_name)[0]
            dirs_to_create.append((stem, current_parent))
            current_parent = f"{current_parent}/{stem}"
    else:
        for d in dir_parts:
            dirs_to_create.append((d, current_parent))
            current_parent = f"{current_parent}/{d}"

    return current_parent, dirs_to_create, file_name, category


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

    # Filter out strm files and staging hash paths
    media_items = []
    for x in state_items:
        if x.endswith(".strm"):
            continue
        norm = x.replace("\\\\", "/").replace("\\", "/")
        stem = os.path.splitext(os.path.basename(norm))[0]
        if "staging/" in norm.lower() or "nebulastage/" in norm.lower() or is_hash_name(stem):
            continue
        media_items.append(x)

    print(f"Clean media items to index: {len(media_items)}")

    now = int(time.time())
    user_root = "/raphael"

    # Step 1: Clear old misclassified directory and file documents in Mongo
    print("Step 1: Cleaning database documents under user root...")
    db.files.delete_many({"parent": {"$regex": f"^{re.escape(user_root)}"}})

    # Step 2: Create root category directories
    print("Step 2: Creating top-level category directories (Filmes, Series, Porno)...")
    db.files.update_one({"name": "Filmes", "parent": user_root}, {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}}, upsert=True)
    db.files.update_one({"name": "Series", "parent": user_root}, {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}}, upsert=True)
    db.files.update_one({"name": "Porno", "parent": user_root}, {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}}, upsert=True)

    # Step 3: Re-index clean media items into MongoDB
    print("Step 3: Re-indexing clean media items into MongoDB...")
    stats = {"Filmes": 0, "Series": 0, "Porno": 0}

    for path in media_items:
        parent_dir, dirs_to_create, file_name, category = build_nodes_for_item(path, user_root)

        for d_name, d_parent in dirs_to_create:
            if is_hash_name(d_name):
                continue
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
        stats[category] = stats.get(category, 0) + 1

    # Remove any leftover hash directory documents from MongoDB
    db.files.delete_many({"type": "dir", "name": {"$regex": "^[0-9a-fA-F]{20,}$"}})

    print(f"\n[SUCESSO] Limpeza de hashes e reorganização concluída no MongoDB!")
    print(f" -> Filmes (N:\\Filmes): {stats.get('Filmes', 0)}")
    print(f" -> Séries (N:\\Series): {stats.get('Series', 0)}")
    print(f" -> Porno (N:\\Porno): {stats.get('Porno', 0)}")


if __name__ == "__main__":
    run()
