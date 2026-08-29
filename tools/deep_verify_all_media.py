"""
deep_verify_all_media.py

Varredura completa de TODAS as mídias no MongoDB contra o Telegram.

Para cada arquivo no MongoDB:
1. Busca a mensagem do Telegram correspondente ao primeiro chunk (part_0)
2. Verifica se o chunk_name no Telegram bate com o obfuscated_id do arquivo
3. Se chunk_name do Telegram NÃO bate com obfuscated_id do MongoDB -> mapeamento errado
4. Constrói um mapa inverso: tg_file_id -> {obfuscated_id, nome, parent}
5. Detecta arquivos que compartilham o mesmo obfuscated_id (duplicatas)
6. Reporta todos os problemas encontrados

Este script APENAS REPORTA - não modifica nada.
Use verify_and_fix_media.py para aplicar as correções.
"""

import asyncio
import json
import os
import sys
import pymongo
from pyrogram import Client
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

CHUNK_SIZE = 67108864


async def deep_verify(batch_size=50):
    client_db = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client_db[os.getenv("MONGO_DATABASE", "ftp")]
    files_col = db.files

    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH")
    bot_tokens = [t.strip() for t in os.getenv("BOT_TOKENS", "").split(",") if t.strip()]
    chat_id = int(os.getenv("CHAT_ID"))

    docs = list(files_col.find({"type": "file"}))
    print(f"Total file docs to verify: {len(docs)}")

    # Group by bot_index for efficient fetching
    # Each file has parts with bot_index - use the bot token that uploaded it
    bot_clients = {}
    for i, token in enumerate(bot_tokens):
        app = Client(f"verify_bot_{i}", api_id=api_id, api_hash=api_hash, bot_token=token, in_memory=True)
        bot_clients[i] = app

    # Track: obfuscated_id -> list of MongoDB docs that claim to use it
    obf_id_map = {}
    # Track: tg_file_id (part_0) -> doc
    tg_first_chunk_map = {}

    problems = []

    # First pass: build internal maps without hitting Telegram
    for d in docs:
        obf_id = d.get("obfuscated_id", "")
        parts = d.get("parts", [])
        full_path = f"{d.get('parent','')}/{d.get('name','')}"

        if not obf_id:
            problems.append({
                "type": "missing_obfuscated_id",
                "path": full_path
            })
            continue

        if obf_id in obf_id_map:
            obf_id_map[obf_id].append(full_path)
        else:
            obf_id_map[obf_id] = [full_path]

        if parts:
            first_tg_file = parts[0].get("tg_file", "")
            if first_tg_file:
                if first_tg_file in tg_first_chunk_map:
                    tg_first_chunk_map[first_tg_file].append(full_path)
                else:
                    tg_first_chunk_map[first_tg_file] = [full_path]

    # Detect obfuscated_id duplicates
    dup_obf = {k: v for k, v in obf_id_map.items() if len(v) > 1}
    if dup_obf:
        for obf_id, paths in dup_obf.items():
            problems.append({
                "type": "duplicate_obfuscated_id",
                "obfuscated_id": obf_id,
                "paths": paths
            })

    # Detect tg_file duplicates (same chunk used by multiple files)
    dup_tg = {k: v for k, v in tg_first_chunk_map.items() if len(v) > 1}
    if dup_tg:
        for tg_id, paths in dup_tg.items():
            problems.append({
                "type": "duplicate_tg_chunk_reference",
                "tg_file_id": tg_id[:40],
                "paths": paths
            })

    # Now verify Telegram chunk names against expected obfuscated_id
    # Pick the first bot to fetch messages
    primary_bot_index = 0
    app = bot_clients[primary_bot_index]

    print(f"\nConnecting to Telegram to verify chunk names...")
    print(f"This may take a while for {len(docs)} files...")

    verified = 0
    chunk_name_mismatches = []
    errors = []

    async with app:
        # Process in batches
        all_parts_to_check = []  # (doc, part_index, msg_id, expected_chunk_name)
        for d in docs:
            parts = d.get("parts", [])
            obf_id = d.get("obfuscated_id", "")
            if not parts or not obf_id:
                continue
            # Only check the first part (part_0) of each file
            first_part = parts[0]
            msg_id = first_part.get("tg_message")
            expected_chunk_name = f"{obf_id}.part_000"
            if msg_id:
                all_parts_to_check.append((d, first_part, msg_id, expected_chunk_name))

        print(f"Checking {len(all_parts_to_check)} first chunks against Telegram...")

        for i in range(0, len(all_parts_to_check), batch_size):
            batch = all_parts_to_check[i:i + batch_size]
            msg_ids = [item[2] for item in batch]

            try:
                msgs = await app.get_messages(chat_id, msg_ids)
                msg_dict = {}
                if isinstance(msgs, list):
                    for m in msgs:
                        if m:
                            msg_dict[m.id] = m
                else:
                    if msgs:
                        msg_dict[msgs.id] = msgs
            except Exception as ex:
                errors.append(f"Batch {i//batch_size}: {ex}")
                continue

            for doc, first_part, msg_id, expected_chunk_name in batch:
                full_path = f"{doc.get('parent','')}/{doc.get('name','')}"
                msg = msg_dict.get(msg_id)

                if not msg:
                    problems.append({
                        "type": "tg_message_not_found",
                        "path": full_path,
                        "msg_id": msg_id
                    })
                    continue

                doc_obj = msg.document
                if not doc_obj:
                    problems.append({
                        "type": "tg_message_has_no_document",
                        "path": full_path,
                        "msg_id": msg_id
                    })
                    continue

                tg_file_name = doc_obj.file_name or ""
                if tg_file_name != expected_chunk_name:
                    chunk_name_mismatches.append({
                        "path": full_path,
                        "obfuscated_id": doc.get("obfuscated_id"),
                        "expected_chunk_name": expected_chunk_name,
                        "actual_tg_chunk_name": tg_file_name,
                        "tg_msg_id": msg_id,
                        "file_size_tg": doc_obj.file_size,
                        "file_size_db": doc.get("size")
                    })
                else:
                    verified += 1

            progress = min(i + batch_size, len(all_parts_to_check))
            print(f"  Progress: {progress}/{len(all_parts_to_check)} ({100*progress//len(all_parts_to_check)}%) | verified={verified} mismatches={len(chunk_name_mismatches)}", flush=True)

    problems.extend([{"type": "chunk_name_mismatch", **m} for m in chunk_name_mismatches])

    report = {
        "total_docs": len(docs),
        "verified_ok": verified,
        "total_problems": len(problems),
        "chunk_name_mismatches": len(chunk_name_mismatches),
        "duplicate_obfuscated_ids": len(dup_obf),
        "duplicate_tg_chunk_refs": len(dup_tg),
        "telegram_errors": errors,
        "problems": problems
    }

    out_path = "deep_verify_report.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n=== DEEP VERIFICATION REPORT ===")
    print(f"Total files scanned:              {len(docs)}")
    print(f"Chunk names verified OK:          {verified}")
    print(f"Chunk name MISMATCHES:            {len(chunk_name_mismatches)}")
    print(f"Duplicate obfuscated_ids:         {len(dup_obf)}")
    print(f"Duplicate TG chunk references:    {len(dup_tg)}")
    print(f"Total problems detected:          {len(problems)}")
    print(f"\nFull report saved to: {out_path}")

    if chunk_name_mismatches[:10]:
        print(f"\nSample chunk name mismatches:")
        for m in chunk_name_mismatches[:10]:
            print(f"  PATH: {m['path']}")
            print(f"    expected: {m['expected_chunk_name']}")
            print(f"    actual:   {m['actual_tg_chunk_name']}")
            print()

if __name__ == "__main__":
    asyncio.run(deep_verify())
