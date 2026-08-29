"""
fix_titles_final.py — Reconstrução definitiva e correta.

FATO DESCOBERTO:
- D: = 115 itens (filmes e porno iniciais)
- E: = 1546 itens (filmes e porno do 2o drive)
- F: = 2188 itens (filmes e séries de um 3o drive, com 2096 EXCLUSIVOS)
- Total = 3849 itens válidos no state file

A ordem de upload foi: D:Filmes, D:Porno, E:Filmes, E:Porno, F:Filmes, F:Series
(na ordem de primeira ocorrência no state file)

Dentro de cada grupo, os arquivos foram enviados em ordem ALFABÉTICA (os.walk order).

AÇÃO:
1. Reconstruir ordered_media_items na ordem correta (sem deduplicação incorreta)
2. Deletar TUDO do MongoDB
3. Recriar cada doc com o nome certo (do state) e os chunks certos (do MongoDB por msg order)
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

# Build ALL raw media items (NO deduplication)
all_media_items = []
for x in state_items:
    if x.endswith(".strm"): continue
    norm = normalize_path(x)
    stem = os.path.splitext(os.path.basename(norm))[0]
    if "staging/" in norm or "nebulastage/" in norm or is_hash_name(stem): continue
    ext = os.path.splitext(norm)[1].lower()
    if ext not in {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".sub", ".ass", ".ssa", ".vtt", ".srt"}: continue
    all_media_items.append(x)

print(f"Total raw media items: {len(all_media_items)}")

# Determine source processing order from state file (first occurrence of each drive+cat)
source_order = []
seen = set()
for x in state_items:
    drv = get_drive(x)
    cat = get_category(x)
    key = f"{drv}:{cat}"
    if key not in seen:
        seen.add(key)
        source_order.append(key)

print(f"Source processing order: {source_order}")

# Group by drive+cat and sort alphabetically within each group
by_source = {}
for x in all_media_items:
    key = f"{get_drive(x)}:{get_category(x)}"
    if key not in by_source: by_source[key] = []
    by_source[key].append(x)

# Build ordered list
ordered_media_items = []
for key in source_order:
    items = sorted(by_source.get(key, []), key=normalize_path)
    print(f"  {key}: {len(items)} items")
    ordered_media_items.extend(items)

print(f"\nTotal ordered items: {len(ordered_media_items)}")

# Re-read original MongoDB docs from a fresh Telegram scan result
# Since we already partially rebuilt, we need to get the CORRECT set of docs with chunks
# The original complete set had 2743 docs. Let's rebuild from what's left.
# 
# IMPORTANT: We still have the full set in MongoDB sorted by tg_message from 
# the ORIGINAL complete database (before our partial rebuild).
# But our partial rebuild left only 420 docs!
#
# SOLUTION: Use the title_fix_plan.json we saved, which has the original doc_ids and chunk data
# OR: Use deep_verify_report.json which confirmed all chunks
# OR: We need to re-run restore_fast_single_bot.py to get back all chunks

# Check current DB state
current_docs = list(files_col.find({"type": "file"}))
print(f"\nCurrent MongoDB file docs: {len(current_docs)}")
print("NOTE: We have lost the full 2743 doc set - need to restore from Telegram")
print()
print("Run: python tools/restore_fast_single_bot.py")
print("Then run this script again")
