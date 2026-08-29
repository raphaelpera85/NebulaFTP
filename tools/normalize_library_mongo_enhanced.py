import os
import sys
import json
import pymongo
import re

sys.stdout.reconfigure(encoding='utf-8')

ROOT = f'/{os.getenv("NEBULA_LIBRARY_USER", "raphael")}/Filmes'

SPECIAL = {
    "0.H0m3m.d4s.Tr3v4s.2016.BDRip.XviD.Dual": "O Homem nas Trevas (2016)",
    "01 - A Maldição do Pérola Negra": "Piratas do Caribe - A Maldição do Pérola Negra (2003)",
    "Argentina.1985.2022.SPANISH.1080p.WEBRip.x265-VXT": "Argentina, 1985 (2022)",
    "A Família Addams": "A Família Addams (1991)",
    "A Mosca": "A Mosca (1986)",
    "A lagoa azul": "A Lagoa Azul (1980)",
    "Amizade Colorida": "Amizade Colorida (2011)",
    "Babe - O porquinho atrapalhado": "Babe - O Porquinho Atrapalhado (1995)",
    "Bicho de Sete Cabecas.Mp4.720p": "Bicho de Sete Cabeças (2001)",
    "Dual 2022 1080p BluRay DUAL 5.1": "Dual (2022)",
    "Minha Mae e uma Peca 3": "Minha Mãe é uma Peça 3 (2019)",
    "Minha Mãe é uma Peça": "Minha Mãe é uma Peça (2013)",
    "O Homem do Futuro": "O Homem do Futuro (2011)",
    "Sucker Punch Mundo Surreal": "Sucker Punch - Mundo Surreal (2011)",
    "Titanic 4k Matered Dublado - YTSBR.COM": "Titanic (1997)",
    "Ó Paí, Ó": "Ó Paí, Ó (2007)",
}

NOISE_PREFIX = re.compile(r"(?i)^(?:COMANDO\.TO\s*-\s*)")
YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
PAREN_YEAR = re.compile(r"\(((?:19|20)\d{2})\)")
TRAILING_EDITION = re.compile(
    r"(?ix)"
    r"\s*(?:"
    r"\[[^\]]+\]|"
    r"\(?\b(?:480p|720p|1080p|2160p|4k|full\s*hd|bluray|blu-ray|bdrip|brrip|"
    r"webrip|web-dl|web|dvdrip|hdrip|remux|xvid|x264|x265|h264|h265|hevc|"
    r"av1|aac|ac3|eac3|ddp\d*(?:\.\d)?|dual|dublado|legendado|nacional|"
    r"portuguese|spanish|danish|french|proper|remastered|imax|docu|"
    r"\d+(?:\.\d+)?ch|10bit|fullscreen)\b.*"
    r")$"
)

def clean_title(raw: str, year_start: int) -> str:
    title = NOISE_PREFIX.sub("", raw[:year_start]).strip(" ._-")
    title = re.sub(r"^\d{1,2}\.(?=[A-Za-zÀ-ÿ])", "", title)
    if "." in title and title.count(" ") < title.count("."):
        title = re.sub(r"(?<=\w)\.(?=\w)", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" ._-[]")
    return title

def normalized_movie_name(name: str, child_names: list[str]) -> str | None:
    if name in SPECIAL:
        return SPECIAL[name]
    if re.fullmatch(r".+ \((?:19|20)\d{2}\)", name):
        return name
    sources = [name, *child_names]
    for source in sources:
        paren = list(PAREN_YEAR.finditer(source))
        years = list(YEAR.finditer(source))
        match = paren[-1] if paren else (years[-1] if years else None)
        if not match:
            continue
        year = match.group(1)
        title = clean_title(name if YEAR.search(name) else source, match.start())
        title = TRAILING_EDITION.sub("", title).strip(" ._-")
        if title:
            return f"{title} ({year})"
    return None

def run_mongo_normalization(apply=False):
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    files_col = db.files

    dirs = list(files_col.find({"parent": ROOT, "type": "dir"}))
    existing_dir_names = {doc["name"] for doc in dirs}

    renames = []
    collisions = []

    for d in sorted(dirs, key=lambda x: x["name"].casefold()):
        old_name = d["name"]
        children = list(files_col.find({"parent": f"{ROOT}/{old_name}"}))
        child_names = [c["name"] for c in children]

        new_name = normalized_movie_name(old_name, child_names)
        if not new_name or new_name == old_name:
            continue

        if new_name in existing_dir_names and new_name != old_name:
            collisions.append((d, old_name, new_name, children))
            continue

        renames.append((d, old_name, new_name, children))
        existing_dir_names.remove(old_name)
        existing_dir_names.add(new_name)

    print(f"Total clean folder renames planned: {len(renames)}")
    print(f"Total collision mergers planned: {len(collisions)}")

    if not apply:
        print("\n[DRY-RUN MODE] Sample renames:")
        for _, old, new, _ in renames[:15]:
            print(f"  RENAME: '{old}' -> '{new}'")
        print("\n[DRY-RUN MODE] Sample collision resolutions:")
        for _, old, new, _ in collisions[:15]:
            print(f"  MERGE/QUARANTINE: '{old}' -> existing '{new}'")
        return

    print("\n[APPLY MODE] Executing renames and collision mergers directly in MongoDB...")
    count_dirs = 0
    count_files = 0
    count_quarantined = 0

    for dir_doc, old_name, new_name, children in renames:
        old_parent_path = f"{ROOT}/{old_name}"
        new_parent_path = f"{ROOT}/{new_name}"

        # Update Directory Doc
        files_col.update_one({"_id": dir_doc["_id"]}, {"$set": {"name": new_name}})
        count_dirs += 1

        video_children = [
            c for c in children
            if os.path.splitext(c["name"])[1].lower() in {".avi", ".m4v", ".mkv", ".mp4", ".mov", ".ts", ".wmv"}
        ]

        # Update Children Docs
        for c in children:
            c_name = c["name"]
            ext = os.path.splitext(c_name)[1].lower()

            if len(video_children) == 1 and c["_id"] == video_children[0]["_id"]:
                new_c_name = f"{new_name}{ext}"
            elif c_name.startswith(old_name):
                new_c_name = c_name.replace(old_name, new_name, 1)
            else:
                new_c_name = c_name

            files_col.update_one(
                {"_id": c["_id"]},
                {"$set": {"parent": new_parent_path, "name": new_c_name}}
            )
            count_files += 1

    # Handle collisions by moving files into target or quarantining duplicates
    audit_root = "/raphael/Auditoria/Duplicatas/Filmes"
    for dir_doc, old_name, new_name, children in collisions:
        old_parent_path = f"{ROOT}/{old_name}"
        new_parent_path = f"{ROOT}/{new_name}"
        existing_children = list(files_col.find({"parent": new_parent_path}))
        existing_sizes = {c.get("size") for c in existing_children if c.get("size")}

        for c in children:
            c_size = c.get("size")
            if c_size and c_size in existing_sizes:
                # Duplicate! Move to Auditoria
                quar_parent = f"{audit_root}/{new_name}"
                quar_name = f"{os.path.splitext(c['name'])[0]}__{str(c['_id'])[-8:]}{os.path.splitext(c['name'])[1]}"
                files_col.update_one(
                    {"_id": c["_id"]},
                    {"$set": {"parent": quar_parent, "name": quar_name}}
                )
                count_quarantined += 1
            else:
                # Unique file, move to target new_parent_path
                ext = os.path.splitext(c["name"])[1].lower()
                new_c_name = f"{new_name}_alt__{str(c['_id'])[-8:]}{ext}"
                files_col.update_one(
                    {"_id": c["_id"]},
                    {"$set": {"parent": new_parent_path, "name": new_c_name}}
                )
                count_files += 1

        # Delete the redundant directory record
        files_col.delete_one({"_id": dir_doc["_id"]})

    print(f"SUCCESS: Renamed {count_dirs} folders, moved {count_files} files, quarantined {count_quarantined} duplicates!")

if __name__ == "__main__":
    apply_flag = "--apply" in sys.argv
    run_mongo_normalization(apply=apply_flag)
