import json
import os
import sys
import re
import time
import urllib.request
import concurrent.futures
import pymongo
from bson import ObjectId

sys.stdout.reconfigure(encoding='utf-8')

LM_STUDIO_URL = "http://127.0.0.1:1234/v1/chat/completions"
MODEL_ID = "huihui-qwen3.5-9b-abliterated"

def safe_component(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]+', " - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value[:180]

def process_one_item(item, doc):
    tags = item.get("format_tags") or {}
    vtags = item.get("video_tags") or {}
    all_tags = {**tags, **vtags}
    raw_titles = [str(val).strip() for k, val in all_tags.items() if k in ("title", "show", "movie_name", "comment") and val]

    title_text = " ".join(raw_titles)
    dur_min = (item.get("duration_seconds") or 0) / 60.0

    if not title_text.strip():
        return None

    prompt = f"""Analise as informações do vídeo e identifique o nome do filme ou série em português:
Tag do Stream: "{title_text}"
Nome Atual do Arquivo: "{doc['name']}"
Duração: {dur_min:.1f} minutos

Responda no formato:
Filme: Nome do Filme (Ano)
Série: Nome da Série - SXXEYY
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
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            ai_out = msg.get("content", "").strip() or msg.get("reasoning_content", "").strip()
            
            # Parse Movie (YYYY) or Series SXXEYY
            ep_match = re.search(r"(?i)\bS(\d{1,2})[ ._-]*E(\d{1,3})\b", ai_out)
            if ep_match:
                lines = [l for l in ai_out.splitlines() if "S" in l and "E" in l]
                target_str = lines[-1] if lines else ai_out
                show = safe_component(target_str[: ep_match.start()].strip(" -_.:"))
                season, episode = int(ep_match.group(1)), int(ep_match.group(2))
                if show:
                    ext = os.path.splitext(doc["name"])[1] or ".mp4"
                    return {
                        "mongo_id": item["mongo_id"],
                        "curr": f"{doc['parent']}/{doc['name']}",
                        "desired_parent": f"/raphael/Series/{show}/Season {season:02d}",
                        "desired_name": f"{show} - S{season:02d}E{episode:02d}{ext}",
                        "ai_out": ai_out[:80]
                    }

            year_match = re.search(r"\b((?:19|20)\d{2})\b", ai_out)
            if year_match and dur_min >= 35.0:
                lines = [l for l in ai_out.splitlines() if "(" in l or ")" in l]
                target_str = lines[-1] if lines else ai_out
                movie = safe_component(target_str[: year_match.start()].strip(" -_.:("))
                year = int(year_match.group(1))
                if movie:
                    ext = os.path.splitext(doc["name"])[1] or ".mkv"
                    canonical = f"{movie} ({year})"
                    return {
                        "mongo_id": item["mongo_id"],
                        "curr": f"{doc['parent']}/{doc['name']}",
                        "desired_parent": f"/raphael/Filmes/{canonical}",
                        "desired_name": f"{canonical}{ext}",
                        "ai_out": ai_out[:80]
                    }
    except Exception as e:
        pass

    return None

def main():
    apply = "--apply" in sys.argv
    inventory_path = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/http_probe/ffprobe_inventory.json"
    with open(inventory_path, "r", encoding="utf-8") as f:
        items = json.load(f)

    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    files_col = db.files
    docs = {str(d["_id"]): d for d in files_col.find({"type": "file"})}

    print(f"Loaded {len(items)} items from inventory. Querying Local AI ({MODEL_ID})...")

    results = []
    valid_items = [it for it in items if it["mongo_id"] in docs][:100]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(process_one_item, it, docs[it["mongo_id"]]): it for it in valid_items}
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                results.append(res)
                print(f"AI MATCH: {res['curr']} -> {res['desired_parent']}/{res['desired_name']}")

    print(f"\nTotal Local AI Realignments: {len(results)}")

if __name__ == "__main__":
    main()
