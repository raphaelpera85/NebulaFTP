from __future__ import annotations

import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor
import hashlib
import mimetypes
import json
import os
import queue
import re
import shutil
import socket
import tempfile
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

UPLOADABLE_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v",
    ".sub", ".ass", ".ssa", ".vtt",
}
MONITORED_EXTENSIONS = UPLOADABLE_EXTENSIONS | {".strm"}
ACTIVE_STATUSES = ("staging", "queued", "uploading")
EPISODE_RE = re.compile(r"(?i)(?P<prefix>.*?)(?:[.\s_-]+)?s(?P<season>\d{1,2})[.\s_-]*e(?P<episode>\d{1,3})")
INCOMPLETE_RE = re.compile(
    r"(?i)(?P<download>.+\.download)(?:\.part\d+)?$|.+\.(?:partial|crdownload|aria2|tmp)$"
)

# Priority order for category processing
CATEGORY_PRIORITY = ["filmes", "porno", "series"]


@dataclass
class Stats:
    queued: int = 0
    copied: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_copied: int = 0


def is_monitored(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in {".download", ".partial"}:
        return False
    return suffix in MONITORED_EXTENSIONS


def extract_media_year(name: str, parent_name: str = "") -> int:
    """Extrai o ano da midia (1900-2099) a partir do nome do arquivo e/ou pasta."""
    for text in (name, parent_name):
        if not text:
            continue
        bracket_matches = re.findall(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]", text)
        if bracket_matches:
            return int(bracket_matches[-1])
        delim_matches = re.findall(r"(?:^|[.\s_\-\(\[])((?:19|20)\d{2})(?:$|[.\s_\-\)\]])", text)
        if delim_matches:
            return int(delim_matches[-1])
        word_matches = re.findall(r"\b((?:19|20)\d{2})\b", text)
        if word_matches:
            return int(word_matches[-1])
    return 0


def get_category_from_path(src: Path, source_root: Path) -> str | None:
    """Extract category (filmes/porno/series) from file path relative to source root."""
    try:
        rel = src.relative_to(source_root)
        parts = [p.lower() for p in rel.parts]
        for p in parts:
            if any(k in p for k in ["filme", "movie"]):
                return "filmes"
            if any(k in p for k in ["porno", "porn", "xxx", "hentai", "adulto"]):
                return "porno"
            if any(k in p for k in ["serie", "tv", "show", "anime", "season"]):
                return "series"
    except ValueError:
        pass
    src_str = str(src).lower()
    if any(k in src_str for k in ["\\filmes\\", "/filmes/", "filme"]):
        return "filmes"
    if any(k in src_str for k in ["\\porno\\", "/porno/", "porn", "xxx", "adulto"]):
        return "porno"
    if any(k in src_str for k in ["\\series\\", "/series/", "serie", "season"]):
        return "series"
    return None


def iter_files_by_priority(sources: list[Path], all_files: bool, exclude_dirs: set[str]):
    """Yield files grouped by priority: Filmes (ano decrescente: 2026 -> 2025 -> ...) -> Porno -> Series."""
    # Collect all files first with their category
    categorized: dict[str, list[tuple[Path, Path]]] = {cat: [] for cat in CATEGORY_PRIORITY}
    categorized["other"] = []
    
    for source in sources:
        for root, _, files in os.walk(source):
            root_path = Path(root)
            rel_parts = root_path.relative_to(source).parts
            if rel_parts and rel_parts[0].lower() in exclude_dirs:
                continue
            for name in files:
                src = root_path / name
                if src.suffix.lower() in {".download", ".partial"}:
                    continue
                if all_files or is_monitored(src):
                    cat = get_category_from_path(src, source)
                    if cat and cat in CATEGORY_PRIORITY:
                        categorized[cat].append((source, src))
                    else:
                        categorized["other"].append((source, src))
    
    # Ordenar filmes por ano da midia em ordem decrescente (ex: 2026 -> 2025 -> 2024 -> ...)
    categorized["filmes"].sort(
        key=lambda item: (
            -extract_media_year(item[1].name, item[1].parent.name),
            item[1].name.lower()
        )
    )

    # Yield in priority order
    for cat in CATEGORY_PRIORITY:
        for item in categorized[cat]:
            yield item
    for item in categorized["other"]:
        yield item


def split_source_paths(raw_sources: list[str] | None) -> list[Path]:
    raw_sources = raw_sources or ["D:/midias"]
    paths: list[Path] = []
    seen: set[str] = set()
    for raw in raw_sources:
        for chunk in re.split(r"[;\n,]", raw):
            value = chunk.strip().strip('"')
            if not value:
                continue
            resolved = Path(value).expanduser().resolve()
            key = str(resolved).lower()
            if key in seen:
                continue
            seen.add(key)
            paths.append(resolved)
    return paths


def iter_files(sources: list[Path], all_files: bool, exclude_dirs: set[str]):
    for source in sources:
        for root, _, files in os.walk(source):
            root_path = Path(root)
            rel_parts = root_path.relative_to(source).parts
            if rel_parts and rel_parts[0].lower() in exclude_dirs:
                continue
            for name in files:
                src = root_path / name
                if src.suffix.lower() in {".download", ".partial"}:
                    continue
                if all_files or is_monitored(src):
                    yield source, src


def cleanup_stale_downloads(sources: list[Path], max_age_seconds: int = 1800) -> tuple[int, int]:
    cutoff = time.time() - max_age_seconds
    groups: dict[tuple[Path, str], list[Path]] = {}
    for source in sources:
        if not source.exists():
            continue
        for root, _, files in os.walk(source):
            parent = Path(root)
            for name in files:
                match = INCOMPLETE_RE.fullmatch(name)
                if not match:
                    continue
                key = match.group("download") or name
                groups.setdefault((parent, key.lower()), []).append(parent / name)

    removed = 0
    released = 0
    for (parent, key), paths in groups.items():
        has_completed_media = False
        sample_name = paths[0].name
        clean_name = sample_name.lstrip(".")
        stem_guess = clean_name.split(".download")[0]
        if "." in stem_guess:
            stem_parts = stem_guess.rsplit(".", 1)
            if len(stem_parts) == 2 and len(stem_parts[1]) >= 4:
                stem_guess = stem_parts[0]
        for ext in UPLOADABLE_EXTENSIONS:
            if (parent / f"{stem_guess}{ext}").exists():
                has_completed_media = True
                break

        try:
            if not has_completed_media and max(path.stat().st_mtime for path in paths) >= cutoff:
                continue
        except FileNotFoundError:
            continue
        for path in paths:
            try:
                released += path.stat().st_size
                path.unlink(missing_ok=True)
                removed += 1
            except FileNotFoundError:
                pass
    return removed, released


def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_seen(path: Path, seen: set[str]) -> None:
    atomic_write_json(path, sorted(seen))


def load_materialized_links(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def save_materialized_links(path: Path, links: dict[str, str]) -> None:
    atomic_write_json(path, links, sort_keys=True)


def atomic_write_json(path: Path, value, *, sort_keys: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=sort_keys),
            encoding="utf-8",
        )
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def mongo_database(client):
    name = os.getenv("MONGO_DATABASE", "ftp")
    try:
        return client[name]
    except (AttributeError, TypeError):
        return getattr(client, name)


def mark_seen(src: Path, seen: set[str] | None, state_file: Path | None) -> None:
    if seen is None:
        return
    seen.add(str(src))
    if state_file:
        save_seen(state_file, seen)


def read_strm_url(src: Path) -> str:
    text = src.read_text(encoding="utf-8-sig", errors="replace")
    for line in text.splitlines():
        value = line.strip()
        if value:
            return value
    raise ValueError(f"Arquivo .strm vazio: {src}")


def guess_media_extension(url: str, content_type: str | None = None) -> str:
    ext = Path(urlsplit(url).path).suffix.lower()
    if ext and len(ext) <= 8:
        return ext
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip().lower())
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    return ".mp4"


def remote_content_size(url: str) -> int | None:
    request = Request(url, headers={"Range": "bytes=0-0", "User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30) as response:
        match = re.match(r"bytes\s+\d+-\d+/(\d+)", response.headers.get("Content-Range", ""))
        if match:
            return int(match.group(1))
        length = response.headers.get("Content-Length")
        return int(length) if length and response.status != 206 else None


def get_free_bytes(path: Path | str) -> int:
    """Retorna os bytes livres no disco correspondente ao caminho."""
    try:
        p = Path(path).resolve()
        while not p.exists() and p.parent != p:
            p = p.parent
        return shutil.disk_usage(p).free
    except Exception:
        return 0


def get_best_staging_root(
    stage_roots: list[Path] | None = None,
    required_bytes: int | None = None,
) -> Path:
    """Retorna o diretorio raiz de staging por ordem de prioridade/velocidade com espaco livre suficiente."""
    if not stage_roots:
        stage_dirs = os.getenv("STAGING_DIRS", os.getenv("STAGING_DIR", "staging"))
        stage_roots = [
            Path(p.strip()).resolve()
            for p in stage_dirs.split(";")
            if p.strip()
        ]
    if not stage_roots:
        return Path("staging").resolve()

    min_free_percent = min(max(int(os.getenv("STRM_MIN_FREE_PERCENT", "10")), 1), 90)

    # 1. Tenta o disco mais rapido na ordem configurada (ex: E: SSD -> F: USB3 -> I:) que tenha espaco livre seguro
    for root in stage_roots:
        try:
            p = root
            while not p.exists() and p.parent != p:
                p = p.parent
            usage = shutil.disk_usage(p)
            reserve = usage.total * min_free_percent // 100
            min_free = max(5 * 1024**3, reserve)  # Minimo de 5GB ou percentual de reserva
            if usage.free - (required_bytes or 0) >= min_free:
                return root
        except Exception:
            continue

    # 2. Fallback: Se os discos mais rapidos estiverem cheios, usa o de maior espaco livre absoluto
    return max(stage_roots, key=get_free_bytes)


def wait_for_disk_capacity(target: Path, required_bytes: int | None, minimum_free_percent: int = 10) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    last_notice = 0.0
    while True:
        usage = shutil.disk_usage(target.parent)
        reserve = usage.total * minimum_free_percent // 100
        if usage.free - (required_bytes or 0) >= reserve:
            return
        now = time.time()
        if now - last_notice >= 30:
            print(
                f"[STRM] Disco aguardando espaco: livre={usage.free / 1024**3:.1f} GB "
                f"reserva={minimum_free_percent}%",
                flush=True,
            )
            last_notice = now
        time.sleep(10)


def materialize_strm(src: Path, overwrite: bool = False, target: Path | None = None) -> Path:
    url = read_strm_url(src)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"URL .strm invalida: {url}")

    target = target or src.with_name(f"{src.stem}{guess_media_extension(url)}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        for old_temp in target.parent.glob(f".{src.stem}.*.download*"):
            with contextlib.suppress(Exception):
                old_temp.unlink(missing_ok=True)
        for old_temp in target.parent.glob(f".{src.stem}.download*"):
            with contextlib.suppress(Exception):
                old_temp.unlink(missing_ok=True)
        return target

    read_timeout = max(5, int(os.getenv("STRM_READ_TIMEOUT", "20")))
    socket.setdefaulttimeout(read_timeout)
    tmp_path = target.parent / f".{src.stem}.download"
    part_paths: list[Path] = []
    try:
        probe = Request(url, headers={"Range": "bytes=0-0", "User-Agent": "Mozilla/5.0"})
        with urlopen(probe, timeout=read_timeout) as response:
            content_range = response.headers.get("Content-Range", "")
            match = re.match(r"bytes\s+0-0/(\d+)", content_range)
            if not match:
                with tmp_path.open("wb") as out:
                    shutil.copyfileobj(response, out, length=1024 * 1024)
                total = tmp_path.stat().st_size
            else:
                total = int(match.group(1))
                parts_count = min(max(int(os.getenv("STRM_DOWNLOAD_PARTS", "20")), 1), 32)
                part_size = (total + parts_count - 1) // parts_count
                part_paths = [Path(f"{tmp_path}.part{index}") for index in range(parts_count)]
                progress_lock = threading.Lock()
                progress = {"downloaded": 0, "next_percent": 1, "last_logged_bytes": 0}

                # Verificar partes existentes válidas para retomada (Resume)
                for index, p_path in enumerate(part_paths):
                    start = index * part_size
                    end = min(start + part_size, total) - 1
                    expected = end - start + 1
                    if p_path.exists():
                        if p_path.stat().st_size == expected:
                            progress["downloaded"] += expected
                        else:
                            p_path.unlink(missing_ok=True)

                if progress["downloaded"] > 0:
                    percent = int(progress["downloaded"] * 100 / total)
                    print(
                        f"[STRM] Resumindo {src.name}: {progress['downloaded'] / 1024 / 1024:.1f} MB "
                        f"de {total / 1024 / 1024:.1f} MB ({percent}%) ja no disco",
                        flush=True,
                    )
                    progress["next_percent"] = percent + 1
                    progress["last_logged_bytes"] = progress["downloaded"]

                read_timeout = max(5, int(os.getenv("STRM_READ_TIMEOUT", "20")))

                def download_part(index):
                    start = index * part_size
                    end = min(start + part_size, total) - 1
                    expected = end - start + 1
                    part_file = part_paths[index]

                    if part_file.exists() and part_file.stat().st_size == expected:
                        print(f"[STRM] Parte {index + 1}/{parts_count} reutilizada (ja baixada)", flush=True)
                        return

                    part_file.unlink(missing_ok=True)
                    part_tmp = Path(f"{part_file}.tmp")
                    part_tmp.unlink(missing_ok=True)

                    retries = max(1, int(os.getenv("STRM_PART_RETRIES", "4")))
                    last_error = None
                    for attempt in range(1, retries + 1):
                        attempt_downloaded = 0
                        try:
                            part_tmp.unlink(missing_ok=True)
                            print(
                                f"[STRM] Parte {index + 1}/{parts_count} iniciando download "
                                f"({expected / 1024 / 1024:.1f} MB, tentativa {attempt}/{retries})...",
                                flush=True,
                            )
                            request = Request(url, headers={"Range": f"bytes={start}-{end}", "User-Agent": "Mozilla/5.0"})
                            with urlopen(request, timeout=read_timeout) as part_response:
                                if getattr(part_response, "status", None) and part_response.status not in (200, 206):
                                    raise IOError(f"HTTP status {part_response.status} ao baixar parte {index + 1}")
                                if start > 0 and getattr(part_response, "status", None) == 200:
                                    raise IOError(f"Servidor ignorou cabecalho Range e retornou HTTP 200 para parte {index + 1}")
                                sock = getattr(getattr(getattr(part_response, "fp", None), "raw", None), "_sock", None)
                                if sock is not None:
                                    with contextlib.suppress(Exception):
                                        sock.settimeout(read_timeout)
                                with part_tmp.open("wb") as out:
                                    while chunk := part_response.read(64 * 1024):
                                        out.write(chunk)
                                        attempt_downloaded += len(chunk)
                                        with progress_lock:
                                            progress["downloaded"] += len(chunk)
                                            percent = min(100, int(progress["downloaded"] * 100 / total))
                                            bytes_since_log = progress["downloaded"] - progress["last_logged_bytes"]
                                            if percent >= progress["next_percent"] or bytes_since_log >= 10 * 1024 * 1024:
                                                print(
                                                    f"[STRM] Baixando {src.name}: {progress['downloaded'] / 1024 / 1024:.1f} MB "
                                                    f"de {total / 1024 / 1024:.1f} MB ({percent}%)",
                                                    flush=True,
                                                )
                                                progress["next_percent"] = percent + 1
                                                progress["last_logged_bytes"] = progress["downloaded"]
                            if not part_tmp.exists() or part_tmp.stat().st_size != expected:
                                actual = part_tmp.stat().st_size if part_tmp.exists() else 0
                                raise IOError(f"Parte {index + 1} incompleta ({actual}/{expected} bytes)")
                            part_tmp.replace(part_file)
                            print(f"[STRM] Parte {index + 1}/{parts_count} concluida", flush=True)
                            return
                        except Exception as exc:
                            last_error = exc
                            with progress_lock:
                                progress["downloaded"] = max(0, progress["downloaded"] - attempt_downloaded)
                                percent = min(100, int(progress["downloaded"] * 100 / total))
                                progress["next_percent"] = percent + 1
                                progress["last_logged_bytes"] = progress["downloaded"]
                            part_tmp.unlink(missing_ok=True)
                            part_file.unlink(missing_ok=True)
                            print(
                                f"[STRM] Parte {index + 1}/{parts_count} falhou "
                                f"(tentativa {attempt}/{retries}): {exc}",
                                flush=True,
                            )
                            if attempt < retries:
                                time.sleep(attempt * 2)
                    raise IOError(f"Parte {index + 1} falhou apos {retries} tentativas: {last_error}")

                with ThreadPoolExecutor(max_workers=parts_count) as executor:
                    list(executor.map(download_part, range(parts_count)))
                print(f"[STRM] Todas as {parts_count} partes concluidas; juntando arquivo.", flush=True)
                with tmp_path.open("wb") as out:
                    for part_path in part_paths:
                        with part_path.open("rb") as part:
                            shutil.copyfileobj(part, out, length=1024 * 1024)
                        part_path.unlink(missing_ok=True)
                if tmp_path.stat().st_size != total:
                    raise IOError("Arquivo materializado com tamanho incorreto")
        tmp_path.replace(target)
        for leftover in target.parent.glob(f".{src.stem}.*.download*"):
            with contextlib.suppress(Exception):
                leftover.unlink(missing_ok=True)
        for leftover in target.parent.glob(f".{src.stem}.download*"):
            with contextlib.suppress(Exception):
                leftover.unlink(missing_ok=True)
        return target
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink(missing_ok=True)
        for part_path in part_paths:
            with contextlib.suppress(FileNotFoundError):
                Path(f"{part_path}.tmp").unlink(missing_ok=True)
            with contextlib.suppress(FileNotFoundError):
                part_path.unlink(missing_ok=True)
        for leftover in target.parent.glob(f"{tmp_path.name}*.tmp"):
            with contextlib.suppress(FileNotFoundError):
                leftover.unlink(missing_ok=True)
        raise


def materialize_or_reuse_strm(
    src: Path,
    overwrite: bool,
    known_links: dict[str, str],
    target: Path | None = None,
) -> tuple[Path, str, bool]:
    url = read_strm_url(src)
    requested_target = target
    target = target or src.with_name(f"{src.stem}{guess_media_extension(url)}")
    cached_target = known_links.get(url)
    if cached_target:
        cached_path = Path(cached_target)
        if cached_path.exists():
            if target.exists() and not overwrite:
                return target, url, True
            if cached_path.resolve() != target.resolve():
                shutil.copy2(cached_path, target)
            return target, url, True
    if requested_target is None:
        materialized = materialize_strm(src, overwrite=overwrite)
    else:
        materialized = materialize_strm(src, overwrite=overwrite, target=target)
    return materialized, url, False


def enqueue_upload_job(
    jobs: queue.Queue[tuple[Path, Path] | None],
    pending: set[str],
    lock: threading.Lock,
    source_root: Path,
    dest: Path,
    src: Path,
    destination: Path | None = None,
    src_key: str | None = None,
) -> bool:
    if src_key is None:
        src_key = str(src)
    with lock:
        if src_key in pending:
            return False
        pending.add(src_key)
    jobs.put((src, destination or destination_for(source_root, dest, src)))
    return True


def enqueue_strm_job(
    strm_jobs: queue.Queue[tuple[Path, Path] | None],
    pending: set[str],
    lock: threading.Lock,
    source_root: Path,
    src: Path,
) -> bool:
    src_key = str(src)
    with lock:
        if src_key in pending:
            return False
        pending.add(src_key)
    try:
        strm_jobs.put_nowait((source_root, src))
    except queue.Full:
        with lock:
            pending.discard(src_key)
        return False
    return True


def strm_worker(
    worker_id: int,
    strm_jobs: queue.Queue[tuple[Path, Path] | None],
    upload_jobs: queue.Queue[tuple[Path, Path] | None],
    stats: Stats,
    lock: threading.Lock,
    dest_root: Path,
    overwrite: bool,
    pending: set[str],
    materialized_links: dict[str, str],
    materialized_links_file: Path,
    failed_strm: set[str],
    seen: set[str] | None = None,
    state_file: Path | None = None,
    direct_mongo: bool = False,
    mongo_uri: str | None = None,
):
    while True:
        item = strm_jobs.get()
        if item is None:
            strm_jobs.task_done()
            return
        source_root, src = item
        src_key = str(src)
        try:
            print(f"[STRM][W{worker_id}] Iniciando: {src}", flush=True)
            url = read_strm_url(src)
            media_source = src.with_suffix(guess_media_extension(url))
            destination = destination_for(source_root, dest_root, media_source)
            if (
                mongo_uri
                and is_completed_destination(mongo_uri, dest_root, destination, url, src)
            ):
                mark_seen(src, seen, state_file)
                print(
                    f"[STRM][W{worker_id}] STRM ja concluido no Telegram. Ignorando: {src}",
                    flush=True,
                )
                continue
            direct_target = None
            if direct_mongo:
                needed_size = remote_content_size(url)
                stage_root = get_best_staging_root(required_bytes=needed_size)
                clean_name = destination.name if destination else media_source.name
                direct_target = stage_root / "strm" / clean_name
                wait_for_disk_capacity(
                    direct_target,
                    needed_size,
                    min(max(int(os.getenv("STRM_MIN_FREE_PERCENT", "10")), 1), 90),
                )
            materialized, url, reused = materialize_or_reuse_strm(
                src,
                overwrite=overwrite,
                known_links=materialized_links,
                target=direct_target,
            )
            materialized_links[url] = str(materialized)
            save_materialized_links(materialized_links_file, materialized_links)
            label = "Reaproveitado" if reused else "Materializado"
            print(f"[STRM][W{worker_id}] {label}: {src} -> {materialized}", flush=True)
            mark_seen(src, seen, state_file)
            if direct_mongo:
                enqueue_upload_job(
                    upload_jobs, pending, lock, source_root, dest_root,
                    materialized, destination,
                    src_key=str(src),  # Use .strm path as key to match pending
                )
            else:
                enqueue_upload_job(
                    upload_jobs, pending, lock, source_root, dest_root,
                    materialized,
                    src_key=str(src),  # Use .strm path as key to match pending
                )
        except Exception as exc:
            with lock:
                failed_strm.add(src_key)
            print(f"[STRM][W{worker_id}] Falha ao materializar {src}: {exc}", flush=True)
        finally:
            # Do NOT discard from pending here - let the upload worker handle it
            # when the upload actually completes (or fails)
            strm_jobs.task_done()


def active_count(mongo_uri: str) -> int:
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
    try:
        return mongo_database(client).files.count_documents({"status": {"$in": list(ACTIVE_STATUSES)}})
    finally:
        client.close()


def normalize_media_title(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", ascii_value.casefold()))


def movie_identity(value: str) -> tuple[str, str] | None:
    years = list(re.finditer(r"(?<!\d)((?:19|20)\d{2})(?!\d)", value))
    if not years:
        return None
    year = years[-1]
    title = normalize_media_title(value[:year.start()])
    return (title, year.group(1)) if title else None


def episode_identity(series_name: str, filename: str) -> tuple[str, int, int] | None:
    match = EPISODE_RE.search(Path(filename).stem)
    title = normalize_media_title(series_name)
    if not match or not title:
        return None
    return title, int(match.group("season")), int(match.group("episode"))


def completed_media_identities(mongo_uri: str) -> tuple[set[tuple[str, str]], set[tuple[str, int, int]]]:
    movies: set[tuple[str, str]] = set()
    episodes: set[tuple[str, int, int]] = set()
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
    try:
        for doc in mongo_database(client).files.find(
            {"type": "file", "status": "completed"},
            {"parent": 1, "name": 1},
        ):
            parts = [part for part in str(doc.get("parent", "")).split("/") if part]
            name = str(doc.get("name", ""))
            lowered = [part.casefold() for part in parts]
            if "filmes" in lowered:
                index = lowered.index("filmes")
                if len(parts) > index + 1:
                    identity = movie_identity(parts[index + 1])
                    if identity:
                        movies.add(identity)
            elif "series" in lowered:
                index = lowered.index("series")
                if len(parts) > index + 1:
                    identity = episode_identity(parts[index + 1], name)
                    if identity:
                        episodes.add(identity)
    finally:
        client.close()
    return movies, episodes


def completed_destination_index(mongo_uri: str) -> tuple[set[tuple[str, str]], set[tuple[str, str]], set[tuple[str, str]], set[tuple[str, int, int]]]:
    exact_set: set[tuple[str, str]] = set()
    stem_set: set[tuple[str, str]] = set()
    movies: set[tuple[str, str]] = set()
    episodes: set[tuple[str, int, int]] = set()
    if not mongo_uri or "mongodb://test" in mongo_uri:
        return exact_set, stem_set, movies, episodes
    try:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=1000)
        try:
            for doc in mongo_database(client).files.find(
                {"status": "completed"},
                {"parent": 1, "name": 1, "type": 1, "status": 1},
            ):
                parent = str(doc.get("parent", "")).casefold()
                name = str(doc.get("name", ""))
                if not name:
                    continue
                name_lower = name.casefold()
                stem_lower = Path(name).stem.casefold()
                exact_set.add((parent, name_lower))
                stem_set.add((parent, stem_lower))

                parts = [part for part in parent.split("/") if part]
                if "filmes" in parts:
                    idx = parts.index("filmes")
                    if len(parts) > idx + 1:
                        identity = movie_identity(parts[idx + 1])
                        if identity:
                            movies.add(identity)
                elif "series" in parts:
                    idx = parts.index("series")
                    if len(parts) > idx + 1:
                        identity = episode_identity(parts[idx + 1], name)
                        if identity:
                            episodes.add(identity)
        finally:
            client.close()
    except Exception as exc:
        print(f"[STRM] Erro ao carregar indice do Mongo: {exc}", flush=True)

    return exact_set, stem_set, movies, episodes


def prune_completed_strm(
    sources: list[Path],
    mongo_uri: str,
    apply: bool = False,
    exclude_dirs: set[str] | None = None,
    dest_root: Path | None = None,
) -> dict[str, int]:
    print("[STRM] Verificando mídias concluídas e arquivos STRM no disco...", flush=True)
    completed_movies, completed_episodes = completed_media_identities(mongo_uri)
    exact_set, stem_set, idx_movies, idx_episodes = completed_destination_index(mongo_uri)
    idx_movies.update(completed_movies)
    idx_episodes.update(completed_episodes)
    index = (exact_set, stem_set, idx_movies, idx_episodes)

    excluded = exclude_dirs or set()
    folders: list[tuple[Path, Path]] = []
    files: list[Path] = []
    stats = {"scanned": 0, "matched": 0, "folders": 0, "files": 0}
    last_log_time = 0.0

    for source in sources:
        source = source.resolve()
        effective_dest_root = dest_root.resolve() if dest_root else source
        for root, dirs, names in os.walk(source):
            folder = Path(root)
            relative = folder.relative_to(source)
            if not relative.parts:
                dirs[:] = [name for name in dirs if name.casefold() not in excluded]
            strm_names = [name for name in names if Path(name).suffix.casefold() == ".strm"]
            if not strm_names:
                continue
            stats["scanned"] += len(strm_names)
            matched: list[Path] = []
            for name in strm_names:
                strm_path = folder / name
                now = time.monotonic()
                if now - last_log_time >= 5.0:
                    print(f"[STRM] Analisando: {folder.name}/{name} (analisados={stats['scanned']} duplicados={stats['matched']})", flush=True)
                    last_log_time = now

                completed = False
                parts_to_check = [strm_path.stem, folder.name] + [p for p in reversed(relative.parts) if p.casefold() not in ("filmes", "series")] + [p for p in reversed(folder.parts) if p.casefold() not in ("filmes", "series")]
                for part in parts_to_check:
                    identity = movie_identity(part)
                    if identity and identity in idx_movies:
                        completed = True
                        break
                    ep_identity = episode_identity(part, name)
                    if ep_identity and ep_identity in idx_episodes:
                        completed = True
                        break

                if not completed:
                    try:
                        url = read_strm_url(strm_path)
                        media_src = strm_path.with_suffix(guess_media_extension(url))
                        dest_path = destination_for(source, effective_dest_root, media_src)
                        completed = is_completed_destination(
                            mongo_uri, effective_dest_root, dest_path, url, strm_path,
                            completed_index=index
                        )
                    except Exception:
                        pass

                if completed:
                    matched.append(folder / name)
            if not matched:
                continue
            stats["matched"] += len(matched)
            other_media = any(
                Path(name).suffix.casefold() in MONITORED_EXTENSIONS
                and Path(name).suffix.casefold() != ".strm"
                for name in names
            )
            can_remove_folder = (
                len(relative.parts) >= 2
                and len(matched) == len(strm_names)
                and not dirs
                and not other_media
                and not folder.is_symlink()
            )
            if can_remove_folder:
                folders.append((source, folder))
            else:
                files.extend(matched)

    stats["folders"] = len(folders)
    stats["files"] = len(files)
    if not apply:
        return stats

    folder_targets = {folder for _, folder in folders}
    for source, folder in sorted(folders, key=lambda item: len(item[1].parts), reverse=True):
        resolved = folder.resolve()
        relative = resolved.relative_to(source)
        if len(relative.parts) < 1:
            continue
        if resolved.exists():
            try:
                shutil.rmtree(resolved)
                print(f"[STRM] [FILME] Pasta de filme duplicado removida: {resolved}", flush=True)
            except Exception as exc:
                print(f"[STRM] Erro ao remover pasta {resolved}: {exc}", flush=True)

    for path in files:
        if any(parent in path.parents for parent in folder_targets):
            continue
        try:
            path.unlink(missing_ok=True)
            print(f"[STRM] STRM duplicado removido: {path}", flush=True)
            parent_dir = path.parent
            if parent_dir.exists() and parent_dir.is_dir():
                remaining_strms = list(parent_dir.glob("*.strm"))
                remaining_media = [
                    f for f in parent_dir.iterdir()
                    if f.is_file() and f.suffix.casefold() in MONITORED_EXTENSIONS
                ]
                if not remaining_strms and not remaining_media:
                    try:
                        shutil.rmtree(parent_dir)
                        print(f"[STRM] [SERIE] Pasta limpa por estar vazia: {parent_dir}", flush=True)
                    except Exception:
                        pass
        except Exception as exc:
            print(f"[STRM] Erro ao remover {path}: {exc}", flush=True)
    return stats


def series_path_from_filename(dest: Path, src: Path) -> Path | None:
    match = EPISODE_RE.search(src.stem)
    if not match:
        return None
    series_name = match.group("prefix").strip(" ._-")
    if not series_name:
        return None
    series_name = re.sub(r"[._]+", " ", series_name)
    series_name = re.sub(r"\s+", " ", series_name).strip()
    season = int(match.group("season"))
    return dest / "Series" / series_name / f"Season {season:02d}" / src.name


def ensure_nebula_metadata(mongo_uri: str, dest_root: Path, dst_parent: Path) -> None:
    try:
        rel_parts = dst_parent.resolve().relative_to(dest_root.resolve()).parts
    except ValueError:
        return
    if not rel_parts:
        return
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
    try:
        files = mongo_database(client).files
        parent = f"/{os.getenv('NEBULA_LIBRARY_USER', 'raphael')}"
        now = int(time.time())
        for name in rel_parts:
            if files.find_one({"parent": parent, "name": name}, {"_id": 1}):
                parent = f"{parent}/{name}" if parent != "/" else f"/{name}"
                continue
            doc = {"type": "dir", "ctime": now, "mtime": now, "name": name, "parent": parent, "size": 0}
            try:
                files.insert_one(doc)
            except DuplicateKeyError:
                pass
            parent = f"{parent}/{name}" if parent != "/" else f"/{name}"
    except PyMongoError:
        return
    finally:
        client.close()


def mongo_parent_for(dest_root: Path, dst_parent: Path) -> str:
    user_root = f"/{os.getenv('NEBULA_LIBRARY_USER', 'raphael')}"
    rel_parts = dst_parent.resolve().relative_to(dest_root.resolve()).parts
    if not rel_parts:
        return user_root
    return user_root + "/" + "/".join(rel_parts).replace("\\", "/")


def is_completed_destination(
    mongo_uri: str,
    dest_root: Path,
    destination: Path,
    url: str | None = None,
    src: Path | None = None,
    completed_identities: tuple[set[tuple[str, str]], set[tuple[str, int, int]]] | None = None,
    completed_index: tuple[set[tuple[str, str]], set[tuple[str, str]], set[tuple[str, str]], set[tuple[str, int, int]]] | None = None,
) -> bool:
    if not mongo_uri:
        return False

    if url:
        try:
            parsed = urlsplit(url)
            params = parse_qs(parsed.query)
            if "id" in params:
                file_id_str = params["id"][0]
                client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
                try:
                    doc = None
                    try:
                        oid = ObjectId(file_id_str)
                        doc = mongo_database(client).files.find_one({"_id": oid, "status": "completed"}, {"_id": 1})
                    except Exception:
                        pass
                    if not doc:
                        doc = mongo_database(client).files.find_one({"_id": file_id_str, "status": "completed"}, {"_id": 1})
                    if doc:
                        return True
                finally:
                    client.close()
        except Exception:
            pass

    parent = mongo_parent_for(dest_root, destination.parent).casefold()
    name_lower = destination.name.casefold()
    stem_lower = destination.stem.casefold()

    if completed_index:
        exact_set, stem_set, completed_movies, completed_episodes = completed_index
        if (parent, name_lower) in exact_set or (parent, stem_lower) in stem_set:
            return True
        try:
            dest_parts = destination.resolve().relative_to(dest_root.resolve()).parts
        except Exception:
            dest_parts = destination.parts

        if len(dest_parts) >= 2 and dest_parts[0].casefold() == "filmes":
            movie_id = movie_identity(dest_parts[1]) if len(dest_parts) >= 2 else None
            if not movie_id:
                movie_id = movie_identity(destination.stem)
            if movie_id and movie_id in completed_movies:
                return True
        elif len(dest_parts) >= 3 and dest_parts[0].casefold() == "series":
            series_name = dest_parts[1]
            ep_id = episode_identity(series_name, destination.name)
            if not ep_id and src:
                ep_id = episode_identity(series_name, src.name)
            if ep_id and ep_id in completed_episodes:
                return True
        return False

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
    try:
        for doc in mongo_database(client).files.find(
            {"parent": parent, "status": "completed"},
            {"name": 1},
        ):
            doc_name = str(doc.get("name", ""))
            if doc_name == destination.name or Path(doc_name).stem.casefold() == stem_lower:
                return True

        try:
            dest_parts = destination.resolve().relative_to(dest_root.resolve()).parts
        except Exception:
            dest_parts = destination.parts

        if len(dest_parts) >= 2 and dest_parts[0].casefold() == "filmes":
            movie_id = movie_identity(dest_parts[1]) if len(dest_parts) >= 2 else None
            if not movie_id:
                movie_id = movie_identity(destination.stem)
            if movie_id:
                completed_movies, _ = completed_identities or completed_media_identities(mongo_uri)
                if movie_id in completed_movies:
                    return True
        elif len(dest_parts) >= 3 and dest_parts[0].casefold() == "series":
            series_name = dest_parts[1]
            ep_id = episode_identity(series_name, destination.name)
            if not ep_id and src:
                ep_id = episode_identity(series_name, src.name)
            if ep_id:
                _, completed_episodes = completed_identities or completed_media_identities(mongo_uri)
                if ep_id in completed_episodes:
                    return True
    except PyMongoError:
        return False
    except Exception:
        return False
    finally:
        client.close()
    return False


def register_one(src: Path, dst: Path, dest_root: Path, mongo_uri: str, overwrite: bool, delete_source: bool = False) -> int:
    size = src.stat().st_size
    ensure_nebula_metadata(mongo_uri, dest_root, dst.parent)
    parent = mongo_parent_for(dest_root, dst.parent)
    now = int(time.time())
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
    try:
        files = mongo_database(client).files
        # PRIMEIRO: Busca por local_path para evitar duplicatas (ex: staging_scanner já registrou com hash name)
        existing_by_path = files.find_one({"local_path": str(src)}, {"status": 1, "name": 1, "parent": 1})
        if existing_by_path:
            st = existing_by_path.get("status")
            # Se já está completed ou em processamento, apenas atualiza delete_source
            if st == "completed" or st in {"queued", "staging", "uploading"}:
                files.update_one(
                    {"_id": existing_by_path["_id"]},
                    {"$set": {"delete_source": bool(delete_source), "name": dst.name, "parent": parent}},
                )
                return 0
            # Se está em outro status (failed, etc), atualiza para queued com nome correto
            files.update_one(
                {"_id": existing_by_path["_id"]},
                {"$set": {"name": dst.name, "parent": parent, "size": size, "status": "queued", "mtime": now, "delete_source": bool(delete_source)}},
            )
            return size
        
        # FALLBACK: Busca por parent+name (compatibilidade com docs antigos)
        existing_by_name = files.find_one({"parent": parent, "name": dst.name}, {"status": 1, "local_path": 1})
        if existing_by_name and not overwrite:
            st = existing_by_name.get("status")
            if st == "completed" or st in {"queued", "staging", "uploading"}:
                files.update_one(
                    {"parent": parent, "name": dst.name},
                    {"$set": {"delete_source": bool(delete_source)}},
                )
                return 0
        doc = {
            "type": "file",
            "name": dst.name,
            "parent": parent,
            "size": size,
            "status": "queued",
            "local_path": str(src),
            "mtime": now,
            "ctime": now,
            "parts": [],
            "delete_source": bool(delete_source),
        }
        files.update_one({"parent": parent, "name": dst.name}, {"$set": doc}, upsert=True)
        return size
    finally:
        client.close()


def destination_for(source: Path, dest: Path, src: Path) -> Path:
    rel = src.relative_to(source)
    parts = list(rel.parts)
    while len(parts) >= 2 and parts[0].lower() in ("filmes", "series", "porno") and parts[1].lower() == parts[0].lower():
        parts.pop(0)

    series_by_name = series_path_from_filename(dest, src)
    if len(parts) >= 2 and parts[0].lower() == "filmes":
        if series_by_name:
            return series_by_name
        if len(parts) == 2:
            return dest / "Filmes" / src.stem / src.name
        return dest / "Filmes" / Path(*parts[1:])
    if len(parts) >= 1 and parts[0].lower() == "porno":
        return dest / "Porno" / src.name
    if len(parts) >= 2 and parts[0].lower() == "series":
        return dest / "Series" / Path(*parts[1:])
    if series_by_name:
        return series_by_name
    return dest / rel


def ensure_directory(path: Path, ensured_dirs: set[str], dir_lock: threading.Lock) -> None:
    resolved = str(path)
    with dir_lock:
        if resolved in ensured_dirs:
            return
        missing: list[Path] = []
        current = path
        while not current.exists():
            missing.append(current)
            parent = current.parent
            if parent == current:
                break
            current = parent
        for item in reversed(missing):
            item.mkdir(exist_ok=True)
        deadline = time.time() + 20
        while not path.is_dir():
            if time.time() >= deadline:
                raise FileNotFoundError(f"Destino ainda nao visivel: {path}")
            time.sleep(0.5)
        ensured_dirs.add(resolved)


def copy_one(
    src: Path,
    dst: Path,
    dest_root: Path,
    mongo_uri: str,
    overwrite: bool,
    ensured_dirs: set[str],
    dir_lock: threading.Lock,
) -> int:
    ensure_nebula_metadata(mongo_uri, dest_root, dst.parent)
    ensure_directory(dst.parent, ensured_dirs, dir_lock)
    if dst.is_file() and not overwrite and dst.stat().st_size == src.stat().st_size:
        return 0
    if dst.exists() and overwrite:
        try:
            dst.unlink()
        except FileNotFoundError:
            pass
    shutil.copyfile(src, dst)
    return src.stat().st_size


def worker(
    worker_id: int,
    jobs: queue.Queue[tuple[Path, Path] | None],
    stats: Stats,
    lock: threading.Lock,
    dest_root: Path,
    mongo_uri: str,
    overwrite: bool,
    retries: int,
    ensured_dirs: set[str],
    dir_lock: threading.Lock,
    pending: set[str],
    direct_mongo: bool,
    seen: set[str] | None = None,
    state_file: Path | None = None,
    delete_source: bool = False,
    failed_strm: set[str] | None = None,
):
    while True:
        item = jobs.get()
        if item is None:
            jobs.task_done()
            return
        src, dst = item
        src_key = str(src)
        copied = False
        for attempt in range(1, retries + 1):
            try:
                if direct_mongo:
                    size = register_one(src, dst, dest_root, mongo_uri, overwrite, delete_source)
                    if not size and delete_source:
                        src.unlink(missing_ok=True)
                else:
                    size = copy_one(src, dst, dest_root, mongo_uri, overwrite, ensured_dirs, dir_lock)
                    if size and delete_source:
                        try:
                            src.unlink()
                            print(f"[W{worker_id}] Removido arquivo de origem: {src}", flush=True)
                        except Exception as e:
                            print(f"[W{worker_id}] Falha ao remover origem {src}: {e}", flush=True)
                with lock:
                    if size:
                        stats.copied += 1
                        stats.bytes_copied += size
                    else:
                        stats.skipped += 1
                    mark_seen(src, seen, state_file)
                copied = True
                break
            except Exception as exc:
                with dir_lock:
                    ensured_dirs.discard(str(dst.parent))
                if attempt == retries:
                    with lock:
                        stats.failed += 1
                    print(f"[W{worker_id}] falhou: {src} -> {dst}: {exc}", flush=True)
                else:
                    time.sleep(min(30, attempt * 3))
        with lock:
            pending.discard(src_key)
            if not copied and failed_strm is not None:
                # Track failed items so main loop doesn't retry immediately
                # Use the original source path as key (could be .strm or regular file)
                failed_strm.add(src_key)
        if copied:
            with lock:
                done = stats.copied + stats.skipped + stats.failed
                remaining = max(stats.queued - done, 0)
                mb = stats.bytes_copied / 1024 / 1024
            print(
                f"[W{worker_id}] progresso: feitos={done}/{stats.queued} "
                f"copiados={stats.copied} ignorados={stats.skipped} "
                f"falhas={stats.failed} faltam={remaining} volume={mb:.2f} MB",
                flush=True,
            )
        jobs.task_done()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Alimenta o Nebula preservando pastas."
    )
    parser.add_argument("--source", action="append", default=[], help="Origem. Pode repetir ou separar por ;, ou ,")
    parser.add_argument("--dest", default=".nebula_virtual_root", help="Raiz virtual do Nebula. No modo direto nao precisa existir.")
    parser.add_argument("--workers", type=int, default=2, help="Copias paralelas. Padrao: 2")
    parser.add_argument("--overwrite", action="store_true", help="Sobrescreve destino existente.")
    parser.add_argument("--all-files", action="store_true", help="Copia tambem arquivos nao-video.")
    parser.add_argument("--retries", type=int, default=3, help="Tentativas por arquivo. Padrao: 3")
    parser.add_argument("--watch", action="store_true", help="Mantem o FTP abastecido conforme a fila libera.")
    parser.add_argument("--direct-mongo", action="store_true", help="Enfileira direto no Mongo sem copiar para o FTP montado.")
    parser.add_argument("--max-active", type=int, default=20, help="Maximo staging+fila+enviando no Nebula. Padrao: 20")
    parser.add_argument("--poll-seconds", type=int, default=60, help="Intervalo do modo watch. Padrao: 60")
    parser.add_argument("--state-file", default="feed_ftp_state.json", help="Arquivo de controle do que ja foi alimentado.")
    parser.add_argument("--exclude-dir", action="append", default=[], help="Ignora uma pasta raiz da origem.")
    parser.add_argument("--delete-source", action="store_true", help="Exclui o arquivo de origem apos mover/enfileirar com sucesso.")
    parser.add_argument(
        "--prune-completed-strm",
        action="store_true",
        help="Remove pastas/STRMs que ja constam como concluidos no Mongo.",
    )
    parser.add_argument(
        "--max-downloads",
        type=int,
        default=1,
        help="Maximo de downloads simultaneos. Padrao: 1 (sequencial).",
    )
    parser.add_argument(
        "--disk-free-threshold",
        type=int,
        default=30,
        help="Percentual minimo de disco livre para iniciar downloads. Padrao: 30%%.",
    )
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        reconfig = getattr(stream, "reconfigure", None)
        if callable(reconfig):
            reconfig(encoding="utf-8", errors="replace")

    dest = Path(args.dest).resolve()
    if not args.direct_mongo and not dest.exists():
        print(f"Destino nao existe ou FTP nao montado: {dest}")
        return 2

    sources = split_source_paths(args.source)
    if not sources:
        print("Nenhuma origem informada.")
        return 2
    missing_sources = [src for src in sources if not src.exists()]
    if missing_sources:
        print("Origem nao existe: " + ", ".join(str(src) for src in missing_sources))
        return 2

    print(f"Feeder iniciado. Monitorando origens: {', '.join(str(s) for s in sources)}", flush=True)

    jobs: queue.Queue[tuple[Path, Path] | None] = queue.Queue(maxsize=args.workers * 4)
    strm_jobs: queue.Queue[tuple[Path, Path] | None] = queue.Queue(maxsize=args.max_downloads)
    stats = Stats()
    lock = threading.Lock()
    dir_lock = threading.Lock()
    ensured_dirs: set[str] = set()
    pending: set[str] = set()
    failed_strm: set[str] = set()
    state_file = Path(args.state_file)
    seen = load_seen(state_file) if args.watch else None
    materialized_links_file = state_file.with_name(f"{state_file.stem}_materialized_links.json")
    materialized_links = load_materialized_links(materialized_links_file)
    last_watch_snapshot: tuple[int, int, int] | None = None
    last_idle_notice = 0.0

    load_dotenv()
    mongo_uri = os.getenv("MONGODB", "mongodb://localhost:27017")
    exclude_dirs = {name.lower() for name in args.exclude_dir}
    stage_roots = [
        Path(path.strip()).resolve()
        for path in os.getenv("STAGING_DIRS", os.getenv("STAGING_DIR", "staging")).split(";")
        if path.strip()
    ]
    cleanup_roots = list({
        str(path).lower(): path
        for path in [*sources, *stage_roots]
    }.values())
    # prune_completed_strm DESABILITADO por padrao - o bot de limpeza (clean_already_sent.py) 
    # ja faz isso separadamente. Manter .strm no disco para o feeder poder verificar/reprocessar.
    if args.prune_completed_strm:
        print("[FEED] --prune-completed-strm desabilitado para preservar .strm no disco. Use o bot de limpeza separado.", flush=True)
        # try:
        #     pruned = prune_completed_strm(
        #         sources,
        #         mongo_uri,
        #         apply=True,
        #         exclude_dirs=exclude_dirs,
        #         dest_root=dest,
        #     )
        #     print(
        #         "Limpeza STRM concluida: "
        #         f"analisados={pruned['scanned']} encontrados={pruned['matched']} "
        #         f"pastas_removidas={pruned['folders']} strm_removidos={pruned['files']}",
        #         flush=True,
        #     )
        # except Exception as exc:
        #     print(f"Limpeza STRM ignorada por seguranca: {exc}", flush=True)

    threads = [
        threading.Thread(
            target=worker,
            args=(
                idx + 1,
                jobs,
                stats,
                lock,
                dest,
                mongo_uri,
                args.overwrite,
                args.retries,
                ensured_dirs,
                dir_lock,
                pending,
                args.direct_mongo,
                seen,
                state_file,
                args.delete_source,
                failed_strm,  # Add failed_strm for tracking failures
            ),
            daemon=True,
        )
        for idx in range(max(args.workers, 1))
    ]
    strm_thread = threading.Thread(
        target=strm_worker,
        args=(
            1,
            strm_jobs,
            jobs,
            stats,
            lock,
            dest,
            args.overwrite,
            pending,
            materialized_links,
            materialized_links_file,
            failed_strm,
            seen,
            state_file,
            args.direct_mongo,
            mongo_uri,
        ),
        daemon=True,
    )
    for thread in threads:
        thread.start()
    strm_thread.start()

    last_cleanup = 0.0
    completed_cache = None
    last_cache_update = 0.0
    last_disk_check = 0.0
    disk_waiting = False

    def check_disk_space() -> bool:
        """Check if staging disk has enough free space (percentage-based)."""
        try:
            stage_root = get_best_staging_root()
            usage = shutil.disk_usage(stage_root)
            free_pct = (usage.free / usage.total) * 100
            return free_pct >= args.disk_free_threshold
        except Exception:
            return True  # If can't check, assume OK

    def wait_for_disk_space():
        """Wait until disk has enough free space."""
        nonlocal disk_waiting, last_disk_check
        if disk_waiting:
            return
        disk_waiting = True
        print(f"[DISK] Espaco em disco abaixo de {args.disk_free_threshold}%. Aguardando liberacao...", flush=True)
        while True:
            time.sleep(args.poll_seconds)
            if check_disk_space():
                print(f"[DISK] Espaco liberado (>= {args.disk_free_threshold}%). Retomando downloads.", flush=True)
                disk_waiting = False
                break

    while True:
        # Periodic cleanup
        if time.monotonic() - last_cleanup >= 600:
            stale_seconds = max(300, int(os.getenv("INCOMPLETE_MAX_AGE_SECONDS", "1800")))
            removed, released = cleanup_stale_downloads(cleanup_roots, stale_seconds)
            last_cleanup = time.monotonic()
            if removed:
                print(
                    f"Limpeza automatica: removidos={removed} incompletos "
                    f"liberado={released / 1024 / 1024:.1f} MB",
                    flush=True,
                )

        # Refresh completed cache periodically
        if mongo_uri and (completed_cache is None or time.monotonic() - last_cache_update >= 300):
            try:
                completed_cache = completed_media_identities(mongo_uri)
                last_cache_update = time.monotonic()
            except Exception:
                pass

        # Check disk space periodically
        if time.monotonic() - last_disk_check >= 30:
            last_disk_check = time.monotonic()
            if not check_disk_space():
                wait_for_disk_space()
                continue  # Re-check after waiting

        # Find next file to process (priority order, one download at a time)
        next_item = None
        try:
            for source_root, src in iter_files_by_priority(sources, args.all_files, exclude_dirs):
                src_key = str(src)

                # Skip if already seen (for ALL files including .strm)
                if seen is not None and src_key in seen:
                    continue

                # Skip if already pending or failed
                with lock:
                    if src_key in pending or src_key in failed_strm:
                        continue

                # Check if already completed in MongoDB BEFORE downloading
                if src.suffix.lower() == ".strm" and mongo_uri:
                    try:
                        url = read_strm_url(src)
                        media_src = src.with_suffix(guess_media_extension(url))
                        dest_path = destination_for(source_root, dest, media_src)
                        if is_completed_destination(mongo_uri, dest, dest_path, url, src, completed_cache):
                            mark_seen(src, seen, state_file)
                            print(f"[STRM] Ja concluido no Mongo, pulando: {src}", flush=True)
                            continue
                    except Exception:
                        pass
                elif src.suffix.lower() != ".strm" and mongo_uri:
                    # For regular files, check if destination already completed
                    dest_path = destination_for(source_root, dest, src)
                    parent = mongo_parent_for(dest, dest_path.parent)
                    if is_completed_destination(mongo_uri, dest, dest_path, None, src, completed_cache):
                        if seen is not None:
                            mark_seen(src, seen, state_file)
                        print(f"[FILE] Ja concluido no Mongo, pulando: {src}", flush=True)
                        continue

                # Check disk space before starting (for .strm downloads)
                if src.suffix.lower() == ".strm":
                    try:
                        url = read_strm_url(src)
                        size = remote_content_size(url)
                        # Check staging disk space on the disk with the most free space
                        stage_root = get_best_staging_root()
                        free_bytes = get_free_bytes(stage_root)
                        free_gb = free_bytes / (1024**3)
                        need_gb = (size or 0) / (1024**3) * 1.1  # 10% margin
                        if size and free_gb < need_gb:
                            print(f"[DISK] Espaco insuficiente no melhor disco ({stage_root}): {free_gb:.1f}GB livre, precisa {need_gb:.1f}GB. Aguardando...", flush=True)
                            time.sleep(args.poll_seconds)
                            break  # Retry next loop
                    except Exception:
                        pass

                # Found next item to process
                next_item = (source_root, src)
                break

        except Exception as exc:
            print(f"Erro ao escanear origem: {exc}", flush=True)

        if next_item is None:
            # Nothing to process
            if not args.watch:
                break
            time.sleep(args.poll_seconds)
            continue

        # Start processing the next item (download only, don't wait for upload)
        source_root, src = next_item
        src_key = str(src)

        if src.suffix.lower() == ".strm":
            if not enqueue_strm_job(strm_jobs, pending, lock, source_root, src):
                time.sleep(2)
                continue
            print(f"[FEEDER] Enfileirado para download: {src}", flush=True)
        else:
            if not enqueue_upload_job(jobs, pending, lock, source_root, dest, src):
                time.sleep(2)
                continue
            print(f"[FEEDER] Enfileirado para upload: {src}", flush=True)
            with lock:
                stats.queued += 1

        # Update MongoDB stats
        try:
            client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
            mongo_database(client).stats.update_one(
                {"_id": "feeder"},
                {"$set": {"source": " | ".join(str(s) for s in sources), "updated_at": int(time.time())}},
                upsert=True
            )
            client.close()
        except Exception as e:
            print(f"Erro ao atualizar estatisticas no MongoDB: {e}", flush=True)

        # In watch mode, continue to next item immediately (download is async)
        # The strm_worker will handle download and enqueue upload when done
        if args.watch:
            time.sleep(1)  # Brief pause to avoid tight loop
            continue
        else:
            # Non-watch mode: process one and exit
            break

    # Wait for all pending items to finish before shutting down
    print("[FEEDER] Aguardando downloads/uploads pendentes finalizarem...", flush=True)
    while True:
        with lock:
            if not pending:
                break
        time.sleep(args.poll_seconds)

    for _ in threads:
        jobs.put(None)
    strm_jobs.put(None)
    jobs.join()
    strm_jobs.join()

    print(
        f"Finalizado: fila={stats.queued} copiados={stats.copied} "
        f"ignorados={stats.skipped} falhas={stats.failed} "
        f"volume={stats.bytes_copied / 1024 / 1024:.2f} MB",
        flush=True,
    )
    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
