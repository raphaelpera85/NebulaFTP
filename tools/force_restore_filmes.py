import json
import os
import sys
import time
import unicodedata

from dotenv import load_dotenv
import pymongo

load_dotenv()


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

    filmes_state = [x for x in state_items if not x.endswith(".strm") and ("\\Filmes\\" in x or "/Filmes/" in x or "\\Filmes" in x or "/Filmes" in x)]
    series_state = [x for x in state_items if not x.endswith(".strm") and x not in filmes_state]

    print(f"Mídias de Filmes no histórico: {len(filmes_state)}")
    print(f"Mídias de Séries no histórico: {len(series_state)}")

    now = int(time.time())
    user_root = "/raphael"

    # Ensure root category directories
    db.files.update_one({"name": "Filmes", "parent": user_root}, {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}}, upsert=True)
    db.files.update_one({"name": "Series", "parent": user_root}, {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}}, upsert=True)

    restored_filmes = 0
    for path in filmes_state:
        file_name = os.path.basename(path)
        parts = path.replace("/", "\\").split("\\")
        folder_name = parts[-2] if len(parts) >= 2 and parts[-2] not in ("Filmes", "midias") else os.path.splitext(file_name)[0]
        parent_dir = f"{user_root}/Filmes/{folder_name}"

        # Insert movie directory doc
        db.files.update_one(
            {"name": folder_name, "parent": f"{user_root}/Filmes"},
            {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}},
            upsert=True,
        )

        # Insert movie file doc
        file_doc = {
            "type": "file",
            "name": file_name,
            "parent": parent_dir,
            "size": 1000000000,
            "status": "completed",
            "mtime": now,
            "ctime": now,
            "obfuscated_id": "restored_" + folder_name,
            "uploaded_at": now,
            "parts": [{"part_id": 0, "tg_file": "restored", "tg_message": 1, "file_size": 1000000000}],
        }
        db.files.replace_one({"name": file_name, "parent": parent_dir}, file_doc, upsert=True)
        restored_filmes += 1

    restored_series = 0
    for path in series_state:
        file_name = os.path.basename(path)
        parts = path.replace("\\", "/").split("/")
        series_idx = -1
        for k_idx, part in enumerate(parts):
            if part.lower() in ("series", "séries"):
                series_idx = k_idx
                break

        if series_idx != -1 and len(parts) > series_idx + 1:
            rel_parent = "/".join(parts[series_idx + 1 : -1])
            parent_dir = f"{user_root}/Series/{rel_parent}"
            curr_p = f"{user_root}/Series"
            for subf in parts[series_idx + 1 : -1]:
                db.files.update_one(
                    {"name": subf, "parent": curr_p},
                    {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}},
                    upsert=True,
                )
                curr_p = f"{curr_p}/{subf}"
        else:
            stem = os.path.splitext(file_name)[0]
            parent_dir = f"{user_root}/Series/{stem}"
            db.files.update_one(
                {"name": stem, "parent": f"{user_root}/Series"},
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
            "obfuscated_id": "restored_" + file_name,
            "uploaded_at": now,
            "parts": [{"part_id": 0, "tg_file": "restored", "tg_message": 1, "file_size": 1000000000}],
        }
        db.files.replace_one({"name": file_name, "parent": parent_dir}, file_doc, upsert=True)
        restored_series += 1

    print(f"\n[SUCESSO] {restored_filmes} filmes e {restored_series} séries restaurados completamente no MongoDB (Drive N:)!")


if __name__ == "__main__":
    run()
