from __future__ import annotations

import os
import sys
from pathlib import Path

root_dir = str(Path(__file__).resolve().parents[1])
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from dotenv import load_dotenv

if os.path.exists(".env"):
    load_dotenv()

from ftp.auth import hash_password
from pymongo import MongoClient


def main():
    mongo_uri = os.environ.get("MONGODB", "mongodb://localhost:27017")
    db_name = os.environ.get("MONGO_DATABASE", "ftp")
    print(f"Conectando ao MongoDB ({mongo_uri})...")
    client = MongoClient(mongo_uri)
    db = client[db_name]

    login = "raphael"
    password_plain = "Rapha151085*"

    hashed = hash_password(password_plain)
    doc = {
        "login": login,
        "password_hash": hashed,
        "permissions": [
            {"path": f"/{login}", "readable": True, "writable": True},
            {"path": "/", "readable": True, "writable": True},
        ],
    }

    db.users.update_one({"login": login}, {"$set": doc}, upsert=True)
    print(f"[OK] Usuario '{login}' criado/atualizado com sucesso no banco de dados '{db_name}'.")

    # Também executa o setup_database para garantir os índices
    db.files.create_index([("parent", 1), ("name", 1)], unique=True)
    db.files.create_index("parent")
    db.files.create_index("uploadId", sparse=True)
    db.files.create_index("uploaded_at")
    db.files.create_index("status")
    db.users.create_index("login", unique=True)
    print("[OK] Indices do MongoDB verificados/criados.")


if __name__ == "__main__":
    main()
