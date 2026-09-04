import json
import os
import sys
import re
import time
import urllib.request
import concurrent.futures
import pymongo
from pathlib import Path
from bson import ObjectId

sys.stdout.reconfigure(encoding='utf-8')

LM_STUDIO_URL = "http://127.0.0.1:1234/v1/chat/completions"
MODEL_ID = "huihui-qwen3.5-9b-abliterated"

def safe_component(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]+', " - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value[:180]

def query_local_ai_for_porno_doc(doc, item):
    fmt_tags = item.get("format_tags") or {}
    v_tags = item.get("video_tags") or {}
    all_tags = {**fmt_tags, **v_tags}
    raw_titles = [str(val).strip() for k, val in all_tags.items() if k in ("title", "show", "movie_name", "comment") and val]

    title_text = " ".join(raw_titles)
    dur_min = (item.get("duration_seconds") or 0) / 60.0

    prompt = f"""Analise este arquivo da biblioteca e identifique se ele é um filme principal, uma série de TV ou vídeo adulto:

- Nome Atual: "{doc['name']}"
- Tag do Stream: "{title_text}"
- Duração: {dur_min:.1f} minutos

Instruções:
1. Se for um filme comercial (duração > 40 min e nome/tag de filme), forneça: Nome do Filme (Ano). Exemplo: Dracula: A Ultima Viagem do Demeter (2023)
2. Se for uma série, forneça: Nome da Série - SXXEYY. Exemplo: Sons of Anarchy - S06E01
3. Se for vídeo adulto legítimo de clipe curto, mantenha a categoria Porno.

Responda no formato:
Mídia: Nome do Filme (Ano) OU Nome da Série - SXXEYY
"""

    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 150
    }

    req = urllib.request.Request(
        LM_STUDIO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            out = msg.get("content", "").strip() or msg.get("reasoning_content", "").strip()

            ep_match = re.search(r"(?i)\bS(\d{1,2})[ ._-]*E(\d{1,3})\b", out)
            if ep_match:
                show = safe_component(out[: ep_match.start()].strip(" -_.:Mídia:mídia:"))
                season, episode = int(ep_match.group(1)), int(ep_match.group(2))
                if show and len(show) >= 2:
                    ext = os.path.splitext(doc["name"])[1] or ".mp4"
                    return {
                        "mongo_id": str(doc["_id"]),
                        "curr": f"{doc['parent']}/{doc['name']}",
                        "desired_parent": f"/raphael/Series/{show}/Season {season:02d}",
                        "desired_name": f"{show} - S{season:02d}E{episode:02d}{ext}",
                        "ai_out": out[:80]
                    }

            year_match = re.search(r"\b((?:19|20)\d{2})\b", out)
            if year_match and dur_min >= 35.0:
                movie = safe_component(out[: year_match.start()].strip(" -_.:(Mídia:mídia:"))
                year = int(year_match.group(1))
                if movie and len(movie) >= 2:
                    ext = os.path.splitext(doc["name"])[1] or ".mkv"
                    canonical = f"{movie} ({year})"
                    return {
                        "mongo_id": str(doc["_id"]),
                        "curr": f"{doc['parent']}/{doc['name']}",
                        "desired_parent": f"/raphael/Filmes/{canonical}",
                        "desired_name": f"{canonical}{ext}",
                        "ai_out": out[:80]
                    }
    except Exception:
        pass

    return None

def ensure_parent_dirs(files_col, parent_path, now):
    user_root = "/raphael"
    if not parent_path.startswith(user_root): return
    rel = [p for p in parent_path[len(user_root):].strip("/").split("/") if p]
    curr = user_root
    for p in rel:
        files_col.update_one(
            {"type": "dir", "name": p, "parent": curr},
            {"$setOnInsert": {"type": "dir", "name": p, "parent": curr, "ctime": now, "mtime": now, "size": 0}},
            upsert=True
        )
        curr = f"{curr}/{p}"

def main():
    apply = "--apply" in sys.argv
    inventory_path = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/http_probe/ffprobe_inventory.json"
    with open(inventory_path, "r", encoding="utf-8") as f:
        items = json.load(f)

    inv_map = {x["mongo_id"]: x for x in items}

    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    files_col = db.files

    porno_pattern = re.compile(r"^/raphael/Porno")
    porno_docs = list(files_col.find({"type": "file", "parent": porno_pattern}))
    print(f"Loaded {len(porno_docs)} docs from /raphael/Porno. Querying LM Studio AI...")

    realignments = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(query_local_ai_for_porno_doc, d, inv_map.get(str(d["_id"]), {})): d for d in porno_docs}
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                realignments.append(res)
                print(f"  [LM STUDIO PORNO RE-ALIGN] {res['curr'][:45]} -> {res['desired_parent']}/{res['desired_name']}")

    print(f"\nMode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Total Local AI Realignments out of Porno: {len(realignments)}")

    if not apply or not realignments:
        return

    now = int(time.time())
    applied = 0
    quarantined = 0

    for r in realignments:
        ensure_parent_dirs(files_col, r["desired_parent"], now)
        try:
            files_col.update_one(
                {"_id": ObjectId(r["mongo_id"])},
                {"$set": {
                    "parent": r["desired_parent"],
                    "name": r["desired_name"],
                    "mtime": now
                }}
            )
            applied += 1
        except pymongo.errors.DuplicateKeyError:
            quar_parent = f"/raphael/Auditoria/Duplicatas/{r['desired_parent'].replace('/raphael/', '')}"
            stem = os.path.splitext(r["desired_name"])[0]
            ext = os.path.splitext(r["desired_name"])[1]
            quar_name = f"{stem}__{r['mongo_id'][-8:]}{ext}"
            ensure_parent_dirs(files_col, quar_parent, now)
            try:
                files_col.update_one(
                    {"_id": ObjectId(r["mongo_id"])},
                    {"$set": {
                        "parent": quar_parent,
                        "name": quar_name,
                        "mtime": now
                    }}
                )
                quarantined += 1
            except pymongo.errors.DuplicateKeyError:
                pass

    # Clean empty directories under Porno
    deleted = 0
    dirs = list(files_col.find({"type": "dir", "parent": porno_pattern}))
    for d in dirs:
        dir_path = f"{d['parent']}/{d['name']}"
        if files_col.count_documents({"parent": dir_path}) == 0:
            files_col.delete_one({"_id": d["_id"]})
            deleted += 1

    print(f"\nSUCCESS: Applied {applied} AI realignments out of Porno, quarantined {quarantined} duplicates, deleted {deleted} empty folders!")

if __name__ == "__main__":
    main()
