import asyncio
import json
import os
import re
import sys
import time
import pymongo
from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.errors import FloodWait

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv(r'd:\Users\rapha\Documents\Projetos\nebula\NebulaFTP-master\.env')

def normalize_path(p):
    return p.replace('\\\\', '/').replace('\\', '/').lower()

def is_hash_name(s):
    return bool(re.match(r'^[0-9a-fA-F]{24}$', s))

def get_category(state_path):
    norm = normalize_path(state_path)
    if "/porno/" in norm or "/adult/" in norm: return "Porno"
    elif "/series/" in norm or "/serie/" in norm: return "Series"
    return "Filmes"

def get_drive(state_path):
    norm = state_path.upper()
    for d in "DEFGHIJKLMNO":
        if norm.startswith(f"{d}:"): return d
    return "?"

def build_nodes_for_item(orig_path: str, user_root="/raphael"):
    norm = orig_path.replace("\\\\", "/").replace("\\", "/")
    parts_orig = [p for p in norm.split("/") if p]

    # Find category
    cat_idx = None
    cat_name = "Filmes"
    for i, p in enumerate(parts_orig):
        p_lower = p.lower()
        if p_lower in ("filmes", "movies"):
            cat_idx = i; cat_name = "Filmes"; break
        elif p_lower in ("series", "serie", "tv"):
            cat_idx = i; cat_name = "Series"; break
        elif p_lower in ("porno", "adult", "xxx"):
            cat_idx = i; cat_name = "Porno"; break

    if cat_idx is None:
        return f"{user_root}/Filmes", [], os.path.basename(norm)

    remaining = parts_orig[cat_idx + 1:]
    if not remaining:
        return f"{user_root}/{cat_name}", [], os.path.basename(norm)

    file_name = remaining[-1]
    dir_parts = remaining[:-1]

    dirs_to_create = []
    current_parent = f"{user_root}/{cat_name}"

    if not dir_parts:
        if cat_name == "Filmes":
            stem = os.path.splitext(file_name)[0]
            dirs_to_create.append((stem, current_parent))
            current_parent = f"{current_parent}/{stem}"
    else:
        for d in dir_parts:
            if is_hash_name(d): continue
            dirs_to_create.append((d, current_parent))
            current_parent = f"{current_parent}/{d}"

    return current_parent, dirs_to_create, file_name

async def scan_and_restore():
    state_path = "feed_ftp_state.json"
    with open(state_path, "r", encoding="utf-8") as f:
        state_items = json.load(f)

    # Build raw media items in exact state order (non-strm, non-hash)
    media_items = []
    for x in state_items:
        if x.endswith(".strm"): continue
        norm = normalize_path(x)
        stem = os.path.splitext(os.path.basename(norm))[0]
        if "staging/" in norm or "nebulastage/" in norm or is_hash_name(stem): continue
        ext = os.path.splitext(norm)[1].lower()
        if ext not in {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".sub", ".ass", ".ssa", ".vtt", ".srt"}: continue
        media_items.append(x)

    # Build ordered list grouped by drive+category processing order
    source_order = []
    seen = set()
    for x in state_items:
        drv = get_drive(x)
        cat = get_category(x)
        key = f"{drv}:{cat}"
        if key not in seen:
            seen.add(key)
            source_order.append(key)

    by_source = {}
    for x in media_items:
        key = f"{get_drive(x)}:{get_category(x)}"
        if key not in by_source: by_source[key] = []
        by_source[key].append(x)

    ordered_state_media = []
    for key in source_order:
        items = sorted(by_source.get(key, []), key=normalize_path)
        ordered_state_media.extend(items)

    print(f"State media items count: {len(ordered_state_media)}")

    # Scan Telegram
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    bot_tokens = [t.strip() for t in os.environ["BOT_TOKENS"].split(",") if t.strip()]
    chat_id = int(os.environ["CHAT_ID"])

    app = Client("scan_restore_bot", api_id=api_id, api_hash=api_hash, bot_token=bot_tokens[0], in_memory=True)
    obfuscated_dict = {}

    batch_size = 100
    total_max_id = 45000

    print("Scanning Telegram channel...")
    async with app:
        for b_start in range(1, total_max_id, batch_size):
            b_end = min(b_start + batch_size, total_max_id)
            batch_ids = list(range(b_start, b_end))

            msgs = None
            for attempt in range(3):
                try:
                    msgs = await app.get_messages(chat_id, batch_ids)
                    break
                except FloodWait as fw:
                    await asyncio.sleep(fw.value + 1)
                except Exception:
                    await asyncio.sleep(0.5)

            if not msgs: continue

            for m in msgs:
                if m and m.document and m.document.file_name and ".part_" in m.document.file_name:
                    fname = m.document.file_name
                    obf_id, part_str = fname.split(".part_")
                    try:
                        part_id = int(part_str)
                    except ValueError:
                        continue

                    if obf_id not in obfuscated_dict:
                        obfuscated_dict[obf_id] = {
                            "obfuscated_id": obf_id,
                            "min_msg_id": m.id,
                            "parts": {},
                        }

                    obfuscated_dict[obf_id]["parts"][part_id] = {
                        "part_id": part_id,
                        "tg_file": m.document.file_id,
                        "tg_message": m.id,
                        "file_size": m.document.file_size,
                        "chunk_name": fname,
                        "bot_index": 0,
                    }

    sorted_obf = sorted(obfuscated_dict.values(), key=lambda x: x["min_msg_id"])
    print(f"Found {len(sorted_obf)} media objects in Telegram.")

    # Populate MongoDB
    client_db = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client_db[os.getenv("MONGO_DATABASE", "ftp")]
    db.files.delete_many({"type": "file"})
    db.files.delete_many({"type": "dir", "name": {"$nin": ["Filmes", "Series", "Porno"]}})

    now = int(time.time())
    user_root = "/raphael"

    db.files.update_one({"name": "Filmes", "parent": user_root}, {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}}, upsert=True)
    db.files.update_one({"name": "Series", "parent": user_root}, {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}}, upsert=True)
    db.files.update_one({"name": "Porno", "parent": user_root}, {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}}, upsert=True)

    restored = 0
    limit = min(len(sorted_obf), len(ordered_state_media))
    print(f"Restoring {limit} files to MongoDB...")

    for idx in range(limit):
        obf = sorted_obf[idx]
        orig_path = ordered_state_media[idx]
        
        parent_dir, dirs_to_create, file_name = build_nodes_for_item(orig_path, user_root)

        for d_name, d_parent in dirs_to_create:
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
        restored += 1

    print(f"Successfully restored {restored} media items to MongoDB!")

if __name__ == "__main__":
    asyncio.run(scan_and_restore())
