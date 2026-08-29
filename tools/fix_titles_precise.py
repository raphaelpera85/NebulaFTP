"""
fix_titles_precise.py

Reconstrói o mapeamento correto entre títulos (state file) e chunks (MongoDB/Telegram)
usando a ORDEM EXATA dos processamentos (por drive+categoria, depois alfabético dentro de cada drive).

Aplica SOMENTE as trocas onde o título está errado — não mexe em quem já está certo.
"""
import os, sys, json, re, pymongo
sys.stdout.reconfigure(encoding='utf-8')

CHUNK_SIZE = 67108864

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
    if norm.startswith("D:"): return "D"
    if norm.startswith("E:"): return "E"
    if norm.startswith("F:"): return "F"
    if norm.startswith("G:"): return "G"
    return "?"

def parse_parent_and_file(state_path, user_root):
    """
    Parse D:\\Filmes\\Movie Title (2020)\\Movie Title (2020).mkv
    Return (parent_dir, file_name, list_of_dirs_to_create)
    """
    norm = state_path.replace('\\\\', '\\').replace('/', '\\')
    parts = norm.split('\\')
    
    cat_idx = None
    cat_name = None
    for i, p in enumerate(parts):
        p_lower = p.lower()
        if p_lower in ("filmes", "movies"):
            cat_idx = i
            cat_name = "Filmes"
            break
        elif p_lower in ("series", "serie", "tv"):
            cat_idx = i
            cat_name = "Series"
            break
        elif p_lower in ("porno", "adult", "xxx"):
            cat_idx = i
            cat_name = "Porno"
            break

    if cat_idx is None:
        return None, None, []

    remaining = parts[cat_idx + 1:]
    if not remaining:
        return None, None, []

    file_name = remaining[-1]
    sub_dirs = remaining[:-1]

    parent = f"{user_root}/{cat_name}"
    dirs_to_create = []
    for sd in sub_dirs:
        if not sd or is_hash_name(sd):
            continue
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

# Build ordered media items in EXACT upload order (by drive, then alphabetical within drive)
all_media_items = []
for x in state_items:
    if x.endswith(".strm"):
        continue
    norm = normalize_path(x)
    stem = os.path.splitext(os.path.basename(norm))[0]
    if "staging/" in norm or "nebulastage/" in norm or is_hash_name(stem):
        continue
    ext = os.path.splitext(norm)[1].lower()
    if ext not in {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".sub", ".ass", ".ssa", ".vtt", ".srt"}:
        continue
    all_media_items.append(x)

# Determine processing order from state file (first occurrence of each drive+cat)
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
    if key not in by_source:
        by_source[key] = []
    by_source[key].append(x)

# Build ordered list (sorted alphabetically within each source group)
ordered_media_items = []
for key in source_order:
    items = sorted(by_source.get(key, []), key=normalize_path)
    ordered_media_items.extend(items)

print(f"Ordered state media items: {len(ordered_media_items)}")

# Load MongoDB docs sorted by first tg_message
docs = list(files_col.find({"type": "file"}))
docs_by_msg = sorted(
    [d for d in docs if d.get("parts") and d["parts"][0].get("tg_message")],
    key=lambda d: d["parts"][0]["tg_message"]
)
print(f"MongoDB docs sortable by msg: {len(docs_by_msg)}")

limit = min(len(ordered_media_items), len(docs_by_msg))

# Find all MISMATCHES
mismatches = []
correct = 0

for i in range(limit):
    state_path = ordered_media_items[i]
    doc = docs_by_msg[i]
    
    state_stem = os.path.splitext(os.path.basename(state_path))[0].lower().strip()
    db_stem = os.path.splitext(doc.get("name", ""))[0].lower().strip()
    
    if state_stem != db_stem:
        parent_dir, file_name, dirs_to_create = parse_parent_and_file(state_path, user_root)
        if parent_dir and file_name:
            mismatches.append({
                "idx": i,
                "doc_id": str(doc["_id"]),
                "current_name": doc.get("name", ""),
                "current_parent": doc.get("parent", ""),
                "correct_name": file_name,
                "correct_parent": parent_dir,
                "dirs_to_create": dirs_to_create,
                "size_mb": doc.get("size", 0) / (1024*1024),
                "first_tg_msg": doc["parts"][0]["tg_message"]
            })
    else:
        correct += 1

print(f"\nCorrect titles already: {correct}")
print(f"Titles that need fixing: {len(mismatches)}")

if not mismatches:
    print("✅ All titles are already correct!")
    sys.exit(0)

print(f"\nSample mismatches:")
for m in mismatches[:15]:
    print(f"  [{m['idx']}] msg={m['first_tg_msg']}")
    print(f"    CURRENT: {m['current_parent']}/{m['current_name']}")
    print(f"    CORRECT: {m['correct_parent']}/{m['correct_name']}")
    print()

# Save report
with open("title_fix_plan.json", "w", encoding="utf-8") as f:
    json.dump(mismatches, f, ensure_ascii=False, indent=2)
print(f"Fix plan saved to title_fix_plan.json")
print(f"\nTo apply: run this script with --apply flag")

if "--apply" in sys.argv:
    print("\n=== APPLYING FIXES ===")
    
    # Two-pass approach: use temp names to avoid duplicate key errors
    from bson import ObjectId
    
    fixed = 0
    errors = []
    
    # Pass 1: rename all mismatch docs to temp names
    for m in mismatches:
        try:
            files_col.update_one(
                {"_id": ObjectId(m["doc_id"])},
                {"$set": {"name": f"__TEMP__{m['doc_id']}", "mtime": now}}
            )
        except Exception as e:
            errors.append(f"Pass1 temp rename {m['doc_id']}: {e}")

    print(f"Pass 1: Renamed {len(mismatches)} docs to temp names")

    # Ensure all needed directories exist
    for m in mismatches:
        for dir_name, dir_parent in m["dirs_to_create"]:
            try:
                files_col.update_one(
                    {"name": dir_name, "parent": dir_parent},
                    {"$set": {"type": "dir", "ctime": now, "mtime": now, "size": 0}},
                    upsert=True
                )
            except Exception as e:
                errors.append(f"Dir create {dir_parent}/{dir_name}: {e}")

    # Pass 2: rename from temp to correct name
    for m in mismatches:
        try:
            files_col.update_one(
                {"_id": ObjectId(m["doc_id"])},
                {"$set": {
                    "name": m["correct_name"],
                    "parent": m["correct_parent"],
                    "mtime": now
                }}
            )
            fixed += 1
        except Exception as e:
            errors.append(f"Pass2 final rename {m['doc_id']}: {e}")

    print(f"Pass 2: Applied {fixed} correct names")

    if errors:
        print(f"\nErrors ({len(errors)}):")
        for e in errors[:10]:
            print(f"  {e}")

    # Clean orphaned dirs
    deleted_dirs = 0
    categories_to_keep = {"Filmes", "Series", "Porno"}
    while True:
        all_dirs = list(files_col.find({"type": "dir"}))
        cleaned = 0
        for d in all_dirs:
            if d.get("name") in categories_to_keep and d.get("parent") == user_root:
                continue
            dir_path = f"{d.get('parent','')}/{d.get('name','')}"
            if files_col.count_documents({"parent": dir_path}) == 0:
                files_col.delete_one({"_id": d["_id"]})
                cleaned += 1
                deleted_dirs += 1
        if cleaned == 0:
            break
    print(f"Cleaned {deleted_dirs} empty directories.")
    print(f"\n✅ Done! Fixed {fixed} titles, {len(errors)} errors.")
