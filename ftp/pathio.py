import logging
import os
import re
import unicodedata
from asyncio import CancelledError, Lock, gather, get_event_loop
from asyncio import sleep as asleep
from collections import OrderedDict, namedtuple
from functools import wraps
from io import BytesIO
from os import environ
from pathlib import PurePosixPath
from sys import exc_info
from time import time
from uuid import uuid4

import aiofiles

try:
    from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError
except ImportError:
    # Fallbacks if pymongo not installed (e.g., test-only env): widen defensively
    class _PyMongoError(Exception):
        pass
    class _ConnectionFailure(_PyMongoError):
        pass
    class _ServerSelectionTimeoutError(_PyMongoError):
        pass
    class _DuplicateKeyError(_PyMongoError):
        pass
    ConnectionFailure = _ConnectionFailure
    DuplicateKeyError = _DuplicateKeyError
    ServerSelectionTimeoutError = _ServerSelectionTimeoutError
    PyMongoError = _PyMongoError

from .common import UPLOAD_QUEUE
from .errors import PathIOError

try:
    from .tg import File  # requires pyrogram + ideally tgcrypto
except (ImportError, RuntimeError, OSError):
    # RuntimeError catches "There is no current event loop" on Py 3.14+
    # when pyrogram's sync wrapper init runs at import time.
    File = None  # type: ignore[assignment,misc]

logger = logging.getLogger("NebulaFTP")

__all__ = (
    "AbstractPathIO", "PathIONursery", "MongoDBPathIO", "BoundedLRUCache",
    "is_uploadable_name", "movie_folder_score", "resolve_part_bot",
)

CACHE_DIR = "staging"
UPLOADABLE_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v",
    ".sub", ".ass", ".ssa", ".vtt",
}
MOVIE_TOKEN_NOISE = {
    "aac", "ac3", "amzn", "bluray", "brrip", "com", "dual", "fgt", "galaxyrg",
    "h264", "h265", "hdr", "imax", "lapumia", "rip", "web", "webdl", "webrip",
    "www", "x264", "x265", "yify", "yts",
}
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)


def is_uploadable_name(name):
    if name.endswith(".partial"):
        name = name[:-8]
    return os.path.splitext(name)[1].lower() in UPLOADABLE_EXTENSIONS


def movie_tokens(name):
    if name.endswith(".partial"):
        name = name[:-8]
    name = os.path.splitext(name)[0]
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    return {t for t in tokens if len(t) > 1 and t not in MOVIE_TOKEN_NOISE and not re.fullmatch(r"\d{3,4}p", t)}


def movie_folder_score(filename, foldername):
    file_tokens = movie_tokens(filename)
    folder_tokens = movie_tokens(foldername)
    if not file_tokens or not folder_tokens:
        return 0.0
    common = file_tokens & folder_tokens
    non_year_common = {t for t in common if not re.fullmatch(r"(19|20)\d{2}", t)}
    if not non_year_common:
        return 0.0
    score = len(common) / min(len(file_tokens), len(folder_tokens))
    file_years = {t for t in file_tokens if re.fullmatch(r"(19|20)\d{2}", t)}
    folder_years = {t for t in folder_tokens if re.fullmatch(r"(19|20)\d{2}", t)}
    if file_years and folder_years:
        score += 0.25 if file_years & folder_years else -0.50
    return score


def resolve_part_bot(part, tg):
    if isinstance(tg, (list, tuple)):
        if not tg:
            return None
        return tg[part.get("bot_index", 0) % len(tg)]
    return tg


class BoundedLRUCache(OrderedDict):
    """Tiny LRUCache so we don't pull in cachetools."""

    def __init__(self, maxsize: int = 10000):
        super().__init__()
        self.maxsize = max(1, int(maxsize))

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.maxsize:
            self.popitem(last=False)

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def get(self, key, default=None):
        if key in self:
            self.move_to_end(key)
            return super().__getitem__(key)
        return default

def universal_exception(coro):
    @wraps(coro)
    async def wrapper(*args, **kwargs):
        try:
            return await coro(*args, **kwargs)
        except (CancelledError, NotImplementedError, StopAsyncIteration):
            raise
        except Exception as exc:
            raise PathIOError(reason=exc_info()) from exc
    return wrapper

class PathIONursery:
    def __init__(self, factory):
        self.factory = factory
        self.state = None

    def __call__(self, *args, **kwargs):
        instance = self.factory(*args, state=self.state, **kwargs)
        if self.state is None:
            self.state = instance.state
        return instance

class AbstractPathIO:
    def __init__(self, connection=None):
        self.connection = connection

class Node:
    def __init__(self, type, name, ctime=None, mtime=None, size=0, parent="/", parts=None, local_path=None, **k):
        if parts is None: parts = []
        self.type = type
        self.name = name
        self.ctime = ctime or int(time())
        self.mtime = mtime or int(time())
        self.size = size
        self.parent = parent
        self.path = str(PurePosixPath(parent) / name)
        self.parts = parts
        self.local_path = local_path

class MongoDBMemoryIO:
    def __init__(self, node, mode, tg, db):
        self._node = node; self._mode = mode; self._tg = tg; self._db = db
        self.offset = 0
        self.safe_name = f"{uuid4().hex}_{node.name}"
        self.local_path = os.path.join(CACHE_DIR, self.safe_name)

    async def __aenter__(self): return self
    async def __aexit__(self, *args, **kwargs): pass
    async def seek(self, offset=0): self.offset = offset

    async def write_stream(self, stream):
        try:
            async with aiofiles.open(self.local_path, "wb") as f:
                if self.offset > 0: await f.seek(self.offset)
                async for data in stream.iter_by_block(1024*1024):
                    await f.write(data)
                await f.flush()
        except Exception as e:
            logger.error(f"❌ [WRITE] Erro disco: {e}"); raise

        final_size = os.path.getsize(self.local_path)
        parent = self._node.parent
        name = self._node.name
        cache_key = f"{parent}::{name}"
        now = int(time())
        should_upload = is_uploadable_name(name)
        if should_upload and final_size == 0:
            try:
                os.remove(self.local_path)
            except OSError as exc:
                logger.debug("empty upload cleanup skipped (%s): %s", name, exc)
            await self._db.files.delete_one({"name": name, "parent": parent})
            async with MongoDBPathIO._cache_lock:
                MongoDBPathIO._memory_cache.pop(cache_key, None)
            logger.warning("Upload vazio ignorado: %s", name)
            return

        doc_cache = {
            "type": "file", "name": name, "parent": parent, "size": final_size,
            "status": "staging" if should_upload else "completed",
            "mtime": now, "ctime": now, "parts": []
        }
        if should_upload:
            doc_cache["local_path"] = self.local_path
        else:
            try:
                os.remove(self.local_path)
            except OSError as exc:
                logger.debug("ignored file cleanup skipped (%s): %s", name, exc)

        # Atualiza Cache (Prioridade para Rclone)
        async with MongoDBPathIO._cache_lock:
            MongoDBPathIO._memory_cache[cache_key] = doc_cache

        # Atualiza DB em background (best effort) — falhas no DB não devem bloquear uploads
        try:
            await self._db.files.replace_one({"name": name, "parent": parent}, doc_cache, upsert=True)
        except (ConnectionFailure, ServerSelectionTimeoutError) as exc:
            logger.warning("DB upsert skipped for %s: %s", name, exc)
        except PyMongoError as exc:
            logger.debug("DB upsert transient error for %s: %s", name, exc)

        if name.endswith(".partial"):
             logger.debug("[WRITE] Aguardando rename para: %s", name)
        else:
             logger.debug("[WRITE] Arquivo salvo em staging: %s", name)

    async def iter_by_block(self, block_size):
        if self._node.local_path and os.path.exists(self._node.local_path):
            async with aiofiles.open(self._node.local_path, 'rb') as f:
                await f.seek(self.offset)
                while True:
                    chunk = await f.read(block_size)
                    if not chunk: break
                    yield chunk
            return

        if File is None:
            logger.error("Cannot stream from Telegram: pyrogram/tgcrypto not installed")
            return
        parts = self._node.parts
        if not parts: return
        parts.sort(key=lambda x: x["part_id"])
        current_file_pos = 0; start_read_at = self.offset

        for part in parts:
            part_size = part.get("file_size", 2 * 1024 * 1024 * 1024)
            part_end = current_file_pos + part_size
            if part_end <= start_read_at: current_file_pos += part_size; continue
            local_offset = max(0, start_read_at - current_file_pos)
            tg = resolve_part_bot(part, self._tg)
            if tg is None:
                logger.error("Cannot stream from Telegram: no bot clients configured")
                return
            file = File(part["tg_file"], tg)
            async for chunk in file.stream(offset=local_offset): yield chunk
            current_file_pos += part_size; start_read_at = current_file_pos

class MongoDBPathIO(AbstractPathIO):
    db = None; tg = None
    _cache_maxsize = int(environ.get("PATHIO_CACHE_MAXSIZE", "10000"))
    _memory_cache = BoundedLRUCache(maxsize=_cache_maxsize)
    _cache_lock = Lock()
    Stats = namedtuple("Stats", ("st_size", "st_ctime", "st_mtime", "st_nlink", "st_mode"))

    @property
    def _files(self):
        if self.db is None:
            raise PathIOError("Banco de dados MongoDB nao foi inicializado")
        return self.db.files

    def __init__(self, *args, state=None, cwd=None, **kwargs):
        super().__init__(*args, **kwargs); self.cwd = PurePosixPath("/")

    @property
    def state(self): return []

    def _absolute(self, path):
        if not path.is_absolute(): path = self.cwd / path
        return path

    def _sanitize(self, text):
        if not text: return ""
        return unicodedata.normalize('NFC', str(text))

    def _split_path(self, path_obj):
        p_str = self._sanitize(path_obj.as_posix())
        if not p_str.startswith("/"): p_str = "/" + p_str
        if p_str != "/" and p_str.endswith("/"): p_str = p_str[:-1]
        return os.path.dirname(p_str), os.path.basename(p_str)

    async def get_node(self, path):
        if str(path) in ("/", "."): return Node("dir", "", 0, 0, size=0, parent="/")
        parent, name = self._split_path(path)
        cache_key = f"{parent}::{name}"

        async with self._cache_lock:
            if cache_key in self._memory_cache:
                return Node(**self._memory_cache[cache_key])

        if self.db is None:
            return None

        node = await self._files.find_one({"name": name, "parent": parent})
        if node:
            async with self._cache_lock: self._memory_cache[cache_key] = node
            return Node(**node)
            
        # Fallback
        if parent.startswith("/") and parent != "/":
            alt = parent[1:]
            node = await self._files.find_one({"name": name, "parent": alt})
            if node:
                async with self._cache_lock: self._memory_cache[cache_key] = node
                return Node(**node)
        return None

    @universal_exception
    async def exists(self, path): return (await self.get_node(self._absolute(path))) is not None

    @universal_exception
    async def is_dir(self, path):
        node = await self.get_node(self._absolute(path))
        return not (node is None or node.type != "dir")

    @universal_exception
    async def is_file(self, path):
        node = await self.get_node(self._absolute(path))
        return not (node is None or node.type != "file")

    @universal_exception
    async def mkdir(self, path, *, exist_ok=False):
        path = self._absolute(path)
        if await self.get_node(path):
            if not exist_ok: raise FileExistsError
            return
        parent, name = self._split_path(path)
        doc = {"type": "dir", "ctime": int(time()), "mtime": int(time()), "name": name, "parent": parent, "size": 0}
        key = f"{parent}::{name}"
        try:
            await self._files.insert_one(doc)
        except DuplicateKeyError:
            # Lost the race against a concurrent insert. Treat as success
            # only when the caller opted into exist_ok=True; otherwise we
            # must surface the conflict (the previous code raised the
            # wrong exception type unconditionally).
            existing = await self.get_node(path)
            if not existing:
                raise FileExistsError from None
            if not exist_ok:
                raise FileExistsError
            async with self._cache_lock:
                self._memory_cache[key] = existing
            return
        except PyMongoError as exc:
            logger.debug("mkdir transient error (%s): %s", name, exc)
            raise
        async with self._cache_lock:
            self._memory_cache[key] = doc

    @universal_exception
    async def rmdir(self, path):
        path = self._absolute(path)
        parent, name = self._split_path(path)
        key = f"{parent}::{name}"
        async with self._cache_lock: self._memory_cache.pop(key, None)
        await self._files.delete_one({"name": name, "parent": parent})
        full = f"{parent}/{name}" if parent != "/" else f"/{name}"
        # Escape any regex metacharacters in the path so sibling trees
        # whose names happen to share a prefix (e.g. /Foo vs /FooBar)
        # are not also wiped out.
        await self._files.delete_many({"parent": {"$regex": f"^{re.escape(full)}"}})

    @universal_exception
    async def unlink(self, path):
        path = self._absolute(path)
        node = await self.get_node(path)
        if node:
            async with self._cache_lock: self._memory_cache.pop(f"{node.parent}::{node.name}", None)
            raw = await self._files.find_one({"name": node.name, "parent": node.parent})
            if raw and "local_path" in raw and os.path.exists(raw["local_path"]):
                try:
                    os.remove(raw["local_path"])
                except OSError as exc:
                    logger.debug("unlink local file skipped (%s): %s", node.name, exc)
            await self._files.delete_one({"name": node.name, "parent": node.parent})

    def list(self, path):
        path = self._absolute(path)
        search = path.as_posix()
        if not search.startswith("/"): search = "/" + search
        if search != "/" and search.endswith("/"): search = search[:-1]

        class Lister:
            iter = None
            def __aiter__(self): return self
            @universal_exception
            async def __anext__(cls):
                if cls.iter is None:
                    cls.iter = self._files.find({"parent": search, "name": {"$not": {"$regex": r"\.partial$"}}})
                try:
                    doc = await cls.iter.__anext__()
                    return path / doc["name"]
                except StopAsyncIteration: raise
        return Lister()

    @universal_exception
    async def stat(self, path):
        node = await self.get_node(self._absolute(path))
        if node is None: raise FileNotFoundError
        mode = (0x8000 | 0o666) if node.type == "file" else (0x4000 | 0o777)
        return MongoDBPathIO.Stats(node.size, node.ctime, node.mtime, 1, mode)

    @universal_exception
    async def open(self, path, mode="rb", *args, **kwargs):
        path = self._absolute(path)
        parent, name = self._split_path(path)
        if mode == "wb":
            doc = {"type": "file", "ctime": int(time()), "mtime": int(time()), "name": name, "parent": parent, "size": 0, "parts": []}
            async with self._cache_lock: self._memory_cache[f"{parent}::{name}"] = doc
            await self._files.replace_one({"name": name, "parent": parent}, doc, upsert=True)
        
        node = await self.get_node(path)
        if not node and mode == "rb": raise FileNotFoundError
        return MongoDBMemoryIO(node, mode, self.tg, self.db)

    @universal_exception
    async def rename(self, source, destination):
        source = self._absolute(source); destination = self._absolute(destination)
        # logger.info(f"🔄 [RENAME] {source} → {destination}")

        src_p, src_n = self._split_path(source)
        dst_p, dst_n = self._split_path(destination)
        
        # 1. BUSCA ORIGEM NO CACHE PRIMEIRO
        old_key = f"{src_p}::{src_n}"
        new_key = f"{dst_p}::{dst_n}"
        src_doc = None

        async with self._cache_lock:
            src_doc = self._memory_cache.get(old_key)
        
        if not src_doc:
            src_doc = await self._files.find_one({"name": src_n, "parent": src_p})
        
        if not src_doc:
            logger.warning(f"⚠️ [RENAME] Origem não encontrada: {source}")
            return 

        existing_dst = await self._files.find_one({"name": dst_n, "parent": dst_p})
        if existing_dst and existing_dst.get("_id") != src_doc.get("_id"):
            if existing_dst.get("type") == "dir" and src_doc.get("type") == "dir":
                async with self._cache_lock:
                    self._memory_cache.pop(old_key, None)
                await self._files.delete_one({"_id": src_doc["_id"]})
                return
            if existing_dst.get("local_path") and os.path.exists(existing_dst["local_path"]):
                try:
                    os.remove(existing_dst["local_path"])
                except OSError as exc:
                    logger.debug("overwrite local cleanup skipped (%s): %s", dst_n, exc)
            await self._files.delete_one({"_id": existing_dst["_id"]})

        # 2. Atualiza Cache Atomicamente
        async with self._cache_lock:
            self._memory_cache.pop(old_key, None)
            self._memory_cache.pop(new_key, None)
            
            src_doc["name"] = dst_n
            src_doc["parent"] = dst_p
            src_doc["mtime"] = int(time())
            
            self._memory_cache[new_key] = src_doc

        # 3. Atualiza DB
        await self._files.update_one(
            {"_id": src_doc["_id"]}, 
            {"$set": {"name": dst_n, "parent": dst_p, "mtime": int(time())}}
        )

        # 4. Dispara Upload (Partial -> Final)
        if src_n.endswith(".partial") and not dst_n.endswith(".partial"):
            local_p = src_doc.get("local_path")
            if not is_uploadable_name(dst_n):
                await self._files.update_one(
                    {"_id": src_doc["_id"]},
                    {"$set": {"status": "completed"}, "$unset": {"local_path": 1}}
                )
                if local_p and os.path.exists(local_p):
                    try:
                        os.remove(local_p)
                    except OSError as exc:
                        logger.debug("ignored renamed file cleanup skipped (%s): %s", dst_n, exc)
                logger.debug("[RENAME] Ignorado sem upload: %s", dst_n)
                return
            
            if local_p and os.path.exists(local_p):
                await UPLOAD_QUEUE.put({
                    "path": local_p,
                    "filename": dst_n,
                    "parent": dst_p,
                    "size": src_doc.get("size", 0)
                })
                logger.info(f"📤 [RENAME] Enfileirado: {dst_n}")
            else:
                logger.warning(f"⚠️ [RENAME] Arquivo físico não encontrado: {dst_n}")
