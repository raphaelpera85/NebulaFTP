"""
fix_titles_clean_rebuild.py

Abordagem definitiva: 
1. Guardar todos os dados de partes/chunks de cada doc
2. Deletar TODOS os docs de arquivo do MongoDB
3. Recriar do zero com nome+parent corretos + chunks corretos
"""
import os, sys, json, re, pymongo
sys.stdout.reconfigure(encoding='utf-8')

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

def parse_parent_and_file(state_path, user_root):
    norm = state_path.replace('\\\\', '\\').replace('/', '\\')
    parts = norm.split('\\')
    cat_idx = None
    cat_name = None
    for i, p in enumerate(parts):
        p_lower = p.lower()
        if p_lower in ("filmes", "movies"):
            cat_idx = i; cat_name = "Filmes"; break
        elif p_lower in ("series", "serie", "tv"):
            cat_idx = i; cat_name = "Series"; break
        elif p_lower in ("porno", "adult", "xxx"):
            cat_idx = i; cat_name = "Porno"; break
    if cat_idx is None: return None, None, []
    remaining = parts[cat_idx + 1:]
    if not remaining: return None, None, []
    file_name = remaining[-1]
    sub_dirs = remaining[:-1]
    parent = f"{user_root}/{cat_name}"
    dirs_to_create = []
    for sd in sub_dirs:
        if not sd or is_hash_name(sd): continue
        dirs_to_create.append((sd, parent))
        parent = f"{parent}/{sd}"
    return parent, file_name, dirs_to_create


client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DATABASE", "ftp")]
files_col = db.files
user_root = f'/{os.getenv("NEBULA_LIBRARY_USER", "raphael")}'
now = int(__import__("time").time())

with open("feed_ftp_state.json", "r", encoding="utf-8") as f:
    state_items = json.load(f)

# Build ordered media items
all_media_items = []
for x in state_items:
    if x.endswith(".strm"): continue
    norm = normalize_path(x)
    stem = os.path.splitext(os.path.basename(norm))[0]
    if "staging/" in norm or "nebulastage/" in norm or is_hash_name(stem): continue
    ext = os.path.splitext(norm)[1].lower()
    if ext not in {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".sub", ".ass", ".ssa", ".vtt", ".srt"}: continue
    all_media_items.append(x)

# DEDUPLICATE: keep only the FIRST occurrence of each unique filename stem
# (F: drive had the same files re-scanned, generating duplicates)
seen_stems = set()
deduped_media_items = []
for x in all_media_items:
    norm = normalize_path(x)
    stem = os.path.splitext(os.path.basename(norm))[0].lower().strip()
    if stem not in seen_stems:
        seen_stems.add(stem)
        deduped_media_items.append(x)
all_media_items = deduped_media_items
print(f"After deduplication: {len(all_media_items)} unique items (removed {len(deduped_media_items) - len(all_media_items)} duplicates)")


source_order = []
seen = set()
for x in state_items:
    drv = get_drive(x)
    cat = get_category(x)
    key = f"{drv}:{cat}"
    if key not in seen:
        seen.add(key)
        source_order.append(key)

# Group by drive+cat
by_source = {}
for x in all_media_items:
    key = f"{get_drive(x)}:{get_category(x)}"
    if key not in by_source: by_source[key] = []
    by_source[key].append(x)

# Build ordered list
ordered_media_items = []
for key in source_order:
    items = sorted(by_source.get(key, []), key=normalize_path)
    ordered_media_items.extend(items)

print(f"Ordered state items: {len(ordered_media_items)}")

# Load MongoDB docs sorted by first tg_message (preserving their chunks)
docs = list(files_col.find({"type": "file"}))
# Sort by first_tg_message OR by name for those that have temp names
def sort_key(d):
    parts = d.get("parts", [])
    if parts and parts[0].get("tg_message"):
        return parts[0]["tg_message"]
    return 999999999

docs_by_msg = sorted(docs, key=sort_key)
print(f"MongoDB docs: {len(docs_by_msg)}")

limit = min(len(ordered_media_items), len(docs_by_msg))
print(f"Will process: {limit}")

# Step 1: Delete ALL file documents and ALL non-root dirs
print("\nStep 1: Deleting all file docs...")
result = files_col.delete_many({"type": "file"})
print(f"  Deleted {result.deleted_count} file documents")

# Delete non-root directories
categories_to_keep = {"Filmes", "Series", "Porno"}
result = files_col.delete_many({
    "type": "dir",
    "$nor": [
        {"name": {"$in": list(categories_to_keep)}, "parent": user_root}
    ]
})
print(f"  Deleted {result.deleted_count} subdirectory documents")

# Step 2: Recreate everything from scratch with correct mappings
print(f"\nStep 2: Recreating {limit} file documents with correct titles...")

inserted = 0
skipped = 0
errors = []

for i in range(limit):
    state_path = ordered_media_items[i]
    doc = docs_by_msg[i]
    
    parent_dir, file_name, dirs_to_create = parse_parent_and_file(state_path, user_root)
    
    if not parent_dir or not file_name:
        skipped += 1
        continue
    
    # Ensure directories exist
    for dir_name, dir_parent in dirs_to_create:
        if is_hash_name(dir_name): continue
        try:
            files_col.update_one(
                {"name": dir_name, "parent": dir_parent},
                {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}},
                upsert=True
            )
        except Exception as e:
            errors.append(f"Dir {dir_parent}/{dir_name}: {e}")
    
    # Insert new file doc with correct name+parent but original chunks
    new_doc = {
        "type": "file",
        "name": file_name,
        "parent": parent_dir,
        "size": doc.get("size", 0),
        "status": "completed",
        "mtime": now,
        "ctime": doc.get("ctime", now),
        "started_at": doc.get("started_at", now),
        "obfuscated_id": doc.get("obfuscated_id", ""),
        "uploaded_at": doc.get("uploaded_at", now),
        "parts": doc.get("parts", []),
    }
    if doc.get("stream_bot_name"):
        new_doc["stream_bot_name"] = doc["stream_bot_name"]
    
    try:
        files_col.insert_one(new_doc)
        inserted += 1
    except Exception as e:
        errors.append(f"Insert [{i}] {file_name}: {e}")
        skipped += 1

print(f"  Inserted: {inserted}")
print(f"  Skipped: {skipped}")
if errors:
    print(f"  Errors ({len(errors)}):")
    for e in errors[:10]:
        print(f"    {e}")

# Final state
final_files = files_col.count_documents({"type": "file"})
final_dirs = files_col.count_documents({"type": "dir"})
print(f"\n✅ FINAL STATE: {final_files} files, {final_dirs} directories")
