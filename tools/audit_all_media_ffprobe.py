"""Auditoria incremental de toda a biblioteca NebulaFTP com ffprobe.

O script e somente leitura: consulta o MongoDB e abre os arquivos pela montagem N:.
Cada resultado e salvo imediatamente em JSONL, permitindo retomar a varredura.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any

import pymongo
from dotenv import dotenv_values


VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov",
    ".mp4", ".mpeg", ".mpg", ".mts", ".ts", ".vob", ".webm", ".wmv",
}
EPISODE_RE = re.compile(
    r"(?i)(?:\bS\d{1,2}E\d{1,3}\b|\b\d{1,2}x\d{1,3}\b|"
    r"\b(?:ep|episode|episodio|capitulo)[ ._-]*\d{1,3}\b)"
)


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def load_cached(path: Path) -> dict[str, dict[str, Any]]:
    cached: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return cached
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            key = item.get("mongo_id")
            if key:
                cached[key] = item
    return cached


def mounted_path(parent: str, name: str, drive: str) -> Path:
    parts = [part for part in parent.replace("\\", "/").split("/") if part]
    if parts and parts[0].casefold() == "raphael":
        parts = parts[1:]
    return Path(drive + "\\", *parts, name)


def probe_one(
    doc: dict[str, Any],
    ffprobe: Path,
    drive: str,
    timeout: int,
    http_base: str | None,
) -> dict[str, Any]:
    path = mounted_path(doc.get("parent", ""), doc.get("name", ""), drive)
    parts = sorted(doc.get("parts") or [], key=lambda p: p.get("part_id", 10**9))
    base: dict[str, Any] = {
        "mongo_id": str(doc["_id"]),
        "obfuscated_id": doc.get("obfuscated_id"),
        "mongo_parent": doc.get("parent", ""),
        "mongo_name": doc.get("name", ""),
        "mounted_path": str(path),
        "mongo_size": doc.get("size"),
        "part_count": len(parts),
        "first_tg_message": parts[0].get("tg_message") if parts else None,
        "probed_at": int(time.time()),
    }
    input_source = (
        f"{http_base.rstrip('/')}/stream?id={urllib.parse.quote(str(doc['_id']))}"
        if http_base
        else str(path)
    )
    base["probe_source"] = "nebula_http" if http_base else "mounted_drive"
    command = [
        str(ffprobe), "-v", "error", "-show_format", "-show_streams",
        "-of", "json", input_source,
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        base["probe_seconds"] = round(time.monotonic() - started, 3)
        if completed.returncode:
            base["probe_error"] = completed.stderr.strip()[-1000:] or f"exit={completed.returncode}"
            return base
        payload = json.loads(completed.stdout)
    except subprocess.TimeoutExpired:
        base["probe_seconds"] = round(time.monotonic() - started, 3)
        base["probe_error"] = f"timeout_after_{timeout}s"
        return base
    except Exception as exc:  # noqa: BLE001 - registrar e continuar o lote
        base["probe_seconds"] = round(time.monotonic() - started, 3)
        base["probe_error"] = f"{type(exc).__name__}: {exc}"
        return base

    fmt = payload.get("format") or {}
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
    tags = fmt.get("tags") or {}
    duration_raw = fmt.get("duration") or video.get("duration")
    try:
        duration = round(float(duration_raw), 3) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration = None

    base.update(
        {
            "duration_seconds": duration,
            "format_name": fmt.get("format_name"),
            "probe_size": int(fmt["size"]) if str(fmt.get("size", "")).isdigit() else None,
            "format_tags": tags,
            "video_codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "video_tags": video.get("tags") or {},
            "audio_codecs": [s.get("codec_name") for s in audio],
            "audio_languages": [(s.get("tags") or {}).get("language") for s in audio],
            "subtitle_languages": [(s.get("tags") or {}).get("language") for s in subtitles],
            "stream_count": len(streams),
            "audio_count": len(audio),
            "subtitle_count": len(subtitles),
        }
    )
    return base


def reasons(item: dict[str, Any]) -> list[str]:
    result: list[str] = []
    if item.get("probe_error"):
        result.append("falha_ffprobe")
        return result
    parent = item.get("mongo_parent", "")
    name = item.get("mongo_name", "")
    duration = item.get("duration_seconds") or 0
    if "/Filmes" in parent and 0 < duration < 45 * 60:
        result.append("filme_com_menos_de_45_min")
    if "/Series" in parent and duration > 100 * 60:
        result.append("episodio_com_mais_de_100_min")
    if "/Filmes" in parent and EPISODE_RE.search(name):
        result.append("padrao_de_episodio_em_filmes")
    internal_titles: list[str] = []
    for tags_key in ("format_tags", "video_tags"):
        tags = item.get(tags_key) or {}
        for key in ("title", "show", "movie_name", "episode_id", "episode_sort", "description"):
            if tags.get(key) not in (None, ""):
                internal_titles.append(str(tags[key]))
    path_norm = normalize(parent + " " + name)
    conflicts = []
    for title in internal_titles:
        title_norm = normalize(title)
        if len(title_norm) >= 4 and title_norm not in path_norm and path_norm not in title_norm:
            conflicts.append(title)
    if conflicts:
        result.append("titulo_interno_divergente")
        item["conflicting_internal_titles"] = conflicts
    if not item.get("video_codec"):
        result.append("sem_stream_de_video")
    if item.get("probe_size") and item.get("mongo_size") and item["probe_size"] != item["mongo_size"]:
        result.append("tamanho_mongo_diverge_do_ffprobe")
    return result


def write_reports(items: list[dict[str, Any]], output_dir: Path) -> None:
    for item in items:
        item["suspect_reasons"] = reasons(item)
    items.sort(key=lambda item: (item.get("mongo_parent", ""), item.get("mongo_name", "")))
    suspects = [item for item in items if item["suspect_reasons"]]
    (output_dir / "ffprobe_inventory.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "ffprobe_suspects.json").write_text(
        json.dumps(suspects, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    category_counts = Counter()
    reason_counts = Counter()
    for item in items:
        parent = item.get("mongo_parent", "")
        category = next((part for part in ("Filmes", "Series", "Porno") if f"/{part}" in parent), "Outros")
        category_counts[category] += 1
        reason_counts.update(item["suspect_reasons"])

    lines = [
        "# Auditoria FFmpeg/FFprobe da biblioteca N:",
        "",
        f"- Arquivos de vídeo inventariados: {len(items)}",
        f"- Arquivos com ao menos um sinal de inconsistência: {len(suspects)}",
        f"- Falhas de leitura: {reason_counts.get('falha_ffprobe', 0)}",
        "",
        "## Contagem por categoria",
        "",
    ]
    lines.extend(f"- {name}: {count}" for name, count in sorted(category_counts.items()))
    lines.extend(["", "## Sinais encontrados", ""])
    lines.extend(f"- {name}: {count}" for name, count in reason_counts.most_common())
    lines.extend(["", "## Observação", "", "Sinais automáticos indicam prioridade de revisão; renomeações só devem usar identificação de conteúdo confirmada.", ""])
    (output_dir / "ffprobe_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffprobe", required=True, type=Path)
    parser.add_argument("--env", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / "media_audit")
    parser.add_argument("--drive", default="N:")
    parser.add_argument("--http-base", help="Ex.: http://127.0.0.1:2122")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    cache_path = args.output / "ffprobe_cache.jsonl"
    progress_path = args.output / "ffprobe_progress.json"
    env = dotenv_values(args.env)
    client = pymongo.MongoClient(env.get("MONGODB", "mongodb://localhost:27017"), serverSelectionTimeoutMS=5000)
    db = client[env.get("MONGO_DATABASE", "ftp")]
    docs = list(db.files.find({"type": "file"}))
    docs = [doc for doc in docs if Path(doc.get("name", "")).suffix.casefold() in VIDEO_EXTENSIONS]
    docs.sort(key=lambda doc: (doc.get("parent", ""), doc.get("name", "")))

    cached = load_cached(cache_path)
    pending = []
    for doc in docs:
        key = str(doc["_id"])
        if (
            key in cached
            and cached[key].get("obfuscated_id") == doc.get("obfuscated_id")
            and cached[key].get("mongo_size") == doc.get("size")
        ):
            # Atualiza parent, name e mounted_path com o estado atual do banco
            cached[key]["mongo_parent"] = doc.get("parent", "")
            cached[key]["mongo_name"] = doc.get("name", "")
            cached[key]["mounted_path"] = str(mounted_path(doc.get("parent", ""), doc.get("name", ""), args.drive))
        else:
            pending.append(doc)
    print(f"total={len(docs)} cached={len(docs) - len(pending)} pending={len(pending)}", flush=True)

    completed_count = len(docs) - len(pending)
    with cache_path.open("a", encoding="utf-8", buffering=1) as cache_file:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(
                    probe_one, doc, args.ffprobe, args.drive, args.timeout, args.http_base
                ): doc
                for doc in pending
            }
            for future in concurrent.futures.as_completed(futures):
                item = future.result()
                cached[item["mongo_id"]] = item
                cache_file.write(json.dumps(item, ensure_ascii=False) + "\n")
                completed_count += 1
                if completed_count % 10 == 0 or completed_count == len(docs):
                    progress = {
                        "status": "running" if completed_count < len(docs) else "complete",
                        "total": len(docs),
                        "completed": completed_count,
                        "pending": len(docs) - completed_count,
                        "errors": sum(bool(value.get("probe_error")) for value in cached.values()),
                        "updated_at": int(time.time()),
                    }
                    progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(json.dumps(progress, ensure_ascii=False), flush=True)

    current = [cached[str(doc["_id"])] for doc in docs if str(doc["_id"]) in cached]
    write_reports(current, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
