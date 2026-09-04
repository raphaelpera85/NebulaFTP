"""Extrai um quadro pelo HTTP local do Nebula e identifica cenas de anime.

Somente leitura no MongoDB/Nebula. Os resultados sao persistidos em JSONL para
retomada. O quadro e enviado ao servico trace.moe, especializado em anime.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pymongo
from dotenv import dotenv_values


def load_cache(path: Path) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("mongo_id"):
            found[item["mongo_id"]] = item
    return found


def compact_result(item: dict[str, Any]) -> dict[str, Any]:
    anime = item.get("anilist") or {}
    titles = anime.get("title") or {}
    return {
        "anilist_id": anime.get("id"),
        "title_english": titles.get("english"),
        "title_romaji": titles.get("romaji"),
        "title_native": titles.get("native"),
        "episode": item.get("episode"),
        "similarity": item.get("similarity"),
        "matched_filename": item.get("filename"),
        "matched_at": item.get("at"),
        "matched_duration": item.get("duration"),
        "is_adult": anime.get("isAdult"),
        "site_url": anime.get("siteUrl"),
    }


def identify_frame(frame: Path, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        "https://api.trace.moe/search?anilistInfo",
        data=frame.read_bytes(),
        headers={"Content-Type": "image/jpeg", "User-Agent": "NebulaMediaAudit/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    results = payload.get("result") or []
    return {
        "quota": payload.get("quota"),
        "quota_used": payload.get("quotaUsed"),
        "api_error": payload.get("error") or None,
        "matches": [compact_result(item) for item in results[:3]],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--query", required=True, help="Regex aplicada ao nome e ao parent do MongoDB")
    parser.add_argument("--env", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--http-base", default="http://127.0.0.1:2122")
    parser.add_argument("--timestamp", type=float, default=300.0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--pause", type=float, default=0.4)
    parser.add_argument("--only-low-from", type=Path)
    parser.add_argument("--confidence", type=float, default=0.90)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    frames_dir = args.output / "frames"
    frames_dir.mkdir(exist_ok=True)
    cache_path = args.output / "anime_recognition.jsonl"
    cached = load_cache(cache_path)

    env = dotenv_values(args.env)
    db = pymongo.MongoClient(env.get("MONGODB", "mongodb://localhost:27017"))[env.get("MONGO_DATABASE", "ftp")]
    expression = re.compile(args.query, re.IGNORECASE)
    docs = [
        doc for doc in db.files.find({"type": "file"})
        if expression.search(doc.get("name", "")) or expression.search(doc.get("parent", ""))
    ]
    if args.only_low_from:
        previous = json.loads(args.only_low_from.read_text(encoding="utf-8"))
        allowed = {
            item["mongo_id"]
            for item in previous
            if not item.get("matches")
            or float(item["matches"][0].get("similarity") or 0) < args.confidence
        }
        docs = [doc for doc in docs if str(doc["_id"]) in allowed]
    docs.sort(key=lambda doc: (doc.get("parent", ""), doc.get("name", "")))
    docs = docs[: args.limit]
    print(f"selected={len(docs)} cached={sum(str(doc['_id']) in cached for doc in docs)}", flush=True)

    with cache_path.open("a", encoding="utf-8", buffering=1) as handle:
        for index, doc in enumerate(docs, 1):
            mongo_id = str(doc["_id"])
            if mongo_id in cached:
                continue
            frame = frames_dir / f"{mongo_id}.jpg"
            url = f"{args.http_base.rstrip('/')}/stream?id={urllib.parse.quote(mongo_id)}"
            command = [
                str(args.ffmpeg), "-hide_banner", "-loglevel", "error",
                "-ss", str(args.timestamp), "-i", url, "-frames:v", "1",
                "-vf", "scale='min(960,iw)':-2", "-q:v", "3", "-y", str(frame),
            ]
            record: dict[str, Any] = {
                "mongo_id": mongo_id,
                "path": f"{doc.get('parent', '')}/{doc.get('name', '')}",
                "name": doc.get("name"),
                "parent": doc.get("parent"),
                "size": doc.get("size"),
                "timestamp": args.timestamp,
                "checked_at": int(time.time()),
            }
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=args.timeout,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if completed.returncode or not frame.exists():
                    record["extract_error"] = completed.stderr.strip()[-1000:] or f"exit={completed.returncode}"
                else:
                    record.update(identify_frame(frame, args.timeout))
            except Exception as exc:  # noqa: BLE001 - persistir a falha e continuar
                record["error"] = f"{type(exc).__name__}: {exc}"
            cached[mongo_id] = record
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            top = (record.get("matches") or [{}])[0]
            print(
                f"[{index}/{len(docs)}] {doc.get('name')} -> "
                f"{top.get('title_english') or top.get('title_romaji')} "
                f"ep={top.get('episode')} sim={top.get('similarity')}",
                flush=True,
            )
            time.sleep(max(0.0, args.pause))

    output = [cached[str(doc["_id"])] for doc in docs if str(doc["_id"]) in cached]
    (args.output / "anime_recognition.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
