import asyncio

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

# Garante runtime dependencies antes de qualquer import de pyrogram/motor/
# bson abaixo. Importar `tools.check_deps` é seguro porque o módulo só
# usa stdlib (importlib/subprocess) — não toca em runtime libs.
try:
    from tools.check_deps import ensure_runtime_dependencies as _ensure_deps
    _ensure_deps()
except RuntimeError as exc:
    raise SystemExit(f"[deps] {exc}") from None

import contextlib
import hmac
import io
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import time
import uuid
from collections import deque
from html import escape
from logging.handlers import RotatingFileHandler
from os import environ
from os.path import exists
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import aiofiles
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from pyrogram import Client
from pyrogram import utils as pyrogram_utils
from pyrogram.errors import FloodWait, RPCError
import pyrogram.session.internals.msg_id as pyrogram_msg_id

# Auto-sync time offset com Telegram (previne SecurityCheckMismatch em relógios dessincronizados)
try:
    import urllib.request
    import email.utils
    _req = urllib.request.Request('https://api.telegram.org', headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(_req, timeout=5) as _resp:
        _date_str = _resp.headers.get('Date')
        if _date_str:
            _server_time = email.utils.parsedate_to_datetime(_date_str).timestamp()
            _time_offset = _server_time - time.time()
            if abs(_time_offset) > 1.0:
                _orig_new = pyrogram_msg_id.MsgId.__new__
                def _patched_msg_id_new(cls):
                    now = int(time.time() + _time_offset)
                    cls.offset = (cls.offset + 4) if now == cls.last_time else 0
                    msg_id = (now * 2 ** 32) + cls.offset
                    cls.last_time = now
                    return msg_id
                pyrogram_msg_id.MsgId.__new__ = _patched_msg_id_new
except Exception:
    pass

from control_plane import ControlPlane, FeederSupervisor, validate_ftp_security
from ftp import MongoDBPathIO, MongoDBUserManager, Server
from ftp.common import UPLOAD_QUEUE
from ftp.pathio import MongoDBMemoryIO, Node, is_uploadable_name, movie_folder_score
from ftp.range import parse_range as _parse_range
from ftp.tg import install_reliable_upload, send_document_bot_api

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

pyrogram_utils.MIN_CHANNEL_ID = min(pyrogram_utils.MIN_CHANNEL_ID, -1009999999999)  # type: ignore
install_reliable_upload()

if exists(".env"):
    from dotenv import load_dotenv
    load_dotenv()

# --- CARREGAMENTO DE CONFIGURAÇÕES DO .ENV ---
LOG_LEVEL = environ.get("LOG_LEVEL", "INFO")
LOG_COMPACT_LINES = int(environ.get("LOG_COMPACT_LINES", "1000"))
LOG_CONTEXT_FILE = environ.get("LOG_CONTEXT_FILE", "nebula_context.md")
LOG_MAX_SIZE = max(1, int(environ.get("LOG_MAX_SIZE", "5")))
LOG_BACKUP_COUNT = max(0, int(environ.get("LOG_BACKUP_COUNT", "3")))
CHUNK_SIZE_MB = int(environ.get("CHUNK_SIZE_MB", "64"))
CHUNK_SIZE = CHUNK_SIZE_MB * 1024 * 1024
MAX_RETRIES = int(environ.get("MAX_RETRIES", "5"))
MAX_STAGING_AGE = int(environ.get("MAX_STAGING_AGE", "3600"))
MAX_WORKERS = int(environ.get("MAX_WORKERS", "4"))
PART_WORKERS_PER_FILE = max(1, int(environ.get("PART_WORKERS_PER_FILE", "2")))
UPLOAD_CONCURRENCY = max(1, int(environ.get("UPLOAD_CONCURRENCY", "8")))
STREAM_ONLY = environ.get("STREAM_ONLY", "false").lower() in ("1", "true", "yes")
UPLOAD_STATUS_MESSAGES = environ.get("UPLOAD_STATUS_MESSAGES", "false").lower() in ("1", "true", "yes")
STREAM_HOST = environ.get("STREAM_HOST", "127.0.0.1")
STREAM_PORT = int(environ.get("STREAM_PORT", "2122"))
STREAM_TOKEN = environ.get("STREAM_TOKEN", "")
TRANSCODE_CONCURRENCY = max(1, int(environ.get("TRANSCODE_CONCURRENCY", "2")))
_TRANSCODE_SEMAPHORE = None
_TRANSCODE_SEMAPHORE_LOOP = None
STAGING_DIRS = [
    os.path.abspath(path.strip())
    for path in environ.get("STAGING_DIRS", environ.get("STAGING_DIR", "staging")).split(";")
    if path.strip()
]
_UPLOAD_SEMAPHORE = None
_UPLOAD_SEMAPHORE_LOOP = None
UPLOAD_BOT_CURSOR = 0


def get_upload_semaphore():
    global _UPLOAD_SEMAPHORE, _UPLOAD_SEMAPHORE_LOOP
    loop = asyncio.get_running_loop()
    if _UPLOAD_SEMAPHORE is None or _UPLOAD_SEMAPHORE_LOOP is not loop:
        _UPLOAD_SEMAPHORE = asyncio.Semaphore(UPLOAD_CONCURRENCY)
        _UPLOAD_SEMAPHORE_LOOP = loop
    return _UPLOAD_SEMAPHORE


def get_transcode_semaphore():
    global _TRANSCODE_SEMAPHORE, _TRANSCODE_SEMAPHORE_LOOP
    loop = asyncio.get_running_loop()
    if _TRANSCODE_SEMAPHORE is None or _TRANSCODE_SEMAPHORE_LOOP is not loop:
        _TRANSCODE_SEMAPHORE = asyncio.Semaphore(TRANSCODE_CONCURRENCY)
        _TRANSCODE_SEMAPHORE_LOOP = loop
    return _TRANSCODE_SEMAPHORE


def is_loopback_host(host):
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def get_upload_worker_count(bot_count):
    # Each worker uploads one file at a time using PART_WORKERS_PER_FILE parallel parts.
    target_workers = max(1, bot_count // PART_WORKERS_PER_FILE)
    return min(MAX_WORKERS, UPLOAD_CONCURRENCY // PART_WORKERS_PER_FILE, target_workers)


def next_upload_bot_index(bot_count):
    """Return a process-wide round-robin start index for the next upload part."""
    global UPLOAD_BOT_CURSOR
    if bot_count == 0:
        return 0
    index = UPLOAD_BOT_CURSOR % bot_count
    UPLOAD_BOT_CURSOR = (UPLOAD_BOT_CURSOR + 1) % bot_count
    return index


def get_contiguous_uploaded_parts(parts, total_parts=None):
    by_id = {part.get("part_id"): part for part in parts or []}
    if total_parts is None:
        total_parts = max(by_id, default=-1) + 1
    completed = []
    for part_id in range(total_parts):
        part = by_id.get(part_id)
        if not part or not part.get("tg_file") or int(part.get("file_size") or 0) <= 0:
            break
        completed.append(part)
    return completed


async def _wait_for_streaming(bot):
    active_streams = getattr(bot, "_nebula_streams", 0)
    while isinstance(active_streams, int) and active_streams > 0:
        await asyncio.sleep(0.1)
        active_streams = getattr(bot, "_nebula_streams", 0)


def classify_media_type(parent: str, filename: str) -> str:
    """Classifica deterministicamente o tipo da mídia: 'SERIE', 'PORNO' ou 'FILME'."""
    p_lower = (parent or "").lower()
    f_lower = (filename or "").lower()
    if any(x in p_lower for x in ["/porno", "porno", "porn", "xxx", "hentai", "adulto"]) or re.search(r"\b(porno|porn|xxx|hentai|adulto)\b", f_lower):
        return "PORNO"
    if any(x in p_lower for x in ["/series", "series"]) or re.search(r"(?i)\bS\d{1,2}[ ._-]*E\d{1,3}\b|\b\d{1,2}x\d{1,3}\b|season", f_lower):
        return "SERIE"
    return "FILME"


def build_part_caption(media_type: str, filename: str, part_num: int, total_parts: int, file_uuid: str, real_size: int) -> str:
    """Gera legenda estruturada para visualização e reconstrução a partir do Telegram."""
    size_mb = real_size / (1024 * 1024) if real_size else 0
    return f"[NEBULA] TIPO: {media_type} | MIDIA: {filename} | PARTE: {part_num + 1}/{total_parts} | UUID: {file_uuid} | TAM: {size_mb:.1f}MB"


async def _send_part_document(
    bot,
    target_chat_id,
    chunk_data,
    chunk_name,
    caption="",
):
    token = getattr(bot, "_nebula_bot_token", None)
    if not isinstance(token, str) or not token:
        return await bot.send_document(
            chat_id=target_chat_id,
            document=io.BytesIO(chunk_data),
            file_name=chunk_name,
            force_document=True,
            caption=caption,
        )

    return await send_document_bot_api(
        bot,
        target_chat_id,
        chunk_data,
        chunk_name,
        caption,
    )


async def _send_part_on_idle_bot(
    bot,
    target_chat_id,
    chunk_data,
    chunk_name,
    caption="",
):
    """Wait for capacity, reserve the bot, and release it before retries/backoff."""
    async with get_upload_semaphore():
        await _wait_for_streaming(bot)
        active_uploads = getattr(bot, "_nebula_uploads", 0)
        if not isinstance(active_uploads, int):
            active_uploads = 0
        bot._nebula_uploads = active_uploads + 1
        try:
            return await _send_part_document(
                bot,
                target_chat_id,
                chunk_data,
                chunk_name,
                caption=caption,
            )
        finally:
            active_uploads = getattr(bot, "_nebula_uploads", 1)
            if not isinstance(active_uploads, int):
                active_uploads = 1
            bot._nebula_uploads = max(0, active_uploads - 1)


# Portas Passivas
PASSIVE_PORTS = None
PASSIVE_PORTS_ERROR = None
pp_str = environ.get("PASSIVE_PORTS")
if pp_str and "-" in pp_str:
    try:
        start_p, end_p = map(int, pp_str.split("-"))
        if start_p > end_p or start_p < 1 or end_p > 65535:
            raise ValueError(f"invalid passive port range: {pp_str}")
        PASSIVE_PORTS = range(start_p, end_p + 1)
    except (ValueError, TypeError) as exc:
        PASSIVE_PORTS_ERROR = str(exc)

# TLS / FTPS (RFC 4217)
TLS_CERTFILE = environ.get("TLS_CERTFILE")
TLS_KEYFILE = environ.get("TLS_KEYFILE")
TLS_REQUIRE_CLIENT_CERT = environ.get("TLS_REQUIRE_CLIENT_CERT", "false").lower() in ("1", "true", "yes")
TLS_REQUIRED = environ.get("TLS_REQUIRED", "false").lower() in ("1", "true", "yes")
FTP_SECURITY_MODE = environ.get("FTP_SECURITY_MODE") or (
    "ftps-explicit" if TLS_CERTFILE and TLS_KEYFILE else "ftp"
)
CONTROL_ENABLED = environ.get("CONTROL_ENABLED", "false").lower() in ("1", "true", "yes")
CONTROL_HOST = environ.get("CONTROL_HOST", "127.0.0.1")
CONTROL_PORT = int(environ.get("CONTROL_PORT", "2130"))
CONTROL_DRAIN_TIMEOUT = max(1, int(environ.get("CONTROL_DRAIN_TIMEOUT", "300")))
PRUNE_PREVIEW_TTL = max(30, int(environ.get("PRUNE_PREVIEW_TTL", "300")))


def configured_paths(name, fallback=""):
    return tuple(
        Path(path.strip()).expanduser().resolve()
        for path in environ.get(name, fallback).split(";")
        if path.strip()
    )


FEED_ALLOWED_ROOTS = configured_paths("FEED_ALLOWED_ROOTS")
FEED_ALLOWED_DESTINATIONS = configured_paths("FEED_ALLOWED_DESTINATIONS")
STRM_OUTPUT_ROOTS = configured_paths(
    "STRM_OUTPUT_ROOTS",
    environ.get("STRM_OUTPUT_DIR", str(Path(__file__).resolve().parent / "strm_library")),
)
FEED_STATE_DIR = Path(
    environ.get("FEED_STATE_DIR", str(Path(__file__).resolve().parent / ".nebula_state"))
).resolve()
FEED_STOP_TIMEOUT = max(1, int(environ.get("FEED_STOP_TIMEOUT", "15")))

# --- CONTROLE DE LOCKS (PROTEÇÃO) ---
ACTIVE_UPLOADS = set()


def is_staging_path(path):
    try:
        target = os.path.abspath(path)
    except (TypeError, ValueError):
        return False
    for root in STAGING_DIRS:
        try:
            if os.path.commonpath([root, target]) == root:
                return True
        except ValueError:
            continue
    return False


MEDIA_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v",
    ".sub", ".ass", ".ssa", ".vtt", ".strm",
}


def _cleanup_empty_parent_dirs(parent_dir: str):
    """Remove parent directories if no media files remain inside them."""
    current = os.path.abspath(parent_dir)
    while current and os.path.exists(current):
        parent_up = os.path.dirname(current)
        if parent_up == current:
            break

        has_media = False
        try:
            for _root, _, files in os.walk(current):
                if any(os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS for f in files):
                    has_media = True
                    break
        except Exception:
            break

        if has_media:
            break

        folder_name = os.path.basename(current).lower()
        if folder_name in ("filmes", "series", "midias"):
            if not os.listdir(current):
                try:
                    os.rmdir(current)
                    logger.info("Removida pasta vazia: %s", current)
                except OSError:
                    pass
            break

        try:
            def _onerror(func, p, _):
                with contextlib.suppress(Exception):
                    os.chmod(p, 0o777)
                    func(p)
            shutil.rmtree(current, onerror=_onerror)
            if not os.path.exists(current):
                logger.info("Removida pasta sem midias: %s", current)
            else:
                break
        except OSError as exc:
            logger.debug("Falha ao remover pasta %s: %s", current, exc)
            break
        current = parent_up


def safe_remove_staging_file(path, force_delete=False):
    try:
        target = os.path.abspath(path)
        in_staging = is_staging_path(target)

        if not in_staging and not force_delete:
            logger.debug("source cleanup skipped outside staging: %s", path)
            return
        os.remove(target)
        logger.info("Removido arquivo: %s", target)
        _cleanup_empty_parent_dirs(os.path.dirname(target))
    except OSError as exc:
        logger.debug("staging cleanup skipped: %s", exc)


def _search_key(value):
    return (value or "").strip().casefold()


STAGING_SAFE_NAME_RE = re.compile(r"^[0-9a-f]{32}_.+")


class SafeStreamHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            super().emit(record)
        except UnicodeEncodeError:
            msg = self.format(record)
            encoding = getattr(self.stream, "encoding", None) or "utf-8"
            msg = msg.encode(encoding, errors="replace").decode(encoding, errors="replace")
            self.stream.write(msg + self.terminator)
            self.flush()


class CompactingFileHandler(RotatingFileHandler):
    def __init__(self, filename, compact_lines, context_file, **kwargs):
        super().__init__(filename, **kwargs)
        self.compact_lines = max(int(compact_lines), 1)
        self.context_file = context_file
        self.line_count = 0
        self.recent_lines = deque(maxlen=self.compact_lines)

    def emit(self, record):
        super().emit(record)
        self.line_count += 1
        self.recent_lines.append(self.format(record))
        if self.line_count >= self.compact_lines:
            self.compact_context()

    def compact_context(self):
        lines = list(self.recent_lines)
        if not lines:
            return
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        completed = sum("Concluido:" in line for line in lines)
        failed = sum(("falhou" in line.lower()) or ("erro" in line.lower()) or ("Traceback" in line) for line in lines)
        queue_lines = [line for line in lines if "Fila:" in line]
        progress_lines = [line for line in lines if "Progresso:" in line]
        tail = lines[-20:]
        summary = [
            f"## Compactacao {now}",
            "",
            f"- linhas compactadas: {len(lines)}",
            f"- concluidos no trecho: {completed}",
            f"- linhas com erro/falha no trecho: {failed}",
        ]
        if queue_lines:
            summary.append(f"- ultimo estado de fila: {queue_lines[-1]}")
        if progress_lines:
            summary.append(f"- ultimo progresso: {progress_lines[-1]}")
        summary.extend(["", "### Ultimas 20 linhas antes da limpeza", "```text", *tail, "```", ""])
        try:
            with open(self.context_file, "a", encoding="utf-8", errors="replace") as fh:
                fh.write("\n".join(summary))
        except Exception:
            pass
        if self.stream:
            try:
                self.stream.flush()
                self.stream.seek(0)
                self.stream.truncate()
            except Exception:
                pass
        self.line_count = 0
        self.recent_lines.clear()


def _get_writable_file_path(filename: str) -> str:
    try:
        p = Path(filename)
        if p.is_absolute():
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                pass
            return str(p)
        with open(filename, "a", encoding="utf-8") as f:
            pass
        return filename
    except Exception:
        pass

    for base in [
        os.environ.get("PROGRAMDATA"),
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("APPDATA"),
        os.environ.get("TEMP"),
    ]:
        if not base:
            continue
        try:
            target_dir = Path(base) / "Mulletaflix" / "nebula"
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / Path(filename).name
            with open(target_path, "a", encoding="utf-8") as f:
                pass
            return str(target_path)
        except Exception:
            continue
    return filename


# --- LOGGING ---
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler = SafeStreamHandler()
console_handler.setFormatter(log_formatter)
logger = logging.getLogger("NebulaFTP")
logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
logger.addHandler(console_handler)

try:
    log_target = _get_writable_file_path(environ.get("LOG_FILE", "nebula.log"))
    context_target = _get_writable_file_path(LOG_CONTEXT_FILE)
    log_handler = CompactingFileHandler(
        log_target,
        compact_lines=LOG_COMPACT_LINES,
        context_file=context_target,
        maxBytes=LOG_MAX_SIZE*1024*1024,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
        errors="replace",
    )
    log_handler.setFormatter(log_formatter)
    logger.addHandler(log_handler)
except Exception as log_err:
    logger.warning("Log em arquivo desabilitado devido a permissoes: %s", log_err)


# --- MÉTRICAS ---
class Metrics:
    uploads_total = 0; uploads_failed = 0; bytes_uploaded = 0
    @classmethod
    def log_success(cls, size): cls.uploads_total += 1; cls.bytes_uploaded += size
    @classmethod
    def log_fail(cls): cls.uploads_failed += 1
    @classmethod
    def report(cls):
        mb = cls.bytes_uploaded / (1024*1024)
        logger.debug(f"Stats runtime: enviados={cls.uploads_total} volume={mb:.2f} MB falhas={cls.uploads_failed}")


async def log_queue_state(mongo, event):
    try:
        counts = {"queued": 0, "staging": 0, "uploading": 0, "completed": 0, "failed": 0}
        async for row in mongo.files.aggregate([
            {"$match": {"type": "file"}},
            {"$group": {"_id": "$status", "count": {"$sum": 1}}},
        ]):
            if row["_id"] in counts:
                counts[row["_id"]] = row["count"]
        queued = counts["queued"]
        staging = counts["staging"]
        uploading = counts["uploading"]
        completed = counts["completed"]
        failed = counts["failed"]
        pending = queued + staging + uploading

        # Lê estatísticas de pendentes físicos do disco do alimentador
        pending_disk = 0
        try:
            feeder_stats = await mongo.stats.find_one({"_id": "feeder"})
            if feeder_stats:
                pending_disk = feeder_stats.get("pending_disk_files", 0)
        except Exception:
            pass

        log_fn = logger.info
        if event == "intervalo":
            log_fn = logger.debug
        elif event.startswith(("enfileirado:", "reenfileirado:")):
            log_fn = logger.debug
        log_fn(
            "Fila: evento=%s stage=%s fila=%s enviando=%s enviados=%s falhas=%s faltam=%s (no disco=%s)",
            event, staging, queued, uploading, completed, failed, pending, pending_disk
        )
    except Exception as exc:
        logger.warning("Fila: falha ao calcular status: %s", exc)


EPISODE_RE = re.compile(r"(?i)(?P<prefix>.*?)(?:[.\s_-]+)?s(?P<season>\d{1,2})e(?P<episode>\d{1,3})")


def series_parent_from_filename(user_root, filename):
    stem = os.path.splitext(filename)[0]
    match = EPISODE_RE.search(stem)
    if not match:
        return None
    series_name = match.group("prefix").strip(" ._-")
    if not series_name:
        return None
    series_name = re.sub(r"[._]+", " ", series_name)
    series_name = re.sub(r"\s+", " ", series_name).strip()
    season = int(match.group("season"))
    return f"{user_root}/Series/{series_name}/Season {season:02d}"


async def resolve_media_parent(mongo, parent, filename):
    stem = os.path.splitext(filename)[0]
    if not is_uploadable_name(filename):
        return parent

    parts = parent.strip("/").split("/")
    default_user_root = f"/{environ.get('NEBULA_LIBRARY_USER', 'raphael')}"
    user_root = f"/{parts[0]}" if parts and parts[0] else default_user_root
    if "Series" in parts:
        return parent

    series_parent = series_parent_from_filename(user_root, filename)
    if series_parent:
        if series_parent != parent:
            logger.info("Roteando episodio para pasta da serie: %s -> %s", filename, series_parent)
        return series_parent

    films_root = f"{user_root}/Filmes"
    if parent.startswith(f"{films_root}/"):
        return parent

    exact_candidates = await mongo.files.find({
        "type": "dir",
        "name": stem,
        "parent": {"$regex": f"^{re.escape(films_root)}(/|$)"},
    }).to_list(length=20)
    if exact_candidates:
        exact_candidates.sort(key=lambda doc: (doc["parent"].count("/")))
        routed = f"{exact_candidates[0]['parent']}/{stem}"
        if routed != parent:
            logger.info("Roteando vídeo para pasta do filme: %s -> %s", filename, routed)
        return routed

    movie_dirs = await mongo.files.find({"type": "dir", "parent": films_root}).to_list(length=5000)
    best = max(movie_dirs, key=lambda doc: movie_folder_score(filename, doc["name"]), default=None)
    if best and movie_folder_score(filename, best["name"]) >= 0.60:
        routed = f"{films_root}/{best['name']}"
        if routed != parent:
            logger.info("Roteando vídeo para pasta do filme: %s -> %s", filename, routed)
        return routed

    now = int(time.time())
    await mongo.files.update_one(
        {"name": "Filmes", "parent": user_root},
        {"$setOnInsert": {"type": "dir", "ctime": now, "mtime": now, "size": 0}},
        upsert=True,
    )
    await mongo.files.update_one(
        {"name": stem, "parent": films_root},
        {"$setOnInsert": {"type": "dir", "ctime": now, "mtime": now, "size": 0}},
        upsert=True,
    )
    routed = f"{films_root}/{stem}"
    logger.info("Roteando vídeo para nova pasta do filme: %s -> %s", filename, routed)
    return routed


async def stats_reporter(mongo):
    while True:
        try:
            for staging_dir in STAGING_DIRS:
                if not os.path.exists(staging_dir):
                    continue
                for root, dirs, files in os.walk(staging_dir):
                    if os.path.normcase(root) == os.path.normcase(staging_dir):
                        dirs[:] = [name for name in dirs if name.lower() != "strm"]
                    for f in files:
                        if f.endswith(".partial"):
                            continue
                        if not is_uploadable_name(f):
                            continue
                        fp = os.path.join(root, f)

                        if not os.path.isfile(fp):
                            continue

                        # Ignora se já estiver sendo enviado (evita duplicar na fila)
                        if fp in ACTIVE_UPLOADS:
                            continue

                        existing_by_path = await mongo.files.find_one({
                            "local_path": fp,
                            "type": "file"
                        })
                        if existing_by_path and existing_by_path.get("status") in {"queued", "uploading", "completed", "staging"}:
                            continue

                        try:
                            size_t1 = os.path.getsize(fp)
                        except FileNotFoundError:
                            continue
                        if size_t1 == 0:
                            continue

                        display_name = f
                        if root == staging_dir and STAGING_SAFE_NAME_RE.match(f):
                            display_name = f[33:]

                        existing_active = await mongo.files.find_one({
                            "name": display_name,
                            "status": {"$in": ["queued", "uploading", "completed", "staging"]}
                        })
                        if existing_active:
                            continue

                        rel_dir = os.path.relpath(root, staging_dir)

                        if rel_dir == ".":
                            parent_path = "/"
                        else:
                            normalized_rel = rel_dir.replace(os.sep, "/")
                            parent_path = f"/{normalized_rel}"

                        parent_path = await resolve_media_parent(mongo, parent_path, display_name)
                        doc = await mongo.files.find_one({"name": display_name, "parent": parent_path})
                        if not doc and existing_by_path and existing_by_path.get("status") in {"failed", "staging"}:
                            doc = existing_by_path
                        if doc and doc.get("status") in {"queued", "uploading", "completed", "staging"}:
                            continue

                        if doc and doc.get("status") in {"failed", "staging"}:
                            await mongo.files.update_one(
                                {"_id": doc["_id"]},
                                {"$set": {"name": display_name, "parent": parent_path, "status": "queued", "local_path": fp, "size": size_t1}}
                            )
                            await UPLOAD_QUEUE.put({
                                "path": fp, "filename": display_name, "parent": parent_path, "size": size_t1
                            })
                            logger.debug(f"Reenfileirado: {display_name}")
                            await log_queue_state(mongo, f"reenfileirado:{display_name}")
                        elif not doc:
                            await asyncio.sleep(2)
                            try:
                                if os.path.getsize(fp) != size_t1:
                                    continue
                            except FileNotFoundError:
                                continue

                            logger.debug(f"Detectado: {display_name} -> {parent_path}")

                            if parent_path != "/":
                                parts = parent_path.strip("/").split("/")
                                current_parent = "/"
                                for part in parts:
                                    await mongo.files.update_one(
                                        {"name": part, "parent": current_parent},
                                        {"$setOnInsert": {"type": "dir", "ctime": int(time.time()), "mtime": int(time.time()), "size": 0}},
                                        upsert=True
                                    )
                                    if current_parent == "/":
                                        current_parent = "/" + part
                                    else:
                                        current_parent = f"{current_parent}/{part}"

                            file_doc = {
                                "type": "file", "name": display_name, "parent": parent_path, "size": size_t1,
                                "status": "queued", "local_path": fp,
                                "mtime": int(time.time()), "ctime": int(time.time()), "parts": [],
                                "search_name": _search_key(display_name), "search_parent": _search_key(parent_path)
                            }

                            try:
                                await mongo.files.insert_one(file_doc)
                                await UPLOAD_QUEUE.put({
                                    "path": fp, "filename": display_name, "parent": parent_path, "size": size_t1
                                })
                                logger.debug(f"Enfileirado: {display_name}")
                                await log_queue_state(mongo, f"enfileirado:{display_name}")
                            except Exception as e:
                                logger.warning(f"Erro registro {display_name}: {e}")

        except Exception as e:
            logger.error(f"? Erro Watcher: {e}")

        await asyncio.sleep(5)



async def cleanup_strm_duplicate_records(mongo):
    pattern = re.compile(r"^[0-9a-f]{24}\.[^.]+$", re.IGNORECASE)
    removed = 0
    async for doc in mongo.files.find(
        {"type": "file", "name": {"$regex": pattern}},
        {"_id": 1, "name": 1, "local_path": 1, "size": 1},
    ):
        local_path = doc.get("local_path")
        if not local_path:
            continue
        sibling = None
        async for candidate in mongo.files.find(
            {
                "_id": {"$ne": doc["_id"]},
                "type": "file",
                "size": doc.get("size"),
                "name": {"$not": pattern},
            },
            {"_id": 1, "local_path": 1},
        ):
            candidate_path = candidate.get("local_path")
            if candidate_path and os.path.basename(candidate_path) == os.path.basename(local_path):
                sibling = candidate
                break
        if sibling:
            await mongo.files.delete_one({"_id": doc["_id"]})
            removed += 1
    if removed:
        logger.warning("Removidos %s registros temporarios STRM duplicados.", removed)


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


def get_media_category_priority(parent: str, filename: str) -> int:
    """Retorna a prioridade da categoria: Filmes (0), Porno (1), Series (2), Outros (3)."""
    p_lower = (parent or "").lower()
    f_lower = (filename or "").lower()
    if any(x in p_lower for x in ["/filmes", "filmes", "filme", "movie", "movies"]):
        return 0
    if any(x in p_lower for x in ["/porno", "porno", "porn", "xxx", "hentai", "adulto"]) or re.search(r"\b(porno|porn|xxx|hentai|adulto)\b", f_lower):
        return 1
    if any(x in p_lower for x in ["/series", "series", "serie"]) or re.search(r"(?i)\bS\d{1,2}[ ._-]*E\d{1,3}\b|\b\d{1,2}x\d{1,3}\b|season", f_lower):
        return 2
    return 3


def doc_download_timestamp(doc):
    mt = doc.get("mtime") or doc.get("ctime")
    if mt:
        return mt
    lp = doc.get("local_path")
    if lp and os.path.exists(lp):
        try:
            return os.path.getmtime(lp)
        except OSError:
            pass
    return 0


async def resolve_local_path(mongo, doc):
    """Verifica se o local_path existe; se nao existir, tenta localiza-lo em qualquer STAGING_DIRS configurado."""
    local_path = doc.get("local_path")
    if local_path and os.path.exists(local_path):
        return local_path

    filename = doc.get("name") or (os.path.basename(local_path) if local_path else None)
    if not filename:
        return None

    for stage_root in STAGING_DIRS:
        candidates = [
            os.path.join(stage_root, filename),
            os.path.join(stage_root, "strm", filename),
        ]
        if local_path:
            try:
                rel = os.path.splitdrive(local_path)[1].lstrip("\\/")
                for prefix in ("NebulaStage", "staging"):
                    if rel.lower().startswith(prefix.lower()):
                        rel = rel[len(prefix):].lstrip("\\/")
                candidates.append(os.path.join(stage_root, rel))
            except Exception:
                pass
        for cand in candidates:
            if os.path.exists(cand) and os.path.isfile(cand):
                abs_cand = os.path.abspath(cand)
                try:
                    await mongo.files.update_one({"_id": doc["_id"]}, {"$set": {"local_path": abs_cand}})
                except Exception:
                    pass
                doc["local_path"] = abs_cand
                return abs_cand
    return None


async def restore_pending_uploads(mongo):
    count = 0
    query = {"type": "file", "status": {"$in": ["queued", "uploading", "staging"]}, "local_path": {"$exists": True}}
    pending = [doc async for doc in mongo.files.find(query)]
    pending.sort(key=lambda doc: (
        doc_download_timestamp(doc),
        doc.get("name", "").lower(),
    ))
    for doc in pending:
        local_path = await resolve_local_path(mongo, doc)
        if not local_path or not os.path.exists(local_path):
            await mongo.files.update_one(
                {"_id": doc["_id"]},
                {"$set": {"status": "failed", "failed_at": int(time.time()), "failed_reason": "local_path_missing"}},
            )
            continue
        parent = await resolve_media_parent(mongo, doc["parent"], doc["name"])
        duplicate = await mongo.files.find_one(
            {"_id": {"$ne": doc["_id"]}, "parent": parent, "name": doc["name"]},
            {"_id": 1},
        )
        if duplicate:
            await mongo.files.update_one(
                {"_id": doc["_id"]},
                {"$set": {"status": "failed", "failed_at": int(time.time()), "failed_reason": "duplicate_target"}},
            )
            logger.warning("Fila restaurada ignorou duplicado: %s em %s", doc["name"], parent)
            continue
        try:
            await mongo.files.update_one({"_id": doc["_id"]}, {"$set": {"status": "staging", "parent": parent}})
        except DuplicateKeyError:
            await mongo.files.update_one(
                {"_id": doc["_id"]},
                {"$set": {"status": "failed", "failed_at": int(time.time()), "failed_reason": "duplicate_target"}},
            )
            logger.warning("Fila restaurada encontrou duplicado concorrente: %s em %s", doc["name"], parent)
            continue
        await UPLOAD_QUEUE.put({
            "path": local_path,
            "filename": doc["name"],
            "parent": parent,
            "size": doc.get("size", os.path.getsize(local_path)),
        })
        count += 1
    logger.info("Fila restaurada: %s arquivo(s) pendente(s)", count)
    await log_queue_state(mongo, "restauracao")


async def queued_mongo_scanner(mongo, max_workers=None):
    """Scans MongoDB for 'queued' files with local_path in oldest-downloaded-first order (mtime/ctime ascending) and moves them to UPLOAD_QUEUE."""
    if max_workers is None:
        max_workers = min(MAX_WORKERS, UPLOAD_CONCURRENCY)
    logger.info("Iniciando queued_mongo_scanner ordenado pelo arquivo mais antigo baixado (intervalo=1s, max_por_iteracao=%d)", max_workers)

    while True:
        try:
            found = 0
            for _ in range(max_workers):
                q = {"type": "file", "status": "queued", "local_path": {"$exists": True}}
                doc = await mongo.files.find_one_and_update(
                    q,
                    {"$set": {"status": "staging", "staged_at": int(time.time())}},
                    sort=[("mtime", 1), ("ctime", 1), ("_id", 1)],
                )
                if not doc:
                    break
                local_path = await resolve_local_path(mongo, doc)
                if not local_path or not os.path.exists(local_path):
                    await mongo.files.update_one(
                        {"_id": doc["_id"]},
                        {"$set": {"status": "failed", "failed_at": int(time.time()), "failed_reason": "local_path_missing"}},
                    )
                    logger.warning("Scanner Mongo: arquivo sem local_path valido: %s", doc.get("name"))
                    continue
                parent = await resolve_media_parent(mongo, doc["parent"], doc["name"])
                await mongo.files.update_one({"_id": doc["_id"]}, {"$set": {"parent": parent}})
                await UPLOAD_QUEUE.put({
                    "path": local_path,
                    "filename": doc["name"],
                    "parent": parent,
                    "size": doc.get("size", os.path.getsize(local_path)),
                })
                found += 1
                logger.info("Scanner Mongo: enfileirado para upload (mais antigo): %s (mtime=%s, local_path=%s)", doc["name"], doc.get("mtime"), local_path)
            if found:
                logger.info("Scanner Mongo: %s arquivo(s) movidos para fila de upload (max_por_iteracao=%d)", found, max_workers)
            else:
                logger.debug("Scanner Mongo: nenhum arquivo 'queued' com local_path encontrado")
        except Exception as exc:
            logger.warning("Scanner Mongo aguardando: %s", exc)
        await asyncio.sleep(1)


async def staging_scanner(mongo, staging_dirs):
    """Monitora pastas de stage (STAGING_DIRS) e registra arquivos pendentes/orfãos no MongoDB."""
    logger.info("Iniciando scanner de pastas de stage: %s", staging_dirs)
    while True:
        try:
            for stage_root in staging_dirs:
                stage_path = Path(stage_root)
                if not stage_path.exists():
                    continue
                # Busca arquivos de midia no stage
                for ext in (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".webm"):
                    for file_path in stage_path.rglob(f"*{ext}"):
                        if not file_path.is_file():
                            continue
                        # Ignora arquivos temporarios ou de download parcial
                        name_str = file_path.name
                        if name_str.startswith(".") or ".part" in name_str.lower() or name_str.endswith(".download"):
                            continue
                        try:
                            size = file_path.stat().st_size
                            if size == 0:
                                continue
                            file_stat = file_path.stat()
                            file_mtime = int(file_stat.st_mtime)
                            file_ctime = int(file_stat.st_ctime)

                            # Inferir nome da midia
                            inferred_name = file_path.name
                            try:
                                rel = file_path.relative_to(stage_path)
                                parts = list(rel.parts)
                                if len(parts) >= 2 and parts[0] in ("Filmes", "Series", "Porno"):
                                    if parts[0] == "Filmes" and len(parts) >= 3:
                                        inferred_name = f"{parts[1]}{file_path.suffix}"
                                    elif parts[0] == "Series" and len(parts) >= 3:
                                        inferred_name = file_path.name
                                    elif parts[0] == "Porno":
                                        inferred_name = file_path.name
                            except (ValueError, IndexError):
                                pass

                            # Verifica se ja existe no MongoDB por local_path ou por nome
                            existing = await mongo.files.find_one({
                                "$or": [
                                    {"local_path": str(file_path)},
                                    {"name": inferred_name, "status": "completed"},
                                    {"name": file_path.name, "status": "completed"},
                                    {"name": inferred_name, "status": {"$in": ["queued", "staging", "uploading"]}},
                                    {"name": file_path.name, "status": {"$in": ["queued", "staging", "uploading"]}},
                                    {"name": inferred_name, "status": "failed"},
                                    {"name": file_path.name, "status": "failed"},
                                ]
                            })

                            if existing:
                                st = existing.get("status")
                                # Ja enviado e concluido no Telegram
                                if st == "completed":
                                    continue
                                # Em fila ou enviando: garante que o local_path atualizado aponta para onde o arquivo esta
                                if st in ("queued", "staging", "uploading"):
                                    if existing.get("local_path") != str(file_path):
                                        await mongo.files.update_one(
                                            {"_id": existing["_id"]},
                                            {"$set": {"local_path": str(file_path), "size": size}},
                                        )
                                    continue
                                # Se estava com falha (ex: local_path_missing de quando o disco F: foi desconectado), reativa para queued!
                                await mongo.files.update_one(
                                    {"_id": existing["_id"]},
                                    {"$set": {
                                        "status": "queued",
                                        "local_path": str(file_path),
                                        "size": size,
                                        "mtime": file_mtime,
                                        "failed_reason": None,
                                    }},
                                )
                                logger.info("Stage scanner: reativado %s (local_path: %s)", inferred_name, file_path)
                                continue

                            # Arquivo novo no stage - registra como queued no MongoDB
                            parent = await resolve_media_parent(mongo, "", inferred_name)
                            doc = {
                                "type": "file",
                                "name": inferred_name,
                                "parent": parent,
                                "size": size,
                                "status": "queued",
                                "local_path": str(file_path),
                                "mtime": file_mtime,
                                "ctime": file_ctime,
                                "parts": [],
                                "delete_source": False,
                            }
                            await mongo.files.insert_one(doc)
                            logger.info("Stage scanner: novo arquivo detectado no stage e enfileirado: %s", inferred_name)
                        except Exception as e:
                            logger.warning("Stage scanner erro em %s: %s", file_path, e)
        except Exception as exc:
            logger.warning("Stage scanner aguardando: %s", exc)
        await asyncio.sleep(5)

async def upload_part_with_retries(worker_id, bots, target_chat_id, local_path, file_uuid, part_num, chunk_data=None, bot_index_offset=0, caption=""):
    """Upload a single part with bot rotation on FloodWait.

    Instead of being pinned to one bot, this function accepts the full bots
    list and rotates to the next bot after each FloodWait.  This prevents
    one bot's rate-limit from blocking an entire batch of parts.

    Also eliminates the BytesIO double-buffer: raw bytes are passed directly
    to Pyrogram (which wraps them in InputFile internally).
    """
    chunk_name = f"{file_uuid}.part_{part_num:03d}"
    if chunk_data is None:
        async with aiofiles.open(local_path, "rb") as f:
            await f.seek(part_num * CHUNK_SIZE)
            chunk_data = await f.read(CHUNK_SIZE)
    if not chunk_data:
        raise Exception(f"Parte vazia {part_num}")

    # Rotate globally so successive media parts do not keep concentrating
    # traffic on the same worker-specific subset of bots.
    if not bots:
        raise RuntimeError("Nenhum bot Telegram disponível para upload. Verifique os tokens configurados.")
    current_bot_idx = next_upload_bot_index(len(bots))
    sent_msg = None
    attempt = 1
    while attempt <= MAX_RETRIES:
        part_bot = bots[current_bot_idx % len(bots)]
        bot_number = bot_index_offset + (current_bot_idx % len(bots)) + 1
        bot_name = getattr(part_bot, "name", None)
        try:
            started_at = time.monotonic()
            logger.info("[UPLOAD] W%s parte=%s bot=#%s iniciando", worker_id, part_num, bot_number)
            sent_msg = await _send_part_on_idle_bot(
                part_bot,
                target_chat_id,
                chunk_data,
                chunk_name,
                caption=caption,
            )
            if sent_msg is None:
                raise RuntimeError("Telegram encerrou o upload sem retornar mensagem")
            elapsed = max(time.monotonic() - started_at, 0.001)
            logger.info(
                "[UPLOAD] W%s parte=%s bot=#%s concluida em %.1fs (%.2f MB/s)",
                worker_id, part_num, bot_number, elapsed,
                len(chunk_data) / 1024 / 1024 / elapsed,
            )
            break
        except FloodWait as e:
            w = e.value + 2
            logger.warning(
                f"[W{worker_id}] FloodWait bot#{bot_number}: {w}s, "
                f"rotating to next bot"
            )
            attempt += 1
            current_bot_idx += 1
            if len(bots) == 1:
                await asyncio.sleep(w)
        except (TimeoutError, ConnectionError) as e:
            logger.warning(
                "[W%s] Timeout bot#%s: %s; rotating to next bot",
                worker_id,
                bot_number,
                e,
            )
            attempt += 1
            current_bot_idx += 1
            await asyncio.sleep(1)
        except RPCError as e:
            w = 2 ** attempt
            logger.error(f"[W{worker_id}] Erro TG ({attempt}) bot#{bot_number}: {e}")
            attempt += 1
            await asyncio.sleep(w)
        except Exception as e:
            logger.error(f"[W{worker_id}] Erro: {e}")
            attempt += 1
            await asyncio.sleep(5)
    if not sent_msg:
        raise Exception(f"Falha upload parte {part_num}")
    return {
        "part_id": part_num,
        "tg_file": sent_msg.document.file_id,
        "tg_message": sent_msg.id,
        "file_size": len(chunk_data),
        "chunk_name": chunk_name,
        "byte_start": part_num * CHUNK_SIZE,
        "byte_end": part_num * CHUNK_SIZE + len(chunk_data) - 1,
        "caption": caption,
        "bot_index": bot_index_offset + (current_bot_idx % len(bots)),
        "bot_name": bot_name,
    }


async def _readahead_producer(
    local_path,
    total_parts,
    queue,
    worker_id,
    start_part=0,
    start_offset=None,
):
    """Pre-read file chunks into an async queue ahead of upload workers.

    This overlaps disk I/O with network I/O: while workers upload chunk N,
    this producer is already reading chunk N+1 into memory.  The queue
    bounds memory usage to ``queue.maxsize * CHUNK_SIZE`` bytes.
    """
    async with aiofiles.open(local_path, "rb") as f:
        if start_offset is None:
            start_offset = start_part * CHUNK_SIZE
        if start_offset:
            await f.seek(start_offset)
        for part_num in range(start_part, total_parts):
            chunk_data = await f.read(CHUNK_SIZE)
            if not chunk_data:
                break
            await queue.put((part_num, chunk_data))
    # Signal end-of-stream
    await queue.put(None)
    logger.debug(f"[W{worker_id}] Read-ahead: all {total_parts} chunks queued")

async def upload_worker(bot, target_chat_id, mongo, worker_id, bot_index=0):
    logger.debug(f"👷 Worker #{worker_id} Pronto (bot #{bot_index + 1})")

    while True:
        try:
            task = await asyncio.wait_for(UPLOAD_QUEUE.get(), timeout=2.0)
        except TimeoutError:
            continue

        local_path = task["path"]; filename = task["filename"]; parent = task["parent"]

        # --- LOCK: Bloqueia o arquivo para o GC não apagar ---
        ACTIVE_UPLOADS.add(local_path)
        # -----------------------------------------------------

        try:
            if filename.endswith(".partial"):
                continue

            if not os.path.exists(local_path):
                continue

            real_size = os.path.getsize(local_path)
            if real_size == 0:
                safe_remove_staging_file(local_path)
                continue

            parent = await resolve_media_parent(mongo, parent, filename)
            total_parts = max(1, (real_size + CHUNK_SIZE - 1) // CHUNK_SIZE)
            logger.info(f"[W{worker_id}] Iniciando upload: {filename} tamanho={real_size/1024/1024:.2f} MB partes={total_parts}")

            status_msg = None
            if UPLOAD_STATUS_MESSAGES:
                try:
                    status_msg = await bot.send_message(
                        target_chat_id,
                        f"Upload iniciado: {parent}/{filename} ({real_size/1024/1024:.2f} MB)",
                        disable_notification=True,
                    )
                except (RPCError, ConnectionError) as exc:
                    logger.debug("upload status message skipped: %s", exc)

            file_doc = await mongo.files.find_one({"name": filename, "parent": parent})
            if not file_doc:
                file_doc = await mongo.files.find_one({"name": filename, "local_path": local_path})
            if not file_doc:
                logger.warning(f"⚠️ [W{worker_id}] Metadados não encontrados: {filename}")
                continue

            claimed_doc = await mongo.files.find_one_and_update(
                {"_id": file_doc["_id"], "status": {"$in": ["queued", "staging"]}},
                {"$set": {"parent": parent, "status": "uploading", "worker_id": worker_id, "bot_index": bot_index + 1, "started_at": int(time.time())}},
                return_document=ReturnDocument.AFTER,
            )
            if not claimed_doc:
                logger.info(f"[W{worker_id}] Ignorado ja processado: {filename}")
                continue
            file_doc = claimed_doc
            await log_queue_state(mongo, f"inicio:{filename}")

            file_uuid = str(uuid.uuid4())
            media_type = classify_media_type(parent, filename)
            parts_metadata = []
            upload_failed = False
            uploaded_bytes = 0
            total_parts = (real_size + CHUNK_SIZE - 1) // CHUNK_SIZE

            try:
                async with aiofiles.open(local_path, "rb") as f:
                    part_num = 0
                    while True:
                        chunk_data = await f.read(CHUNK_SIZE)
                        if not chunk_data:
                            break

                        chunk_name = f"{file_uuid}.part_{part_num:03d}"
                        caption = build_part_caption(media_type, filename, part_num, total_parts, file_uuid, real_size)
                        mem_file = io.BytesIO(chunk_data); mem_file.name = chunk_name
                        sent_msg = None

                        for attempt in range(1, MAX_RETRIES + 1):
                            try:
                                mem_file.seek(0)
                                sent_msg = await bot.send_document(
                                    chat_id=target_chat_id,
                                    document=mem_file,
                                    file_name=chunk_name,
                                    force_document=True,
                                    caption=caption
                                )
                                break
                            except FloodWait as e:
                                w = e.value + 2; logger.warning(f"⏳ [W{worker_id}] FloodWait: {w}s")
                                await asyncio.sleep(w)
                            except RPCError as e:
                                w = (2 ** attempt); logger.error(f"❌ [W{worker_id}] Erro TG ({attempt}): {e}")
                                await asyncio.sleep(w)
                            except Exception as e:
                                logger.error(f"❌ [W{worker_id}] Erro: {e}"); await asyncio.sleep(5)

                        if not sent_msg:
                            raise Exception(f"Falha upload parte {part_num}")

                        parts_metadata.append({
                            "part_id": part_num,
                            "tg_file": sent_msg.document.file_id,
                            "tg_message": sent_msg.id,
                            "file_size": len(chunk_data),
                            "chunk_name": chunk_name,
                            "byte_start": part_num * CHUNK_SIZE,
                            "byte_end": part_num * CHUNK_SIZE + len(chunk_data) - 1,
                            "caption": caption,
                            "media_type": media_type,
                            "bot_index": bot_index,
                            "bot_name": getattr(bot, "name", None),
                        })
                        uploaded_bytes += len(chunk_data)
                        part_num += 1
                        percent = uploaded_bytes / real_size * 100
                        logger.debug(
                            f"[W{worker_id}] Progresso: {filename} parte={part_num}/{total_parts} "
                            f"percentual={percent:.1f}% enviado={uploaded_bytes/1024/1024:.2f}/{real_size/1024/1024:.2f} MB"
                        )
                        if status_msg and (part_num == 1 or part_num % 5 == 0):
                            with contextlib.suppress(RPCError, ConnectionError):
                                await status_msg.edit_text(
                                    f"Upload em andamento: {parent}/{filename}\n"
                                    f"{percent:.1f}% ({uploaded_bytes/1024/1024:.2f}/{real_size/1024/1024:.2f} MB)"
                                )

            except Exception as e:
                logger.error(f"❌ [W{worker_id}] Abortado: {filename}: {e}"); upload_failed = True; Metrics.log_fail()

            if upload_failed and status_msg:
                with contextlib.suppress(RPCError, ConnectionError):
                    await status_msg.edit_text(f"Upload falhou: {parent}/{filename}")
            if upload_failed:
                await mongo.files.update_one(
                    {"_id": file_doc["_id"]},
                    {"$set": {"status": "failed", "failed_at": int(time.time())}},
                )
                await log_queue_state(mongo, f"falha:{filename}")

            if not upload_failed:
                parts_metadata.sort(key=lambda item: item["part_id"])
                await mongo.files.update_one(
                    {"_id": file_doc["_id"]},
                    {
                        "$set": {
                            "size": real_size,
                            "media_type": media_type,
                            "part_count": total_parts,
                            "uploaded_at": int(time.time()),
                            "parts": parts_metadata,
                            "obfuscated_id": file_uuid,
                            "status": "completed",
                            "stream_bot_name": getattr(bot, "name", None),
                            "search_name": filename.lower(),
                            "search_parent": parent.lower(),
                        },
                        "$unset": {"uploadId": 1, "local_path": 1},
                    }
                )
                async with MongoDBPathIO._cache_lock:
                    MongoDBPathIO._memory_cache.pop(f"{parent}::{filename}", None)
                logger.info(f"[W{worker_id}] Concluido: {filename}")
                await log_queue_state(mongo, f"concluido:{filename}")
                if status_msg:
                    with contextlib.suppress(RPCError, ConnectionError):
                        await status_msg.edit_text(f"Upload concluido: {parent}/{filename}")
                Metrics.log_success(real_size)
                force_del = bool(file_doc.get("delete_source")) if file_doc else False
                safe_remove_staging_file(local_path, force_delete=force_del)

        except Exception as e:
            logger.error(f"❌ [W{worker_id}] Crítico: {e}")
        finally:
            # --- UNLOCK: Libera o arquivo ---
            ACTIVE_UPLOADS.discard(local_path)
            UPLOAD_QUEUE.task_done()

async def upload_worker_parallel(bots, target_chat_id, mongo, worker_id):
    """Upload worker with bot rotation and read-ahead.

    Each worker now receives the full ``bots[]`` list so that parts within
    a batch are distributed across bots, and a single bot's FloodWait
    doesn't block the entire batch.  A read-ahead producer overlaps disk
    I/O with network I/O by pre-filling an async queue of chunks.
    """
    logger.debug(f"Worker #{worker_id} pronto (bots={len(bots)}, partes={PART_WORKERS_PER_FILE})")
    while True:
        try:
            task = await asyncio.wait_for(UPLOAD_QUEUE.get(), timeout=2.0)
        except TimeoutError:
            continue

        local_path = task["path"]
        filename = task["filename"]
        parent = task["parent"]
        ACTIVE_UPLOADS.add(local_path)
        try:
            if filename.endswith(".partial") or not os.path.exists(local_path):
                continue
            real_size = os.path.getsize(local_path)
            if real_size == 0:
                safe_remove_staging_file(local_path)
                continue

            parent = await resolve_media_parent(mongo, parent, filename)
            file_doc = await mongo.files.find_one({"name": filename, "parent": parent})
            if not file_doc:
                file_doc = await mongo.files.find_one({"name": filename, "local_path": local_path})
            if not file_doc:
                logger.warning(f"[W{worker_id}] Metadados nao encontrados: {filename}")
                continue

            file_doc = await mongo.files.find_one_and_update(
                {"_id": file_doc["_id"], "status": {"$in": ["queued", "staging"]}},
                {"$set": {"parent": parent, "status": "uploading", "worker_id": worker_id, "started_at": int(time.time())}},
                return_document=ReturnDocument.AFTER,
            )
            if not file_doc:
                logger.info(f"[W{worker_id}] Ignorado ja processado: {filename}")
                continue
            await log_queue_state(mongo, f"inicio:{filename}")

            parts_metadata = get_contiguous_uploaded_parts(file_doc.get("parts"))
            resume_part = len(parts_metadata)
            uploaded_bytes = sum(int(part["file_size"]) for part in parts_metadata)
            if uploaded_bytes > real_size:
                raise RuntimeError(
                    f"Metadados excedem arquivo: {uploaded_bytes}/{real_size} bytes"
                )
            remaining_bytes = real_size - uploaded_bytes
            remaining_parts = (remaining_bytes + CHUNK_SIZE - 1) // CHUNK_SIZE
            total_parts = resume_part + remaining_parts
            logger.info(
                f"[W{worker_id}] Iniciando upload: {filename} "
                f"tamanho={real_size/1024/1024:.2f} MB partes={total_parts} "
                f"paralelo={PART_WORKERS_PER_FILE} bots={len(bots)}"
            )
            first_chunk_name = parts_metadata[0].get("chunk_name", "") if parts_metadata else ""
            file_uuid = (
                first_chunk_name.rsplit(".part_", 1)[0]
                if ".part_" in first_chunk_name
                else str(uuid.uuid4())
            )
            if resume_part:
                logger.info(
                    "[W%s] Retomando %s na parte %s/%s (%.2f MB ja enviados)",
                    worker_id,
                    filename,
                    resume_part,
                    total_parts,
                    uploaded_bytes / 1024 / 1024,
                )

            # --- Read-ahead: start producer that fills a bounded queue ---
            readahead_queue: asyncio.Queue = asyncio.Queue(
                maxsize=PART_WORKERS_PER_FILE
            )
            producer_task = asyncio.create_task(
                _readahead_producer(
                    local_path,
                    total_parts,
                    readahead_queue,
                    worker_id,
                    resume_part,
                    uploaded_bytes,
                )
            )

            for start_part in range(resume_part, total_parts, PART_WORKERS_PER_FILE):
                batch_end = min(start_part + PART_WORKERS_PER_FILE, total_parts)
                batch_size = batch_end - start_part

                # Collect pre-read chunks from the queue (or fallback to disk)
                chunk_map: dict[int, bytes] = {}
                for _ in range(batch_size):
                    item = await readahead_queue.get()
                    if item is None:
                        break
                    part_num, chunk_data = item
                    chunk_map[part_num] = chunk_data

                media_type = classify_media_type(parent, filename)

                # Upload each part in the batch — bot rotation happens
                # inside upload_part_with_retries via the bots[] list.
                async def _upload_one(pn: int) -> dict:
                    chunk_data = chunk_map.get(pn)
                    caption = build_part_caption(media_type, filename, pn, total_parts, file_uuid, real_size)
                    return await upload_part_with_retries(
                        worker_id, bots, target_chat_id, local_path, file_uuid, pn, chunk_data, 1, caption=caption
                    )

                results = await asyncio.gather(
                    *[_upload_one(pn) for pn in range(start_part, batch_end)]
                )
                parts_metadata.extend(results)
                parts_metadata.sort(key=lambda item: item["part_id"])
                uploaded_bytes += sum(item["file_size"] for item in results)
                percent = uploaded_bytes / real_size * 100
                stream_bot_name = next((item.get("bot_name") for item in parts_metadata if item.get("bot_name")), None)
                await mongo.files.update_one(
                    {"_id": file_doc["_id"]},
                    {"$set": {"uploaded_bytes": uploaded_bytes, "parts": parts_metadata, "stream_bot_name": stream_bot_name}}
                )
                logger.debug(
                    f"[W{worker_id}] Progresso: {filename} parte={batch_end}/{total_parts} "
                    f"percentual={percent:.1f}% enviado={uploaded_bytes/1024/1024:.2f}/{real_size/1024/1024:.2f} MB"
                )

            # Ensure producer finishes cleanly
            if not producer_task.done():
                producer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await producer_task

            if len(parts_metadata) != total_parts or uploaded_bytes != real_size:
                raise RuntimeError(
                    f"Upload incompleto: partes={len(parts_metadata)}/{total_parts} "
                    f"bytes={uploaded_bytes}/{real_size}"
                )
            parts_metadata.sort(key=lambda item: item["part_id"])
            stream_bot_name = next(
                (item.get("bot_name") for item in parts_metadata if item.get("bot_name")),
                None,
            )
            await mongo.files.update_one(
                {"_id": file_doc["_id"]},
                {
                    "$set": {
                        "size": real_size,
                        "media_type": media_type,
                        "part_count": total_parts,
                        "uploaded_at": int(time.time()),
                        "parts": parts_metadata,
                        "obfuscated_id": file_uuid,
                        "status": "completed",
                        "stream_bot_name": stream_bot_name,
                        "search_name": filename.lower(),
                        "search_parent": parent.lower(),
                    },
                    "$unset": {"uploadId": 1, "local_path": 1},
                },
            )
            async with MongoDBPathIO._cache_lock:
                MongoDBPathIO._memory_cache.pop(f"{parent}::{filename}", None)
            logger.info(f"[W{worker_id}] Concluido: {filename}")
            await log_queue_state(mongo, f"concluido:{filename}")
            Metrics.log_success(real_size)
            force_del = bool(file_doc.get("delete_source")) if file_doc else False
            safe_remove_staging_file(local_path, force_delete=force_del)
        except Exception as exc:
            logger.error(f"[W{worker_id}] Abortado: {filename}: {exc}")
            Metrics.log_fail()
            with contextlib.suppress(Exception):
                await mongo.files.update_one(
                    {"name": filename, "local_path": local_path},
                    {"$set": {"status": "failed", "failed_at": int(time.time()), "failed_reason": str(exc)}},
                )
            await log_queue_state(mongo, f"falha:{filename}")
        finally:
            ACTIVE_UPLOADS.discard(local_path)
            UPLOAD_QUEUE.task_done()

async def resolve_channel(bot):
    raw_chat = environ.get("CHAT_ID")
    target_chat = int(raw_chat) if raw_chat and raw_chat.lstrip("-").isdigit() else raw_chat

    logger.info("🔍 Verificando acesso ao canal...")

    try:
        chat = await bot.get_chat(target_chat)
        logger.info(f"✅ Canal Confirmado: {chat.title} (ID: {chat.id})")
        try:
            await bot.send_message(
                chat.id,
                "🔄 Nebula FTP MonoBot Conectado",
                disable_notification=True,
            )
        except (RPCError, ConnectionError) as exc:
            logger.debug("startup ping skipped: %s", exc)
        return chat.id
    except Exception as e:
        logger.critical(f"❌ Canal inválido '{target_chat}': {e}"); return None

def http_headers(status, content_type, body=b"", extra=None, include_body=True):
    reason = {
        200: "OK",
        206: "Partial Content",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        416: "Range Not Satisfiable",
        500: "Server Error",
    }.get(status, "OK")
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "Connection": "close",
    }
    if extra:
        headers.update(extra)
    head = [f"HTTP/1.1 {status} {reason}", *[f"{k}: {v}" for k, v in headers.items()], "", ""]
    return "\r\n".join(head).encode("utf-8") + (body if include_body else b"")


async def http_write_json(writer, data, status=200, head_only=False):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    writer.write(http_headers(status, "application/json; charset=utf-8", body, include_body=not head_only))
    await writer.drain()


async def list_completed_files(mongo, query, limit):
    limit = min(max(int(limit or 100), 1), 500)
    criteria: dict[str, Any] = {"type": "file", "status": "completed", "parts.0": {"$exists": True}}
    if query:
        search = _search_key(query)
        if search:
            criteria["search_name"] = {"$gte": search, "$lt": f"{search}\uffff"}
    cursor = mongo.files.find(criteria, {"name": 1, "parent": 1, "size": 1, "uploaded_at": 1})
    if query:
        cursor = cursor.hint("type_1_status_1_search_name_1_uploaded_at_-1")
        cursor = cursor.sort("search_name", 1)
    else:
        cursor = cursor.sort("uploaded_at", -1)
    cursor = cursor.limit(limit)
    files = []
    async for doc in cursor:
        files.append({
            "id": str(doc["_id"]),
            "name": doc.get("name", ""),
            "parent": doc.get("parent", ""),
            "path": f"{doc.get('parent', '')}/{doc.get('name', '')}",
            "size": doc.get("size", 0),
            "uploaded_at": doc.get("uploaded_at"),
            "stream": f"/stream?id={quote(str(doc['_id']))}",
            "play": f"/play?id={quote(str(doc['_id']))}",
        })
    return files


async def http_index(writer, mongo, query, head_only=False):
    files = await list_completed_files(mongo, query, 100)
    rows = "\n".join(
        f"<tr><td>{escape(f['path'])}</td><td>{f['size']}</td>"
        f"<td><a href='{f['play']}'>assistir</a> | <a href='{f['stream']}'>baixar</a></td></tr>"
        for f in files
    )
    body = f"""<!doctype html>
<meta charset="utf-8">
<title>Nebula Stream</title>
<style>body{{font-family:Arial,sans-serif;margin:24px}}input{{width:420px;max-width:90%}}td{{padding:6px 10px;border-bottom:1px solid #ddd}}</style>
<h1>Nebula Stream</h1>
<form><input name="q" value="{escape(query)}" placeholder="Buscar filme ou serie"><button>Buscar</button></form>
<p>{len(files)} arquivo(s)</p>
<table>{rows}</table>""".encode("utf-8")
    writer.write(http_headers(200, "text/html; charset=utf-8", body, include_body=not head_only))
    await writer.drain()


async def http_player(writer, mongo, file_id, head_only=False):
    try:
        obj_id = ObjectId(file_id)
    except Exception:
        writer.write(
            http_headers(
                400,
                "text/plain; charset=utf-8",
                b"id invalido",
                include_body=not head_only,
            )
        )
        await writer.drain()
        return
    doc = await mongo.files.find_one({"_id": obj_id, "status": "completed"}, {"name": 1})
    if not doc:
        writer.write(
            http_headers(
                404,
                "text/plain; charset=utf-8",
                b"arquivo nao encontrado",
                include_body=not head_only,
            )
        )
        await writer.drain()
        return
    name = str(doc.get("name", "media"))
    direct = os.path.splitext(name)[1].lower() in {".mp4", ".m4v", ".webm", ".ogg", ".ogv"}
    source = f"/stream?id={quote(file_id)}" if direct else f"/transcode?id={quote(file_id)}"
    body = f"""<!doctype html>
<meta charset="utf-8">
<title>{escape(name)}</title>
<style>html,body{{margin:0;background:#000;height:100%}}video{{width:100%;height:100%;object-fit:contain}}</style>
<video controls autoplay src="{source}"></video>""".encode("utf-8")
    writer.write(http_headers(200, "text/html; charset=utf-8", body, include_body=not head_only))
    await writer.drain()


async def _transcode_completed_file(writer, mongo, file_id, head_only=False):
    try:
        obj_id = ObjectId(file_id)
    except Exception:
        writer.write(
            http_headers(
                400,
                "text/plain; charset=utf-8",
                b"id invalido",
                include_body=not head_only,
            )
        )
        await writer.drain()
        return
    doc = await mongo.files.find_one(
        {"_id": obj_id, "status": "completed"},
        {"name": 1},
    )
    if not doc:
        writer.write(
            http_headers(
                404,
                "text/plain; charset=utf-8",
                b"arquivo nao encontrado",
                include_body=not head_only,
            )
        )
        await writer.drain()
        return
    if head_only:
        writer.write(http_headers(200, "video/mp4"))
        await writer.drain()
        return
    input_url = f"http://127.0.0.1:{STREAM_PORT}/stream?id={quote(file_id)}"
    extension = os.path.splitext(str(doc.get("name", "")))[1].lower()
    codec_args = (
        ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-sn"]
        if extension == ".mkv"
        else ["-c:v", "libx264", "-preset", "ultrafast", "-g", "48",
              "-keyint_min", "48", "-sc_threshold", "0", "-c:a", "aac"]
    )
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-probesize", "8M",
        "-analyzeduration", "5M", "-seekable", "0", "-i", input_url, *codec_args,
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: video/mp4\r\n"
        b"Cache-Control: no-store\r\nTransfer-Encoding: chunked\r\n"
        b"Connection: close\r\n\r\n"
    )
    await writer.drain()
    try:
        while chunk := await process.stdout.read(1024 * 1024):
            writer.write(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
            await writer.drain()
        writer.write(b"0\r\n\r\n")
        await writer.drain()
    finally:
        if process.returncode is None:
            process.terminate()
        with contextlib.suppress(Exception):
            await process.wait()


async def transcode_completed_file(writer, mongo, file_id, head_only=False):
    if head_only:
        await _transcode_completed_file(writer, mongo, file_id, True)
        return
    async with get_transcode_semaphore():
        await _transcode_completed_file(writer, mongo, file_id, head_only)


def parse_range(value, size):
    """Re-export shim. Canonical implementation lives in ``ftp.range``.

    Kept here because a few legacy callers (and the inline HTTP streaming
    endpoint) still import ``parse_range`` from this module. New code
    should import directly from ``ftp.range``.
    """
    return _parse_range(value, size)


async def stream_completed_file(writer, mongo, bots, file_id, range_header, head_only=False):
    try:
        obj_id = ObjectId(file_id)
    except Exception:
        writer.write(
            http_headers(
                400,
                "text/plain; charset=utf-8",
                b"id invalido",
                include_body=not head_only,
            )
        )
        await writer.drain()
        return
    doc = await mongo.files.find_one({"_id": obj_id, "type": "file", "status": "completed", "parts.0": {"$exists": True}})
    if not doc:
        writer.write(
            http_headers(
                404,
                "text/plain; charset=utf-8",
                b"arquivo nao encontrado",
                include_body=not head_only,
            )
        )
        await writer.drain()
        return
    size = int(doc.get("size") or 0)
    start, end, status = parse_range(range_header, size)
    if status == 416:
        writer.write(
            http_headers(
                416,
                "text/plain; charset=utf-8",
                b"",
                {"Content-Range": f"bytes */{max(size, 0)}"},
                include_body=not head_only,
            )
        )
        await writer.drain()
        return
    if size <= 0 or start > end:
        writer.write(
            http_headers(
                404,
                "text/plain; charset=utf-8",
                b"arquivo vazio",
                include_body=not head_only,
            )
        )
        await writer.drain()
        return
    filename = str(doc.get("name", "media.bin")).replace('"', "").replace("\r", "").replace("\n", "")
    content_type, _ = mimetypes.guess_type(filename)
    if not content_type:
        content_type = "application/octet-stream"
    length = end - start + 1
    headers = {
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{filename}"',
        "Content-Type": content_type,
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    reason = "Partial Content" if status == 206 else "OK"
    writer.write(
        (f"HTTP/1.1 {status} {reason}\r\n" + "\r\n".join(f"{k}: {v}" for k, v in headers.items()) + "\r\n\r\n").encode("utf-8")
    )
    await writer.drain()
    if head_only:
        return
    reader = MongoDBMemoryIO(Node(**doc), "rb", bots, mongo)
    await reader.seek(start)
    sent = 0
    async for chunk in reader.iter_by_block(1024 * 1024):
        if sent >= length:
            break
        data = chunk[: length - sent]
        writer.write(data)
        await writer.drain()
        sent += len(data)


async def handle_http_client(reader, writer, mongo, bots):
    try:
        request = await reader.readuntil(b"\r\n\r\n")
        lines = request.decode("iso-8859-1", errors="replace").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.lower()] = value.strip()
        if method not in {"GET", "HEAD"}:
            writer.write(http_headers(400, "text/plain; charset=utf-8", b"metodo invalido"))
            await writer.drain()
            return
        head_only = method == "HEAD"
        peer_host = writer.get_extra_info("peername", ("", 0))[0]
        if STREAM_TOKEN and not is_loopback_host(str(peer_host)) and not hmac.compare_digest(
            headers.get("authorization", ""),
            f"Bearer {STREAM_TOKEN}",
        ):
            writer.write(
                http_headers(
                    401,
                    "application/json; charset=utf-8",
                    b'{"error":"unauthorized"}',
                    {"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                    include_body=not head_only,
                )
            )
            await writer.drain()
            return
        url = urlsplit(target)
        params = parse_qs(url.query)
        if url.path == "/api/files":
            await http_write_json(
                writer,
                await list_completed_files(mongo, params.get("q", [""])[0], params.get("limit", ["100"])[0]),
                head_only=head_only,
            )
        elif url.path == "/play":
            await http_player(writer, mongo, params.get("id", [""])[0], head_only)
        elif url.path == "/transcode":
            await transcode_completed_file(writer, mongo, params.get("id", [""])[0], head_only)
        elif url.path == "/stream":
            await stream_completed_file(
                writer,
                mongo,
                bots,
                params.get("id", [""])[0],
                headers.get("range"),
                head_only,
            )
        else:
            await http_index(writer, mongo, params.get("q", [""])[0], head_only)
    except Exception as exc:
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError)):
            logger.debug("HTTP stream desconectado: %s", exc)
        else:
            logger.warning("HTTP stream erro: %s", exc)
        with contextlib.suppress(Exception):
            writer.write(http_headers(500, "text/plain; charset=utf-8", b"erro interno"))
            await writer.drain()
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def start_http_stream_server(mongo, bots):
    server = await asyncio.start_server(lambda r, w: handle_http_client(r, w, mongo, bots), STREAM_HOST, STREAM_PORT)
    logger.info("HTTP Stream local: http://%s:%s", STREAM_HOST, STREAM_PORT)
    return server

def get_required_config():
    if PASSIVE_PORTS_ERROR:
        raise RuntimeError(f"PASSIVE_PORTS invalido: {PASSIVE_PORTS_ERROR}")
    if not is_loopback_host(STREAM_HOST) and len(STREAM_TOKEN) < 32:
        raise RuntimeError("STREAM_TOKEN must contain at least 32 characters for non-loopback streaming")
    missing = [key for key in ("API_ID", "API_HASH", "MONGODB", "CHAT_ID") if not environ.get(key)]
    tokens = [t.strip() for t in (environ.get("BOT_TOKENS") or environ.get("BOT_TOKEN") or "").split(",") if t.strip()]
    if not tokens:
        missing.append("BOT_TOKENS")
    if missing:
        raise RuntimeError(f"Configuração ausente no .env: {', '.join(missing)}")
    try:
        api_id = int(environ["API_ID"])
    except ValueError as exc:
        raise RuntimeError("API_ID precisa ser um número inteiro.") from exc
    return api_id, environ["API_HASH"], tokens

async def garbage_collector(mongo):
    """Periodically clean up stale upload records and orphaned staging files."""
    GC_INTERVAL = int(environ.get("GC_INTERVAL", "300"))  # every 5 minutes
    STALE_AGE = int(environ.get("GC_STALE_AGE", "86400"))  # 24 hours
    while True:
        try:
            cutoff = int(time.time()) - STALE_AGE
            # Remove failed records older than STALE_AGE
            result = await mongo.files.delete_many({
                "type": "file",
                "status": "failed",
                "failed_at": {"$lt": cutoff},
            })
            if result.deleted_count:
                logger.info("GC: removidos %s registros falhos antigos.", result.deleted_count)

            # Remove staging records whose local_path no longer exists
            stale_staging = [
                doc async for doc in mongo.files.find(
                    {"type": "file", "status": "staging", "local_path": {"$exists": True}},
                    {"_id": 1, "local_path": 1, "staged_at": 1},
                )
                if (
                    doc.get("local_path")
                    and not os.path.exists(doc["local_path"])
                    and (doc.get("staged_at", 0) or 0) < cutoff
                )
            ]
            if stale_staging:
                ids = [d["_id"] for d in stale_staging]
                await mongo.files.delete_many({"_id": {"$in": ids}})
                logger.info("GC: removidos %s registros staging sem arquivo local.", len(ids))
        except Exception as exc:
            logger.debug("GC erro (não crítico): %s", exc)
        await asyncio.sleep(GC_INTERVAL)


async def folder_watcher(mongo):
    """Watch staging directories for new files and enqueue them for upload.
    Delegates to the stats_reporter coroutine which already implements this logic."""
    await stats_reporter(mongo)


async def setup_database_indexes(db) -> None:
    """Ensure required MongoDB indexes exist. Skips indexes already present."""
    files = db.files
    # The (parent, name) compound unique index already exists - do not recreate
    # Just ensure the non-unique supplementary indexes exist
    supplementary = [
        ([("type", 1), ("parent", 1)], {}),
        ([("obfuscated_id", 1)], {"sparse": True}),
        ([("parts.tg_message", 1)], {"sparse": True}),
    ]
    from pymongo.errors import OperationFailure
    for keys, kwargs in supplementary:
        try:
            await files.create_index(keys, background=True, **kwargs)
        except OperationFailure:
            pass  # index already exists with same or compatible spec
    logger.info("Índices MongoDB verificados.")


async def main():
    try:
        api_id, api_hash, tokens = get_required_config()
    except RuntimeError as exc:
        logger.critical(str(exc))
        return 1
    # Sessions ficam em diretório gravável fora do Program Files
    _sessions_dir = Path(
        environ.get("SESSIONS_DIR",
            str(Path.home() / ".nebulaftp" / "sessions")
        )
    )
    _sessions_dir.mkdir(parents=True, exist_ok=True)
    bots = [
        Client(
            f"Nebula_Bot_{idx + 1}",
            api_id=api_id,
            api_hash=api_hash,
            bot_token=token,
            no_updates=True,
            workers=1,
            max_concurrent_transmissions=1,
            workdir=str(_sessions_dir),
        )
        for idx, token in enumerate(tokens)
    ]
    for bot, token in zip(bots, tokens, strict=True):
        bot._nebula_bot_token = token
    logger.info("🤖 Iniciando %s bot(s) em paralelo...", len(bots))
    BOT_START_TIMEOUT = 15  # segundos por bot (paralelo)

    async def _start_one(idx: int, bot):
        try:
            await asyncio.wait_for(bot.start(), timeout=BOT_START_TIMEOUT)
            return bot
        except FloodWait as exc:
            logger.warning("Bot #%s FloodWait %ss; ignorando.", idx, exc.value)
        except asyncio.TimeoutError:
            logger.warning(
                "Bot #%s timeout %ss ao conectar no Telegram; ignorando.", idx, BOT_START_TIMEOUT
            )
            with contextlib.suppress(Exception):
                await bot.stop()
        except Exception as exc:
            logger.warning("Bot #%s falhou ao iniciar (%s); ignorando.", idx, exc)
        return None

    results = await asyncio.gather(
        *[_start_one(idx, bot) for idx, bot in enumerate(bots, start=1)]
    )
    active_bots = [b for b in results if b is not None]
    bots = active_bots

    if not bots:
        logger.warning(
            "⚠️ Nenhum bot Telegram disponivel. FTP iniciara em modo degradado (sem upload para Telegram)."
        )
    else:
        logger.info("Bots ativos nesta execucao: %s de %s.", len(bots), len(tokens))

    # Modo degradado: sem bots, FTP funciona mas sem upload para Telegram
    if bots:
        materializer_bot = bots[0]
        upload_bots = bots[1:] or bots

        target_chat_id = await resolve_channel(materializer_bot)
        if not target_chat_id:
            logger.warning("Canal Telegram nao resolvido; continuando em modo degradado sem upload.")
            bots = []
            upload_bots = []
            target_chat_id = None
        else:
            confirm_bots = upload_bots if upload_bots is not bots else bots[1:]
            failed_confirm = []
            for idx, bot in enumerate(confirm_bots, start=2 if len(bots) > 1 else 1):
                try:
                    await asyncio.wait_for(bot.get_chat(target_chat_id), timeout=20)
                    logger.info("Bot #%s confirmado no canal.", idx)
                except (asyncio.TimeoutError, Exception) as exc:
                    logger.warning(
                        "Bot #%s sem acesso ao canal (%s); removendo da lista de upload.",
                        idx, exc
                    )
                    failed_confirm.append(bot)
            if failed_confirm:
                bots = [b for b in bots if b not in failed_confirm]
                upload_bots = bots[1:] or bots
                if bots:
                    logger.info(
                        "Bots confirmados no canal: %s de %s.",
                        len(bots), len(bots) + len(failed_confirm)
                    )
    else:
        materializer_bot = None
        upload_bots = []
        target_chat_id = None

    loop = asyncio.get_event_loop()
    try:
        mongo_uri = environ.get("MONGODB")
        mongo_database = environ.get("MONGO_DATABASE", "ftp")
        mongo = AsyncIOMotorClient(mongo_uri, io_loop=loop, w="majority")[mongo_database]
        await setup_database_indexes(mongo)
    except Exception as e:
        logger.critical(f"❌ Erro DB: {e}")
        for bot in bots:
            await bot.stop()
        return 1

    MongoDBPathIO.db = mongo; MongoDBPathIO.tg = bots
    try:
        tls_enabled = validate_ftp_security(
            FTP_SECURITY_MODE,
            TLS_CERTFILE,
            TLS_KEYFILE,
            TLS_REQUIRED,
        )
    except ValueError as exc:
        logger.critical("FTP security configuration failed: %s", exc)
        for bot in bots:
            await bot.stop()
        return 1
    server = Server(
        MongoDBUserManager(mongo),
        MongoDBPathIO,
        passive_ports=PASSIVE_PORTS,
        security_mode=FTP_SECURITY_MODE,
    )
    http_stream_server = None
    if tls_enabled:
        try:
            tlscfg = server._build_ssl_context(TLS_CERTFILE, TLS_KEYFILE, TLS_REQUIRE_CLIENT_CERT)
            server.set_ssl_context(tlscfg)
            logger.info(
                "FTPS enabled: mode=%s require_client_cert=%s",
                FTP_SECURITY_MODE,
                TLS_REQUIRE_CLIENT_CERT,
            )
        except (FileNotFoundError, ssl.SSLError, OSError) as exc:
            logger.critical("TLS configuration failed; refusing plaintext fallback: %s", exc)
            for bot in bots:
                await bot.stop()
            return 1
    else:
        logger.warning("FTPS disabled. Plain FTP must not be exposed beyond a trusted network.")

    host = environ.get("HOST", "0.0.0.0")
    port = int(environ.get("PORT", "2121"))
    try:
        await server.start(host, port)
    except OSError as exc:
        logger.critical("❌ Não foi possível abrir %s:%s. Feche outro NebulaFTP usando essa porta. Erro: %s", host, port, exc)
        for bot in bots:
            await bot.stop()
        return 1
    logger.info(f"🚀 Nebula FTP (MonoBot) Rodando na porta {port}")
    try:
        http_stream_server = await start_http_stream_server(mongo, bots)
    except OSError as exc:
        logger.warning("HTTP Stream nao iniciado em %s:%s: %s", STREAM_HOST, STREAM_PORT, exc)

    background_tasks = [
        asyncio.create_task(garbage_collector(mongo)),
        asyncio.create_task(stats_reporter(mongo)),
        asyncio.create_task(staging_scanner(mongo, STAGING_DIRS)),
    ]
    folder_task = None
    # Initialize upload semaphore with correct bot count
    import os
    os.environ["BOT_COUNT"] = str(len(upload_bots))
    if STREAM_ONLY:
        logger.info("Modo somente streaming: feeder, fila e workers de upload desativados.")
    else:
        await cleanup_strm_duplicate_records(mongo)
        folder_task = asyncio.create_task(folder_watcher(mongo))
        background_tasks.append(folder_task)
        await restore_pending_uploads(mongo)
        upload_worker_count = get_upload_worker_count(len(upload_bots))
        logger.info(
            "Workers de upload ativos: %s (configurados=%s, transmissoes=%s, bots=%s).",
            upload_worker_count,
            MAX_WORKERS,
            UPLOAD_CONCURRENCY,
            len(upload_bots),
        )
        background_tasks.append(asyncio.create_task(queued_mongo_scanner(mongo, max_workers=upload_worker_count * PART_WORKERS_PER_FILE)))
        for i in range(upload_worker_count):
            background_tasks.append(
                asyncio.create_task(upload_worker_parallel(upload_bots, target_chat_id, mongo, i + 1))
            )

    ftp_server_task = asyncio.create_task(server.serve_forever())
    stop_event = asyncio.Event()
    control_plane = None

    async def drain_and_stop():
        server.stop_accepting()
        if folder_task:
            folder_task.cancel()
        try:
            deadline = asyncio.get_running_loop().time() + CONTROL_DRAIN_TIMEOUT
            while asyncio.get_running_loop().time() < deadline:
                pending = await mongo.files.count_documents(
                    {"type": "file", "status": {"$in": ["queued", "staging", "uploading"]}}
                )
                if not server.connections and pending == 0 and UPLOAD_QUEUE.empty():
                    break
                await asyncio.sleep(0.5)
        except Exception as exc:
            logger.warning("Control drain status check failed: %s", exc)
        finally:
            stop_event.set()

    if CONTROL_ENABLED:
        try:
            feeder = FeederSupervisor(
                Path(__file__).resolve().parent / "tools" / "feed_ftp.py",
                source_roots=FEED_ALLOWED_ROOTS,
                destination_roots=FEED_ALLOWED_DESTINATIONS or FEED_ALLOWED_ROOTS,
                state_dir=FEED_STATE_DIR,
                stop_timeout=FEED_STOP_TIMEOUT,
            )

            async def control_status():
                try:
                    mongo_ready = bool((await mongo.command("ping")).get("ok"))
                except Exception:
                    mongo_ready = False
                return {
                    "mode": "stream-only" if STREAM_ONLY else "full",
                    "mongoDb": "connected" if mongo_ready else "unreachable",
                    "telegram": f"{len(bots)} bot(s) active",
                    "ftp": f"{len(server.connections)} connection(s)",
                    "streaming": "enabled" if http_stream_server is not None else "disabled",
                    "ftp_connections": len(server.connections),
                    "bots_active": len(bots),
                    "stream_enabled": http_stream_server is not None,
                }

            control_plane = ControlPlane(
                token=environ.get("CONTROL_TOKEN", ""),
                mongo=mongo,
                upload_queue=UPLOAD_QUEUE,
                drain_callback=drain_and_stop,
                status_provider=control_status,
                feeder=feeder,
                mongo_uri=mongo_uri,
                database=mongo_database,
                source_roots=FEED_ALLOWED_ROOTS,
                output_roots=STRM_OUTPUT_ROOTS,
                disconnect_user=server.disconnect_user,
                prune_ttl=PRUNE_PREVIEW_TTL,
            )
            await control_plane.start(CONTROL_HOST, CONTROL_PORT)
            control_plane.set_ready(True)
            logger.info("Control plane v1 listening on %s:%s", CONTROL_HOST, control_plane.bound_port)
        except (OSError, ValueError) as exc:
            logger.critical("Control plane configuration failed: %s", exc)
            stop_event.set()
    try:
        loop.add_signal_handler(signal.SIGINT, stop_event.set)
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    except NotImplementedError:
        logger.info("Signal handlers unavailable on this platform; use Ctrl+C to stop.")

    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _pending = await asyncio.wait({stop_task, ftp_server_task}, return_when=asyncio.FIRST_COMPLETED)
        if ftp_server_task in done:
            if control_plane and control_plane.draining:
                await stop_task
                return 0
            exc = ftp_server_task.exception()
            if exc:
                logger.critical("❌ Servidor FTP parou com erro: %s", exc)
                return 1
    except asyncio.CancelledError:
        pass
    finally:
        stop_task.cancel()
        logger.info("⏳ Shutdown...")
        if control_plane:
            await control_plane.close()
        try:
            if not UPLOAD_QUEUE.empty():
                await asyncio.wait_for(UPLOAD_QUEUE.join(), timeout=30)
        except (TimeoutError, RuntimeError) as exc:
            logger.warning("shutdown drain incomplete: %s", exc)
        if http_stream_server:
            http_stream_server.close()
            await http_stream_server.wait_closed()
        await server.close()
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        for bot in bots:
            await bot.stop()
        logger.info("👋 Desligado.")
    return 0

if __name__ == "__main__":
    exit_code = 0
    try:
        exit_code = asyncio.run(main()) or 0
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception:
        exit_code = 1
        logger.exception("Falha inesperada ao iniciar o NebulaFTP")
    if exit_code and os.name == "nt" and environ.get("NEBULA_PAUSE_ON_EXIT", "1").lower() not in ("0", "false", "no"):
        with contextlib.suppress(EOFError, OSError):
            input("Erro ao iniciar. Pressione Enter para fechar...")
    raise SystemExit(exit_code)
