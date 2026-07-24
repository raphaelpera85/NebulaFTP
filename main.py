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
import io
import json
import logging
import mimetypes
import os
import re
import signal
import ssl
import sys
import time
import uuid
from collections import deque
from html import escape
from logging.handlers import RotatingFileHandler
from os import environ
from os.path import exists
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import aiofiles
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pyrogram import Client
from pyrogram import utils as pyrogram_utils
from pyrogram.errors import FloodWait, RPCError

from ftp import MongoDBPathIO, MongoDBUserManager, Server
from ftp.common import UPLOAD_QUEUE
from ftp.pathio import MongoDBMemoryIO, Node, is_uploadable_name, movie_folder_score

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

pyrogram_utils.MIN_CHANNEL_ID = min(pyrogram_utils.MIN_CHANNEL_ID, -1009999999999)  # type: ignore

if exists(".env"):
    from dotenv import load_dotenv
    load_dotenv()

# --- CARREGAMENTO DE CONFIGURAÇÕES DO .ENV ---
LOG_LEVEL = environ.get("LOG_LEVEL", "INFO")
LOG_COMPACT_LINES = int(environ.get("LOG_COMPACT_LINES", 1000))
LOG_CONTEXT_FILE = environ.get("LOG_CONTEXT_FILE", "nebula_context.md")
CHUNK_SIZE_MB = int(environ.get("CHUNK_SIZE_MB", 64))
CHUNK_SIZE = CHUNK_SIZE_MB * 1024 * 1024 
MAX_RETRIES = int(environ.get("MAX_RETRIES", 5))
MAX_STAGING_AGE = int(environ.get("MAX_STAGING_AGE", 3600))
MAX_WORKERS = int(environ.get("MAX_WORKERS", 4))
PART_WORKERS_PER_FILE = max(1, int(environ.get("PART_WORKERS_PER_FILE", 2)))
UPLOAD_STATUS_MESSAGES = environ.get("UPLOAD_STATUS_MESSAGES", "false").lower() in ("1", "true", "yes")
STREAM_HOST = environ.get("STREAM_HOST", "127.0.0.1")
STREAM_PORT = int(environ.get("STREAM_PORT", 2122))

# Portas Passivas
PASSIVE_PORTS = None
pp_str = environ.get("PASSIVE_PORTS")
if pp_str and "-" in pp_str:
    try:
        start_p, end_p = map(int, pp_str.split("-"))
        if start_p > end_p or start_p < 1 or end_p > 65535:
            raise ValueError(f"invalid passive port range: {pp_str}")
        PASSIVE_PORTS = range(start_p, end_p + 1)
    except (ValueError, TypeError) as exc:
        logger.warning("PASSIVE_PORTS parse failed (%s); using ephemeral range", exc)
        PASSIVE_PORTS = range(60000, 60100)

# TLS / FTPS (RFC 4217)
TLS_CERTFILE = environ.get("TLS_CERTFILE")
TLS_KEYFILE = environ.get("TLS_KEYFILE")
TLS_REQUIRE_CLIENT_CERT = environ.get("TLS_REQUIRE_CLIENT_CERT", "false").lower() in ("1", "true", "yes")

# --- CONTROLE DE LOCKS (PROTEÇÃO) ---
# Conjunto para armazenar caminhos de arquivos que estão sendo enviados agora.
# O Garbage Collector NÃO pode tocar nestes arquivos.
ACTIVE_UPLOADS = set()

def safe_remove_staging_file(path, force_delete=False):
    try:
        root = os.path.abspath("staging")
        target = os.path.abspath(path)
        try:
            in_staging = os.path.commonpath([root, target]) == root
        except ValueError:
            in_staging = False
        
        # Permite deleção global fora do staging se configurado no .env
        global_delete = os.environ.get("DELETE_SOURCE_AFTER_UPLOAD", "false").lower() in ("1", "true", "yes")

        if not in_staging and not force_delete and not global_delete:
            logger.debug("source cleanup skipped outside staging: %s", path)
            return
        os.remove(target)
        logger.info("Removido arquivo: %s", target)
    except OSError as exc:
        logger.debug("staging cleanup skipped: %s", exc)
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
        with open(self.context_file, "a", encoding="utf-8", errors="replace") as fh:
            fh.write("\n".join(summary))
        if self.stream:
            self.stream.flush()
            self.stream.seek(0)
            self.stream.truncate()
        self.line_count = 0
        self.recent_lines.clear()


# --- LOGGING ---
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_handler = CompactingFileHandler(
    'nebula.log',
    compact_lines=LOG_COMPACT_LINES,
    context_file=LOG_CONTEXT_FILE,
    maxBytes=5*1024*1024,
    backupCount=2,
    encoding="utf-8",
    errors="replace",
)
log_handler.setFormatter(log_formatter)
console_handler = SafeStreamHandler()
console_handler.setFormatter(log_formatter)
logger = logging.getLogger("NebulaFTP")
logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
logger.addHandler(log_handler)
logger.addHandler(console_handler)

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
    user_root = f"/{parts[0]}" if parts and parts[0] else "/raphael"
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
        await asyncio.sleep(300)
        Metrics.report()
        await log_queue_state(mongo, "intervalo")

async def setup_database_indexes(mongo):
    logger.info("🔧 Verificando índices do Banco de Dados...")
    try:
        await mongo.files.create_index([("parent", 1), ("name", 1)], unique=True)
        await mongo.files.create_index("parent")
        await mongo.files.create_index("uploadId", sparse=True)
        await mongo.files.create_index("uploaded_at")
        await mongo.files.create_index("status") 
        logger.info("✅ Índices verificados.")
    except Exception as e: logger.warning(f"⚠️ Aviso índices: {e}")

GC_INTERVAL_SECONDS = int(environ.get("GC_INTERVAL_SECONDS", 60))

async def garbage_collector(mongo):
    logger.info(f"🧹 Garbage Collector Iniciado (Max Age: {MAX_STAGING_AGE}s, Intervalo: {GC_INTERVAL_SECONDS}s)")
    staging_dir = "staging"
    while True:
        try:
            now = time.time()
            if os.path.exists(staging_dir):
                for root, dirs, files in os.walk(staging_dir):
                    for f in files:
                        if f.endswith(".partial"): continue
                        fp = os.path.join(root, f)

                        if fp in ACTIVE_UPLOADS:
                            continue

                        tracked = await mongo.files.find_one({
                            "type": "file",
                            "local_path": fp,
                            "status": {"$in": ["queued", "uploading", "staging"]},
                        })
                        if tracked:
                            continue

                        try:
                            mtime = os.path.getmtime(fp)
                        except OSError as exc:
                            logger.debug("gc stat error on %s: %s", fp, exc)
                            continue

                        if now - mtime > MAX_STAGING_AGE:
                            try:
                                os.remove(fp)
                                logger.warning(f"🧹 GC: Lixo removido: {f}")
                            except OSError as exc:
                                logger.error(f"❌ GC Erro {f}: {exc}")
        except Exception as e:
            logger.error(f"❌ GC Falha Geral: {e}")
        await asyncio.sleep(GC_INTERVAL_SECONDS)

async def folder_watcher(mongo):
    """
    Vigia a pasta 'staging' RECURSIVAMENTE.
    Mapeia arquivos para a PASTA DO UTILIZADOR.
    """
    logger.info("👀 Folder Watcher Iniciado")
    staging_dir = "staging"
    if not os.path.exists(staging_dir): os.makedirs(staging_dir)

    target_root = "/" 
    try:
        user = await mongo.users.find_one({})
        if user:
            target_root = f"/{user['login']}"
            logger.info(f"🎯 Modo MonoBot: Arquivos de staging irão para: {target_root}")
        else:
            logger.warning("⚠️ Nenhum utilizador encontrado no DB. Arquivos irão para a Raiz '/'.")
    except Exception as e:
        logger.error(f"❌ Erro ao buscar utilizador: {e}")

    while True:
        try:
            for root, dirs, files in os.walk(staging_dir):
                for f in files:
                    if f.endswith(".partial"): continue
                    if not is_uploadable_name(f): continue
                    fp = os.path.join(root, f)
                    
                    if not os.path.isfile(fp): continue
                    
                    # Ignora se já estiver sendo enviado (evita duplicar na fila)
                    if fp in ACTIVE_UPLOADS: continue

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
                    if size_t1 == 0: continue

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
                        parent_path = target_root
                    else:
                        normalized_rel = rel_dir.replace(os.sep, "/")
                        if target_root == "/": parent_path = f"/{normalized_rel}"
                        else: parent_path = f"{target_root}/{normalized_rel}"

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
                            if os.path.getsize(fp) != size_t1: continue
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
                                if current_parent == "/": current_parent = "/" + part
                                else: current_parent = f"{current_parent}/{part}"

                        file_doc = {
                            "type": "file", "name": display_name, "parent": parent_path, "size": size_t1,
                            "status": "queued", "local_path": fp,
                            "mtime": int(time.time()), "ctime": int(time.time()), "parts": []
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
            logger.error(f"❌ Erro Watcher: {e}")
        
        await asyncio.sleep(5)

async def restore_pending_uploads(mongo):
    count = 0
    query = {"type": "file", "status": {"$in": ["queued", "uploading", "staging"]}, "local_path": {"$exists": True}}
    async for doc in mongo.files.find(query):
        local_path = doc.get("local_path")
        if not local_path or not os.path.exists(local_path):
            await mongo.files.update_one(
                {"_id": doc["_id"]},
                {"$set": {"status": "failed", "failed_at": int(time.time()), "failed_reason": "local_path_missing"}},
            )
            continue
        parent = await resolve_media_parent(mongo, doc["parent"], doc["name"])
        await mongo.files.update_one({"_id": doc["_id"]}, {"$set": {"status": "queued", "parent": parent}})
        await UPLOAD_QUEUE.put({
            "path": local_path,
            "filename": doc["name"],
            "parent": parent,
            "size": doc.get("size", os.path.getsize(local_path)),
        })
        count += 1
    logger.info("Fila restaurada: %s arquivo(s) pendente(s)", count)
    await log_queue_state(mongo, "restauracao")

async def queued_mongo_scanner(mongo):
    while True:
        try:
            for _ in range(MAX_WORKERS):
                doc = await mongo.files.find_one_and_update(
                    {"type": "file", "status": "queued", "local_path": {"$exists": True}},
                    {"$set": {"status": "staging", "staged_at": int(time.time())}},
                )
                if not doc:
                    break
                local_path = doc.get("local_path")
                if not local_path or not os.path.exists(local_path):
                    await mongo.files.update_one(
                        {"_id": doc["_id"]},
                        {"$set": {"status": "failed", "failed_at": int(time.time()), "failed_reason": "local_path_missing"}},
                    )
                    continue
                parent = await resolve_media_parent(mongo, doc["parent"], doc["name"])
                await mongo.files.update_one({"_id": doc["_id"]}, {"$set": {"parent": parent}})
                await UPLOAD_QUEUE.put({
                    "path": local_path,
                    "filename": doc["name"],
                    "parent": parent,
                    "size": doc.get("size", os.path.getsize(local_path)),
                })
        except Exception as exc:
            logger.warning("scanner Mongo aguardando: %s", exc)
        await asyncio.sleep(1)

async def upload_part_with_retries(worker_id, bots, target_chat_id, local_path, file_uuid, part_num, chunk_data=None):
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

    # Start rotation from part_num % len(bots) so different parts
    # in the same batch prefer different bots from the start.
    current_bot_idx = part_num % len(bots)
    sent_msg = None
    for attempt in range(1, MAX_RETRIES + 1):
        part_bot = bots[current_bot_idx % len(bots)]
        try:
            sent_msg = await part_bot.send_document(
                chat_id=target_chat_id,
                document=io.BytesIO(chunk_data),
                file_name=chunk_name,
                force_document=True,
                caption="",
            )
            break
        except FloodWait as e:
            w = e.value + 2
            logger.warning(
                f"[W{worker_id}] FloodWait bot#{current_bot_idx + 1}: {w}s, "
                f"rotating to next bot"
            )
            await asyncio.sleep(w)
            # Rotate to next bot for retry
            current_bot_idx += 1
        except RPCError as e:
            w = 2 ** attempt
            logger.error(f"[W{worker_id}] Erro TG ({attempt}) bot#{current_bot_idx + 1}: {e}")
            await asyncio.sleep(w)
        except Exception as e:
            logger.error(f"[W{worker_id}] Erro: {e}")
            await asyncio.sleep(5)
    if not sent_msg:
        raise Exception(f"Falha upload parte {part_num}")
    return {
        "part_id": part_num,
        "tg_file": sent_msg.document.file_id,
        "tg_message": sent_msg.id,
        "file_size": len(chunk_data),
        "chunk_name": chunk_name,
        "bot_index": current_bot_idx % len(bots),
    }


async def _readahead_producer(local_path, total_parts, queue, worker_id):
    """Pre-read file chunks into an async queue ahead of upload workers.

    This overlaps disk I/O with network I/O: while workers upload chunk N,
    this producer is already reading chunk N+1 into memory.  The queue
    bounds memory usage to ``queue.maxsize * CHUNK_SIZE`` bytes.
    """
    async with aiofiles.open(local_path, "rb") as f:
        for part_num in range(total_parts):
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
        try: task = await asyncio.wait_for(UPLOAD_QUEUE.get(), timeout=2.0)
        except asyncio.TimeoutError: continue
            
        local_path = task["path"]; filename = task["filename"]; parent = task["parent"]
        
        # --- LOCK: Bloqueia o arquivo para o GC não apagar ---
        ACTIVE_UPLOADS.add(local_path)
        # -----------------------------------------------------

        try:
            if filename.endswith(".partial"): continue

            if not os.path.exists(local_path): continue
            
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
            parts_metadata = []
            upload_failed = False
            uploaded_bytes = 0
            
            try:
                async with aiofiles.open(local_path, "rb") as f:
                    part_num = 0
                    while True:
                        chunk_data = await f.read(CHUNK_SIZE)
                        if not chunk_data: break
                        
                        chunk_name = f"{file_uuid}.part_{part_num:03d}"
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
                                    caption=""
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
                        
                        if not sent_msg: raise Exception(f"Falha upload parte {part_num}")

                        parts_metadata.append({
                            "part_id": part_num, "tg_file": sent_msg.document.file_id,
                            "tg_message": sent_msg.id, "file_size": len(chunk_data),
                            "chunk_name": chunk_name, "bot_index": bot_index
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
                await mongo.files.update_one(
                    {"_id": file_doc["_id"]},
                    {"$set": {"size": real_size, "uploaded_at": int(time.time()), "parts": parts_metadata, "obfuscated_id": file_uuid, "status": "completed"}, "$unset": {"uploadId": 1, "local_path": 1}}
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
            
        except Exception as e: logger.error(f"❌ [W{worker_id}] Crítico: {e}")
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
        except asyncio.TimeoutError:
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
            total_parts = max(1, (real_size + CHUNK_SIZE - 1) // CHUNK_SIZE)
            logger.info(
                f"[W{worker_id}] Iniciando upload: {filename} "
                f"tamanho={real_size/1024/1024:.2f} MB partes={total_parts} "
                f"paralelo={PART_WORKERS_PER_FILE} bots={len(bots)}"
            )

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

            file_uuid = str(uuid.uuid4())
            uploaded_bytes = 0
            parts_metadata = []

            # --- Read-ahead: start producer that fills a bounded queue ---
            readahead_queue: asyncio.Queue = asyncio.Queue(
                maxsize=PART_WORKERS_PER_FILE
            )
            producer_task = asyncio.create_task(
                _readahead_producer(local_path, total_parts, readahead_queue, worker_id)
            )

            for start_part in range(0, total_parts, PART_WORKERS_PER_FILE):
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

                # Upload each part in the batch — bot rotation happens
                # inside upload_part_with_retries via the bots[] list.
                async def _upload_one(pn: int) -> dict:
                    chunk_data = chunk_map.get(pn)
                    return await upload_part_with_retries(
                        worker_id, bots, target_chat_id, local_path, file_uuid, pn, chunk_data
                    )

                results = await asyncio.gather(
                    *[_upload_one(pn) for pn in range(start_part, batch_end)]
                )
                parts_metadata.extend(results)
                parts_metadata.sort(key=lambda item: item["part_id"])
                uploaded_bytes += sum(item["file_size"] for item in results)
                percent = uploaded_bytes / real_size * 100
                await mongo.files.update_one(
                    {"_id": file_doc["_id"]},
                    {"$set": {"uploaded_bytes": uploaded_bytes, "parts": parts_metadata}}
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

            await mongo.files.update_one(
                {"_id": file_doc["_id"]},
                {
                    "$set": {
                        "size": real_size,
                        "uploaded_at": int(time.time()),
                        "parts": parts_metadata,
                        "obfuscated_id": file_uuid,
                        "status": "completed",
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
        try: await bot.send_message(chat.id, "🔄 Nebula FTP MonoBot Conectado", disable_notification=True)
        except (RPCError, ConnectionError) as exc:
            logger.debug("startup ping skipped: %s", exc)
        return chat.id
    except Exception as e:
        logger.critical(f"❌ Canal inválido '{target_chat}': {e}"); return None

def http_headers(status, content_type, body=b"", extra=None):
    reason = {200: "OK", 206: "Partial Content", 400: "Bad Request", 404: "Not Found", 500: "Server Error"}.get(status, "OK")
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "Connection": "close",
        "Access-Control-Allow-Origin": "*",
    }
    if extra:
        headers.update(extra)
    head = [f"HTTP/1.1 {status} {reason}", *[f"{k}: {v}" for k, v in headers.items()], "", ""]
    return "\r\n".join(head).encode("utf-8") + body


async def http_write_json(writer, data, status=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    writer.write(http_headers(status, "application/json; charset=utf-8", body))
    await writer.drain()


async def list_completed_files(mongo, query, limit):
    limit = min(max(int(limit or 100), 1), 500)
    criteria: dict[str, Any] = {"type": "file", "status": "completed", "parts.0": {"$exists": True}}
    if query:
        rx = re.compile(re.escape(query), re.IGNORECASE)
        criteria["$or"] = [{"name": rx}, {"parent": rx}]
    cursor = mongo.files.find(criteria, {"name": 1, "parent": 1, "size": 1, "uploaded_at": 1}).sort("uploaded_at", -1).limit(limit)
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
        })
    return files


async def http_index(writer, mongo, query):
    files = await list_completed_files(mongo, query, 100)
    rows = "\n".join(
        f"<tr><td>{escape(f['path'])}</td><td>{f['size']}</td>"
        f"<td><a href='{f['stream']}'>stream/download</a></td></tr>"
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
    writer.write(http_headers(200, "text/html; charset=utf-8", body))
    await writer.drain()


def parse_range(value, size):
    """Re-export shim. Canonical implementation lives in ``ftp.range``.

    Kept here because a few legacy callers (and the inline HTTP streaming
    endpoint) still import ``parse_range`` from this module. New code
    should import directly from ``ftp.range``.
    """
    from ftp.range import parse_range as _impl
    return _impl(value, size)


async def stream_completed_file(writer, mongo, bots, file_id, range_header):
    try:
        obj_id = ObjectId(file_id)
    except Exception:
        writer.write(http_headers(400, "text/plain; charset=utf-8", b"id invalido"))
        await writer.drain()
        return
    doc = await mongo.files.find_one({"_id": obj_id, "type": "file", "status": "completed", "parts.0": {"$exists": True}})
    if not doc:
        writer.write(http_headers(404, "text/plain; charset=utf-8", b"arquivo nao encontrado"))
        await writer.drain()
        return
    size = int(doc.get("size") or 0)
    start, end, status = parse_range(range_header, size)
    if size <= 0 or start > end:
        writer.write(http_headers(404, "text/plain; charset=utf-8", b"arquivo vazio"))
        await writer.drain()
        return
    filename = str(doc.get("name", "media.bin")).replace('"', "")
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
        url = urlsplit(target)
        params = parse_qs(url.query)
        if url.path == "/api/files":
            await http_write_json(writer, await list_completed_files(mongo, params.get("q", [""])[0], params.get("limit", ["100"])[0]))
        elif url.path == "/stream":
            await stream_completed_file(writer, mongo, bots, params.get("id", [""])[0], headers.get("range"))
        else:
            await http_index(writer, mongo, params.get("q", [""])[0])
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

async def main():
    try:
        api_id, api_hash, tokens = get_required_config()
    except RuntimeError as exc:
        logger.critical(str(exc))
        return 1

    bots = [
        Client(
            f"Nebula_Bot_{idx + 1}",
            api_id=api_id,
            api_hash=api_hash,
            bot_token=token,
            in_memory=True,
        )
        for idx, token in enumerate(tokens)
    ]
    logger.info("🤖 Iniciando %s bot(s)...", len(bots))
    try:
        for bot in bots:
            await bot.start()
    except Exception as e:
        logger.critical(f"❌ Falha ao iniciar bot: {e}")
        for started in bots:
            with contextlib.suppress(Exception):
                await started.stop()
        return 1

    target_chat_id = await resolve_channel(bots[0])
    if not target_chat_id:
        for bot in bots:
            await bot.stop()
        return 1
    for idx, bot in enumerate(bots[1:], start=2):
        try:
            await bot.get_chat(target_chat_id)
            logger.info("Bot #%s confirmado no canal.", idx)
        except Exception as exc:
            logger.critical("Bot #%s sem acesso ao canal: %s", idx, exc)
            for started in bots:
                with contextlib.suppress(Exception):
                    await started.stop()
            return 1

    loop = asyncio.get_event_loop()
    try:
        mongo = AsyncIOMotorClient(environ.get("MONGODB"), io_loop=loop, w="majority").ftp
        await setup_database_indexes(mongo)
    except Exception as e:
        logger.critical(f"❌ Erro DB: {e}")
        for bot in bots:
            await bot.stop()
        return 1
    
    MongoDBPathIO.db = mongo; MongoDBPathIO.tg = bots
    server = Server(MongoDBUserManager(mongo), MongoDBPathIO)
    http_stream_server = None
    if TLS_CERTFILE and TLS_KEYFILE:
        try:
            tlscfg = server._build_ssl_context(TLS_CERTFILE, TLS_KEYFILE, TLS_REQUIRE_CLIENT_CERT)
            server.set_ssl_context(tlscfg)
            logger.info(f"🔐 FTPS enabled: cert={TLS_CERTFILE} require_client_cert={TLS_REQUIRE_CLIENT_CERT}")
        except (FileNotFoundError, ssl.SSLError, OSError) as exc:
            logger.critical(f"❌ TLS configuration failed ({exc}); falling back to plaintext FTP. Set TLS_CERTFILE/TLS_KEYFILE properly or leave both blank to disable FTPS.")
            server.set_ssl_context(None)
    else:
        logger.warning("⚠️  FTPS disabled (TLS_CERTFILE/TLS_KEYFILE not set). Credentials and payloads will travel in clear — do not expose this beyond localhost.")

    host = environ.get("HOST", "0.0.0.0")
    port = int(environ.get("PORT", 2121))
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

    asyncio.create_task(garbage_collector(mongo))
    asyncio.create_task(stats_reporter(mongo))
    asyncio.create_task(folder_watcher(mongo))
    asyncio.create_task(queued_mongo_scanner(mongo))
    await restore_pending_uploads(mongo)

    for i in range(MAX_WORKERS):
        asyncio.create_task(upload_worker_parallel(bots, target_chat_id, mongo, i + 1))

    ftp_server_task = asyncio.create_task(server.serve_forever())
    
    stop_event = asyncio.Event()
    try:
        loop.add_signal_handler(signal.SIGINT, stop_event.set)
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    except NotImplementedError:
        logger.info("Signal handlers unavailable on this platform; use Ctrl+C to stop.")
    
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _pending = await asyncio.wait({stop_task, ftp_server_task}, return_when=asyncio.FIRST_COMPLETED)
        if ftp_server_task in done:
            exc = ftp_server_task.exception()
            if exc:
                logger.critical("❌ Servidor FTP parou com erro: %s", exc)
                return 1
    except asyncio.CancelledError: pass
    finally:
        stop_task.cancel()
        logger.info("⏳ Shutdown...")
        try:
            if not UPLOAD_QUEUE.empty(): await asyncio.wait_for(UPLOAD_QUEUE.join(), timeout=30)
        except (asyncio.TimeoutError, RuntimeError) as exc:
            logger.warning("shutdown drain incomplete: %s", exc)
        if http_stream_server:
            http_stream_server.close()
            await http_stream_server.wait_closed()
        await server.close()
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
