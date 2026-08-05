import asyncio
import json
import os
import sys
import time
import unicodedata

from dotenv import load_dotenv
import pymongo
from pyrogram import Client

load_dotenv()


def normalize_title(s: str) -> str:
    if not s:
        return ""
    normalized = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("utf-8")
    return "".join(c for c in normalized.lower() if c.isalnum())


async def scan_bot_chunk(bot_index, token, chat_id, api_id, api_hash, start_id, end_id, obfuscated_dict, lock):
    """Scan a specific range of message IDs using one bot worker."""
    app = Client(f"recon_bot_{bot_index}", api_id=api_id, api_hash=api_hash, bot_token=token, in_memory=True)
    batch_size = 100
    local_count = 0

    try:
        async with app:
            for b_start in range(start_id, end_id, batch_size):
                b_end = min(b_start + batch_size, end_id)
                batch_ids = list(range(b_start, b_end))

                try:
                    msgs = await app.get_messages(chat_id, batch_ids)
                except Exception as err:
                    await asyncio.sleep(5)
                    try:
                        msgs = await app.get_messages(chat_id, batch_ids)
                    except Exception:
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

                                obf_entry["parts"][part_id] = {
                                    "part_id": part_id,
                                    "tg_file": m.document.file_id,
                                    "tg_message": m.id,
                                    "file_size": m.document.file_size,
                                    "chunk_name": fname,
                                    "bot_index": bot_index % 28,
                                }
                                local_count += 1

                await asyncio.sleep(0.05)
    except Exception as exc:
        print(f"[BOT #{bot_index}] Exceção: {exc}")

    print(f"[BOT #{bot_index}] Concluído {start_id}..{end_id} ({local_count} peças)")


async def scan_telegram_parallel(api_id, api_hash, bot_tokens, chat_id, total_max_id=85000):
    """Scan Telegram channel in parallel using all 28 bots."""
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
    """Match ordered Telegram files with feed_ftp_state.json categorized by Filmes vs Series."""
    print(f"\n[2/3] Organizando mídias do Telegram em Filmes e Séries...")

    now = int(time.time())

    filmes_state = []
    series_state = []

    for path in state_items:
        if path.endswith(".strm"):
            continue
        if "\\Filmes\\" in path or "/Filmes/" in path:
            filmes_state.append(path)
        else:
            series_state.append(path)

    print(f" -> Mídias de Filmes no histórico: {len(filmes_state)}")
    print(f" -> Mídias de Séries no histórico: {len(series_state)}")

    # Ensure root directories exist
    db.files.update_one(
        {"name": "Filmes", "parent": user_root},
        {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}},
        upsert=True,
    )
    db.files.update_one(
        {"name": "Series", "parent": user_root},
        {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}},
        upsert=True,
    )

    restored_filmes = 0
    restored_series = 0

    categorized_state = filmes_state + series_state

    for idx, obf in enumerate(ordered_obfuscated_list):
        if idx >= len(categorized_state):
            print(f"[AVISO] Fim do histórico de caminhos atingido em idx={idx}")
            break

        orig_path = categorized_state[idx]
        file_name = os.path.basename(orig_path)
        is_filme = idx < len(filmes_state)

        if is_filme:
            parts = orig_path.replace("/", "\\").split("\\")
            if len(parts) >= 2 and parts[-2] not in ("Filmes", "midias"):
                folder_name = parts[-2]
            else:
                folder_name = os.path.splitext(file_name)[0]

            parent_dir = f"{user_root}/Filmes/{folder_name}"

            db.files.update_one(
                {"name": folder_name, "parent": f"{user_root}/Filmes"},
                {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}},
                upsert=True,
            )
            restored_filmes += 1
        else:
            parts = orig_path.replace("\\", "/").split("/")
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
            restored_series += 1

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

    print(f"\n[3/3] Restauração concluída com sucesso!")
    print(f" -> Filmes reindexados no Drive N: {restored_filmes}")
    print(f" -> Séries reindexadas no Drive N: {restored_series}")


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

    print("=== Restauração Paralela de Filmes e Séries (Telegram -> MongoDB / N:) ===")
    print(f"Banco MongoDB: {db_name}")
    print(f"Bots em paralelo: {len(bot_tokens)}")
    print("=========================================================================\n")

    ordered_obfuscated_list = await scan_telegram_parallel(api_id, api_hash, bot_tokens, chat_id, total_max_id=85000)
    restore_mongo_documents(ordered_obfuscated_list, state_items, db)


if __name__ == "__main__":
    asyncio.run(main())
