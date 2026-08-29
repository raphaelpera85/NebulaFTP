import asyncio
import json
import os
import re
import sys
import time
import unicodedata

from dotenv import load_dotenv
import pymongo
from pyrogram import Client
from pyrogram.errors import FloodWait

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


async def scan_bot_chunk(bot_index, token, chat_id, api_id, api_hash, start_id, end_id, obfuscated_dict, lock):
    """Scan message range with staggered start and rate limiting."""
    app = Client(f"recon_bot_{bot_index}", api_id=api_id, api_hash=api_hash, bot_token=token, in_memory=True)
    batch_size = 100
    local_count = 0

    await asyncio.sleep((bot_index - 1) * 0.1)
    try:
        async with app:
            for b_start in range(start_id, end_id, batch_size):
                b_end = min(b_start + batch_size, end_id)
                batch_ids = list(range(b_start, b_end))

                try:
                    msgs = await app.get_messages(chat_id, batch_ids)
                except FloodWait as fw:
                    await asyncio.sleep(fw.value + 1)
                    try:
                        msgs = await app.get_messages(chat_id, batch_ids)
                    except Exception:
                        continue
                except Exception:
                    await asyncio.sleep(1)
                    try:
                        msgs = await app.get_messages(chat_id, batch_ids)
                    except Exception:
                        continue

                if not msgs:
                    continue

                for m in msgs:
                    if m and m.document and m.document.file_name:
                        fname = m.document.file_name
                        if ".part_" in fname:
                            obf_id, part_str = fname.split(".part_")
                            try:
                                part_id = int(part_str)
                            except ValueError:
                                continue

                            async with lock:
                                if obf_id not in obfuscated_dict:
                                    obfuscated_dict[obf_id] = {
                                        "obfuscated_id": obf_id,
                                        "min_msg_id": m.id,
                                        "max_msg_id": m.id,
                                        "parts": {},
                                    }

                                obf_entry = obfuscated_dict[obf_id]
                                obf_entry["min_msg_id"] = min(obf_entry["min_msg_id"], m.id)
                                obf_entry["max_msg_id"] = max(obf_entry["max_msg_id"], m.id)
                                cap = m.caption or ""
                                if cap and "caption" not in obf_entry:
                                    obf_entry["caption"] = cap

                                obf_entry["parts"][part_id] = {
                                    "part_id": part_id,
                                    "tg_file": m.document.file_id,
                                    "tg_message": m.id,
                                    "file_size": m.document.file_size,
                                    "chunk_name": fname,
                                    "caption": cap,
                                    "bot_index": (bot_index - 1) % 28,
                                }
                                local_count += 1

                await asyncio.sleep(0.3)
    except Exception as exc:
        print(f"[BOT #{bot_index}] Exceção: {exc}")

    print(f"[BOT #{bot_index}] Concluído {start_id}..{end_id} ({local_count} peças)")


async def scan_telegram_parallel(api_id, api_hash, bot_tokens, chat_id, total_max_id=120000):
    print(f"[1/3] Varrendo canal do Telegram com {len(bot_tokens)} bots em paralelo (faixa 1..{total_max_id})...")

    obfuscated_dict = {}
    lock = asyncio.Lock()
    chunk_size = (total_max_id // len(bot_tokens)) + 1
    tasks = []

    for i, token in enumerate(bot_tokens):
        b_start = 1 + (i * chunk_size)
        b_end = min(total_max_id + 1, b_start + chunk_size)
        if b_start <= total_max_id:
            tasks.append(
                scan_bot_chunk(
                    bot_index=i + 1,
                    token=token,
                    chat_id=chat_id,
                    api_id=api_id,
                    api_hash=api_hash,
                    start_id=b_start,
                    end_id=b_end,
                    obfuscated_dict=obfuscated_dict,
                    lock=lock,
                )
            )

    await asyncio.gather(*tasks)
    print(f" -> Varredura paralela concluída! {len(obfuscated_dict)} mídias completas encontradas no Telegram.")
    return sorted(obfuscated_dict.values(), key=lambda x: x["min_msg_id"])


def restore_mongo_documents(ordered_obfuscated_list, state_items, db, user_root="/raphael"):
    print(f"\n[2/3] Reorganizando mídias do Telegram e salvando partes de streaming no MongoDB...")

    now = int(time.time())

    # Filter out strm and staging hash paths
    media_items = []
    for x in state_items:
        if x.endswith(".strm"):
            continue
        norm = x.replace("\\\\", "/").replace("\\", "/")
        stem = os.path.splitext(os.path.basename(norm))[0]
        if "staging/" in norm.lower() or "nebulastage/" in norm.lower() or is_hash_name(stem):
            continue
        media_items.append(x)

    # Ensure root category directories
    db.files.update_one({"name": "Filmes", "parent": user_root}, {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}}, upsert=True)
    db.files.update_one({"name": "Series", "parent": user_root}, {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}}, upsert=True)
    db.files.update_one({"name": "Porno", "parent": user_root}, {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}}, upsert=True)

    stats = {"Filmes": 0, "Series": 0, "Porno": 0}

    for idx, obf in enumerate(ordered_obfuscated_list):
        if idx >= len(media_items):
            print(f"[AVISO] Fim da lista de mídias limpas atingido em idx={idx}")
            break

        orig_path = media_items[idx]
        file_name = os.path.basename(orig_path)
        parent_dir, dirs_to_create, _, category = build_nodes_for_item(orig_path, user_root)

        for d_name, d_parent in dirs_to_create:
            if is_hash_name(d_name):
                continue
            db.files.update_one(
                {"name": d_name, "parent": d_parent},
                {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}},
                upsert=True,
            )

        parts_dict = obf["parts"]
        sorted_part_ids = sorted(parts_dict.keys())
        parts_array = [parts_dict[pid] for pid in sorted_part_ids]
        total_size = sum(p["file_size"] for p in parts_array)

        file_doc = {
            "type": "file",
            "name": file_name,
            "parent": parent_dir,
            "size": total_size,
            "status": "completed",
            "mtime": now,
            "ctime": now,
            "started_at": now - 300,
            "obfuscated_id": obf["obfuscated_id"],
            "uploaded_at": now,
            "parts": parts_array,
        }

        db.files.replace_one(
            {"name": file_name, "parent": parent_dir},
            file_doc,
            upsert=True,
        )
        stats[category] = stats.get(category, 0) + 1

    print(f"\n[3/3] Restauração e salvamento de rotas de streaming concluídos com sucesso!")
    print(f" -> Filmes com streaming ativo (N:\\Filmes): {stats.get('Filmes', 0)}")
    print(f" -> Séries com streaming ativo (N:\\Series): {stats.get('Series', 0)}")
    print(f" -> Porno com streaming ativo (N:\\Porno): {stats.get('Porno', 0)}")


async def main():
    mongo_uri = os.getenv("MONGODB", "mongodb://localhost:27017")
    db_name = os.getenv("MONGO_DATABASE", "ftp")

    client_db = pymongo.MongoClient(mongo_uri)
    db = client_db[db_name]

    state_path = "feed_ftp_state.json"
    if not os.path.exists(state_path):
        print(f"[ERRO] {state_path} não encontrado!")
        sys.exit(1)

    with open(state_path, "r", encoding="utf-8") as f:
        state_items = json.load(f)

    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    bot_tokens = [t.strip() for t in os.environ["BOT_TOKENS"].split(",") if t.strip()]
    chat_id = int(os.environ["CHAT_ID"])

    print("=== Restauração Completa do Telegram -> MongoDB / Drive N: ===")
    print(f"Banco MongoDB: {db_name}")
    print(f"Bots em paralelo: {len(bot_tokens)}")
    print("=============================================================\n")

    ordered_obfuscated_list = await scan_telegram_parallel(api_id, api_hash, bot_tokens, chat_id, total_max_id=120000)
    restore_mongo_documents(ordered_obfuscated_list, state_items, db)


if __name__ == "__main__":
    asyncio.run(main())
