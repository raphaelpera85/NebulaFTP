from __future__ import annotations

import argparse
import contextlib
import mimetypes
import json
import os
import queue
import re
import shutil
import tempfile
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

UPLOADABLE_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v",
    ".sub", ".ass", ".ssa", ".vtt",
}
MONITORED_EXTENSIONS = UPLOADABLE_EXTENSIONS | {".strm"}
ACTIVE_STATUSES = ("staging", "queued", "uploading")
EPISODE_RE = re.compile(r"(?i)(?P<prefix>.*?)(?:[.\s_-]+)?s(?P<season>\d{1,2})e(?P<episode>\d{1,3})")


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


def split_source_paths(raw_sources: list[str] | None) -> list[Path]:
    raw_sources = raw_sources or [r"E:\\"]
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


def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_seen(path: Path, seen: set[str]) -> None:
    path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")


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


def materialize_strm(src: Path, overwrite: bool = False) -> Path:
    url = read_strm_url(src)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"URL .strm invalida: {url}")

    target_name = f"{src.stem}{guess_media_extension(url)}"
    target = src.with_name(target_name)
    if target.exists() and not overwrite:
        src.unlink(missing_ok=True)
        return target

    fd, tmp_name = tempfile.mkstemp(dir=str(src.parent), prefix=f".{src.stem}.", suffix=".download")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with urlopen(url, timeout=120) as response, tmp_path.open("wb") as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
        tmp_path.replace(target)
        src.unlink(missing_ok=True)
        return target
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


def active_count(mongo_uri: str) -> int:
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
    try:
        return client.ftp.files.count_documents({"status": {"$in": list(ACTIVE_STATUSES)}})
    finally:
        client.close()


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
        files = client.ftp.files
        parent = "/raphael"
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
    rel_parts = dst_parent.resolve().relative_to(dest_root.resolve()).parts
    if not rel_parts:
        return "/raphael"
    return "/raphael/" + "/".join(rel_parts).replace("\\", "/")


def register_one(src: Path, dst: Path, dest_root: Path, mongo_uri: str, overwrite: bool, delete_source: bool = False) -> int:
    size = src.stat().st_size
    ensure_nebula_metadata(mongo_uri, dest_root, dst.parent)
    parent = mongo_parent_for(dest_root, dst.parent)
    now = int(time.time())
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
    try:
        files = client.ftp.files
        existing = files.find_one({"parent": parent, "name": dst.name}, {"status": 1})
        if existing and existing.get("status") in {"queued", "staging", "uploading", "completed"} and not overwrite:
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
        }
        if delete_source:
            doc["delete_source"] = True
        files.update_one({"parent": parent, "name": dst.name}, {"$set": doc}, upsert=True)
        return size
    finally:
        client.close()


def destination_for(source: Path, dest: Path, src: Path) -> Path:
    rel = src.relative_to(source)
    parts = rel.parts
    series_by_name = series_path_from_filename(dest, src)
    if len(parts) >= 2 and parts[0].lower() == "filmes":
        if series_by_name:
            return series_by_name
        if len(parts) == 2:
            return dest / "Filmes" / src.stem / src.name
        return dest / "Filmes" / Path(*parts[1:])
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

    jobs: queue.Queue[tuple[Path, Path] | None] = queue.Queue(maxsize=args.workers * 4)
    stats = Stats()
    lock = threading.Lock()
    dir_lock = threading.Lock()
    ensured_dirs: set[str] = set()
    pending: set[str] = set()
    state_file = Path(args.state_file)
    seen = load_seen(state_file) if args.watch else None
    last_watch_snapshot: tuple[int, int, int] | None = None
    last_idle_notice = 0.0

    load_dotenv()
    mongo_uri = os.getenv("MONGODB", "mongodb://localhost:27017")
    exclude_dirs = {name.lower() for name in args.exclude_dir}

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
            ),
            daemon=True,
        )
        for idx in range(max(args.workers, 1))
    ]
    for thread in threads:
        thread.start()

    while True:
        # Conta mídias pendentes físicas no disco antes de filtrar por slots livres
        all_unseen = []
        try:
            for source_root, src in iter_files(sources, args.all_files, exclude_dirs):
                src_key = str(src)
                if seen is not None and src_key in seen:
                    continue
                with lock:
                    if src_key in pending:
                        continue
                all_unseen.append((source_root, src))
        except Exception as exc:
            print(f"Erro ao escanear origem: {exc}", flush=True)

        remaining_count = len(all_unseen)

        # Atualiza estatísticas no MongoDB
        try:
            client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
            client.ftp.stats.update_one(
                {"_id": "feeder"},
                {"$set": {"source": " | ".join(str(src) for src in sources), "pending_disk_files": remaining_count, "updated_at": int(time.time())}},
                upsert=True
            )
            client.close()
        except Exception as e:
            print(f"Erro ao atualizar estatisticas no MongoDB: {e}", flush=True)

        free_slots = args.max_active
        if args.watch:
            try:
                active = active_count(mongo_uri)
                free_slots = max(args.max_active - active, 0)
                snapshot = (active, free_slots, remaining_count)
                now = time.monotonic()
                if snapshot != last_watch_snapshot or now - last_idle_notice >= args.poll_seconds * 5:
                    print(
                        f"Fila Nebula: ativos={active} limite={args.max_active} livres={free_slots} | Pendentes no disco: {remaining_count}",
                        flush=True,
                    )
                    last_watch_snapshot = snapshot
                    last_idle_notice = now
                if free_slots <= 0:
                    time.sleep(args.poll_seconds)
                    continue
            except Exception as exc:
                print(f"Nao consegui ler Mongo, aguardando: {exc}", flush=True)
                time.sleep(args.poll_seconds)
                continue

        added = 0
        for source_root, src in all_unseen:
            src_key = str(src)
            if src.suffix.lower() == ".strm":
                try:
                    materialized = materialize_strm(src, overwrite=args.overwrite)
                    print(f"[STRM] Materializado: {src} -> {materialized}", flush=True)
                    mark_seen(src, seen, state_file)
                except Exception as exc:
                    print(f"[STRM] Falha ao materializar {src}: {exc}", flush=True)
                continue
            with lock:
                stats.queued += 1
                pending.add(src_key)
            jobs.put((src, destination_for(source_root, dest, src)))
            added += 1
            if args.watch and added >= free_slots:
                break

        if not args.watch:
            break
        if added == 0:
            pass
        time.sleep(args.poll_seconds)

    for _ in threads:
        jobs.put(None)
    jobs.join()

    print(
        f"Finalizado: fila={stats.queued} copiados={stats.copied} "
        f"ignorados={stats.skipped} falhas={stats.failed} "
        f"volume={stats.bytes_copied / 1024 / 1024:.2f} MB",
        flush=True,
    )
    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
