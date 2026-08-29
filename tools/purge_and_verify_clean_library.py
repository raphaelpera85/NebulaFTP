import asyncio
import os
import re
import sys
import json
import time
import pymongo
from dotenv import load_dotenv
from pyrogram import Client

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv(r'd:\Users\rapha\Documents\Projetos\nebula\NebulaFTP-master\.env')

EPISODE_RE = re.compile(r's(?P<season>\d{1,2})e(?P<episode>\d{1,2})|season|\.s\d{2}|\b\d{1,2}x\d{2}\b', re.IGNORECASE)
PORNO_RE = re.compile(r'\b(porno|porn|xxx|hentai|adulto|brazzers|bangbros|naughtyamerica)\b', re.IGNORECASE)

def is_hash_name(s: str) -> bool:
    return bool(re.match(r"^[0-9a-fA-F]{20,}$", s))

def is_truncated_file(parts: list) -> bool:
    """A multi-part file where the last chunk is exactly 64MB is truncated."""
    if len(parts) > 1:
        last_part = parts[-1]
        if last_part.get("file_size") == 67108864:
            return True
    return False

def categorize_filename(filename: str, parent: str) -> str:
    norm_parent = parent.lower()
    norm_name = filename.lower()

    if "/porno" in norm_parent or PORNO_RE.search(norm_name):
        return "Porno"
    elif "/series" in norm_parent or EPISODE_RE.search(norm_name):
        return "Series"
    else:
        return "Filmes"

async def verify_and_clean_library():
    print("=== HIGIENIZAÇÃO E VERIFICAÇÃO DE INTEGRIDADE DA BIBLIOTECA ===")

    mongo_uri = os.getenv("MONGODB", "mongodb://localhost:27017")
    db_name = os.getenv("MONGO_DATABASE", "ftp")
    client_db = pymongo.MongoClient(mongo_uri)
    db = client_db[db_name]

    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    bot_tokens = [t.strip() for t in os.environ["BOT_TOKENS"].split(",") if t.strip()]
    chat_id = int(os.environ["CHAT_ID"])

    docs = list(db.files.find({"type": "file"}))
    print(f"Total de arquivos no MongoDB: {len(docs)}")

    app = Client("clean_verifier", api_id=api_id, api_hash=api_hash, bot_token=bot_tokens[0], in_memory=True)

    removed_truncated = 0
    removed_hash = 0
    removed_mismatched_category = 0
    removed_missing_tg = 0
    verified_ok = 0

    batch_size = 50

    async with app:
        for i in range(0, len(docs), batch_size):
            batch = docs[i : i + batch_size]
            msg_ids_to_check = []
            doc_map = {}

            for doc in batch:
                name = doc.get("name", "")
                parent = doc.get("parent", "")
                parts = doc.get("parts", [])

                # Check 1: Hash named file or directory
                if is_hash_name(os.path.splitext(name)[0]):
                    db.files.delete_one({"_id": doc["_id"]})
                    removed_hash += 1
                    continue

                # Check 2: Truncated file (last chunk = 64MB)
                if is_truncated_file(parts):
                    db.files.delete_one({"_id": doc["_id"]})
                    removed_truncated += 1
                    continue

                # Check 3: Mismatched category (e.g. Episode placed inside Filmes)
                expected_cat = categorize_filename(name, parent)
                if expected_cat == "Series" and "/Filmes" in parent:
                    # Remove episodes wrongly placed in Filmes
                    db.files.delete_one({"_id": doc["_id"]})
                    removed_mismatched_category += 1
                    continue
                elif expected_cat == "Filmes" and "/Series" in parent:
                    # Remove movies wrongly placed in Series
                    db.files.delete_one({"_id": doc["_id"]})
                    removed_mismatched_category += 1
                    continue

                # Check 4: Telegram message availability
                if parts:
                    first_msg = parts[0].get("tg_message")
                    if first_msg:
                        msg_ids_to_check.append(first_msg)
                        doc_map[first_msg] = doc

            # Batch check Telegram message validity
            if msg_ids_to_check:
                try:
                    msgs = await app.get_messages(chat_id, msg_ids_to_check)
                    msg_dict = {}
                    if isinstance(msgs, list):
                        for m in msgs:
                            if m: msg_dict[m.id] = m
                    else:
                        if msgs: msg_dict[msgs.id] = msgs

                    for msg_id, doc in doc_map.items():
                        m = msg_dict.get(msg_id)
                        if not m or not m.document:
                            db.files.delete_one({"_id": doc["_id"]})
                            removed_missing_tg += 1
                        else:
                            verified_ok += 1
                except Exception as ex:
                    print(f"Erro na verificação de batch no Telegram: {ex}")

            progress = min(i + batch_size, len(docs))
            if progress % 250 == 0 or progress == len(docs):
                print(f"Progresso: {progress}/{len(docs)} | Verificados OK: {verified_ok} | Removidos: truncados={removed_truncated}, hash={removed_hash}, categoria_errada={removed_mismatched_category}, sem_tg={removed_missing_tg}")

    # Remove empty directories
    print("\nLimpando diretórios vazios no MongoDB...")
    deleted_empty_dirs = 0
    categories_to_keep = {"Filmes", "Series", "Porno"}
    user_root = "/raphael"

    while True:
        all_dirs = list(db.files.find({"type": "dir"}))
        cleaned = 0
        for d in all_dirs:
            if d.get("name") in categories_to_keep and d.get("parent") == user_root:
                continue
            dir_path = f"{d.get('parent','')}/{d.get('name','')}"
            if db.files.count_documents({"parent": dir_path}) == 0:
                db.files.delete_one({"_id": d["_id"]})
                cleaned += 1
                deleted_empty_dirs += 1
        if cleaned == 0:
            break

    print(f"Diretórios vazios limpos: {deleted_empty_dirs}")

    # Final summary
    final_files = db.files.count_documents({"type": "file"})
    filmes = sum(1 for d in db.files.find({"type": "file"}) if "/Filmes" in d.get("parent",""))
    series = sum(1 for d in db.files.find({"type": "file"}) if "/Series" in d.get("parent",""))
    porno = sum(1 for d in db.files.find({"type": "file"}) if "/Porno" in d.get("parent",""))

    print("\n=== RESULTADO FINAL DA BIBLIOTECA VERIFICADA ===")
    print(f"Total de mídias 100% integras no MongoDB: {final_files}")
    print(f" -> Filmes: {filmes}")
    print(f" -> Séries: {series}")
    print(f" -> Porno:  {porno}")

if __name__ == "__main__":
    asyncio.run(verify_and_clean_library())
