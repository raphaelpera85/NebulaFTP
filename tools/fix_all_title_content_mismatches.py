"""
fix_all_title_content_mismatches.py

O problema raiz: o restore_mongo_from_telegram.py mapeou
  state_items[idx] (nome/título) -> obfuscated_list[idx] (chunks do Telegram)
mas a ordem dos chunks no Telegram (ordenados por tg_message) é DIFERENTE
da ordem dos itens no feed_ftp_state.json.

Resultado: todos os documentos MongoDB têm o NOME de um filme mas os CHUNKS de outro.

SOLUÇÃO:
1. Para cada doc MongoDB (que tem chunks corretos por obfuscated_id),
   descobrir qual é o TÍTULO REAL pelo número do chunk no Telegram.
   - Os chunks foram enviados agrupados por arquivo (consecutivos por obfuscated_id)
   - A ORDEM dos grupos de chunks = a ORDEM em que os arquivos foram uploadados
   - A ORDEM dos uploads = a ORDEM do feed_ftp_state.json (histórico real)

2. Construir a lista de media_items do state.json na ordem correta.
3. Para cada doc ordenado por first_tg_message, atribuir o nome do state_item correspondente.
4. Atualizar name, parent no MongoDB.
"""
import os, sys, json, re, pymongo
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')

CHUNK_SIZE = 67108864

def normalize_path(p):
    return p.replace('\\\\', '/').replace('\\', '/').lower()

def is_hash_name(s):
    return bool(re.match(r'^[0-9a-fA-F]{24}$', s))

def classify_category(state_path):
    norm = normalize_path(state_path)
    if "/porno/" in norm or "/adult/" in norm:
        return "Porno"
    elif "/series/" in norm or "/serie/" in norm:
        return "Series"
    else:
        return "Filmes"

def get_folder_and_file(state_path, user_root="/raphael"):
    """Given original state path like D:\\Filmes\\Movie Title (2020)\\Movie Title (2020).mkv,
    return (folder_name, file_name, category, parent_dir, dirs_to_create)"""
    norm = state_path.replace('\\\\', '\\').replace('/', '\\')
    parts = norm.split('\\')

    # Find category index
    cat_idx = None
    for i, p in enumerate(parts):
        if p.lower() in ("filmes", "series", "porno", "adult", "serie"):
            cat_idx = i
            break

    if cat_idx is None:
        return None, None, None, None, []

    cat_name = parts[cat_idx].title()
    if cat_name.lower() in ("porno", "adult"):
        cat_name = "Porno"
    elif cat_name.lower() in ("series", "serie"):
        cat_name = "Series"
    else:
        cat_name = "Filmes"

    remaining = parts[cat_idx + 1:]
    file_name = remaining[-1] if remaining else None
    sub_dirs = remaining[:-1] if len(remaining) > 1 else []

    # Build parent path
    parent = f"{user_root}/{cat_name}"
    dirs_to_create = [(cat_name, user_root)]

    for sd in sub_dirs:
        dirs_to_create.append((sd, parent))
        parent = f"{parent}/{sd}"

    return parent, file_name, cat_name, sub_dirs, dirs_to_create


client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DATABASE", "ftp")]
files_col = db.files
user_root = f'/{os.getenv("NEBULA_LIBRARY_USER", "raphael")}'
now = int(__import__("time").time())

# Load state
with open("feed_ftp_state.json", "r", encoding="utf-8") as f:
    state_items = json.load(f)

# Filter to non-strm non-hash video files only (same filter as restore script)
media_items = []
for x in state_items:
    if x.endswith(".strm"):
        continue
    norm = normalize_path(x)
    stem = os.path.splitext(os.path.basename(norm))[0]
    if "staging/" in norm or "nebulastage/" in norm or is_hash_name(stem):
        continue
    # Only uploadable extensions
    ext = os.path.splitext(norm)[1].lower()
    if ext not in {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".sub", ".ass", ".ssa", ".vtt", ".srt"}:
        continue
    media_items.append(x)

print(f"Feed state valid media items: {len(media_items)}")

# Load all MongoDB docs and sort by first tg_message (= upload order)
docs = list(files_col.find({"type": "file"}))
docs_sortable = [d for d in docs if d.get("parts") and d["parts"][0].get("tg_message")]
docs_by_msg = sorted(docs_sortable, key=lambda d: d["parts"][0]["tg_message"])
print(f"MongoDB docs sortable by upload order: {len(docs_by_msg)}")

limit = min(len(media_items), len(docs_by_msg))
print(f"Will fix: {limit} documents\n")

# Build the correct mapping and apply fixes
fixed = 0
skipped = 0
errors = []

# First: delete all existing dir documents (we'll recreate them)
# Keep root categories
categories_to_keep = {"Filmes", "Series", "Porno"}
all_dirs = list(files_col.find({"type": "dir"}))
for d in all_dirs:
    if d.get("name") in categories_to_keep and d.get("parent") == user_root:
        continue
    files_col.delete_one({"_id": d["_id"]})

print("Cleared old directory documents.")

# Now apply the correct name -> chunk mapping
for i in range(limit):
    state_path = media_items[i]
    doc = docs_by_msg[i]
    
    parent_dir, file_name, cat_name, sub_dirs, dirs_to_create = get_folder_and_file(state_path, user_root)
    
    if not parent_dir or not file_name:
        skipped += 1
        continue

    # Ensure directories exist
    current_parent = user_root
    for dir_name, dir_parent in dirs_to_create[1:]:  # skip root cat (already exists)
        if is_hash_name(dir_name):
            continue
        files_col.update_one(
            {"name": dir_name, "parent": dir_parent},
            {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}},
            upsert=True
        )
        current_parent = f"{dir_parent}/{dir_name}"

    # Update the file document with the correct name and parent
    old_name = doc.get("name", "")
    old_parent = doc.get("parent", "")

    files_col.update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "name": file_name,
            "parent": parent_dir,
            "mtime": now
        }}
    )
    fixed += 1

    if i < 10:
        print(f"  [{i}] {old_name[:60]} -> {file_name[:60]}")
        print(f"       {old_parent} -> {parent_dir}")

print(f"\nFixed: {fixed}")
print(f"Skipped: {skipped}")

# Remove any docs that had no state_item match (excess docs)
excess_count = len(docs_by_msg) - limit
if excess_count > 0:
    print(f"\nWarning: {excess_count} MongoDB docs have no corresponding state item!")

# Clean up empty dirs recursively
deleted_empty = 0
while True:
    dirs = list(files_col.find({"type": "dir"}))
    cleaned = 0
    for d in dirs:
        if d.get("name") in categories_to_keep and d.get("parent") == user_root:
            continue
        dir_path = f"{d.get('parent','')}/{d.get('name','')}"
        if files_col.count_documents({"parent": dir_path}) == 0:
            files_col.delete_one({"_id": d["_id"]})
            cleaned += 1
            deleted_empty += 1
    if cleaned == 0:
        break

print(f"Cleaned {deleted_empty} empty directories.")

remaining = files_col.count_documents({"type": "file"})
print(f"\nFINAL: {remaining} files in MongoDB with corrected titles.")
