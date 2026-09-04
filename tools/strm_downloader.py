#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STRM Downloader & Feeder para NebulaFTP.

Programa autônomo para escanear bibliotecas de arquivos .strm, validar
duplicatas/mídias já enviadas no Nebula, realizar download multipart diretamente
para as pastas de stage configuradas e registrar as mídias na fila do MongoDB
(db.files) com status='queued' e delete_source=True, respeitando a ordem
de prioridade por categoria e ano de lançamento (2026 -> 2025 -> ...).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import mimetypes
import os
import queue
import re
import shutil
import socket
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# Carregar variáveis de ambiente
load_dotenv()

# --- CONSTANTES E PADRÕES ---
UPLOADABLE_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v",
    ".ts", ".webm", ".sub", ".ass", ".ssa", ".vtt",
}
MONITORED_EXTENSIONS = UPLOADABLE_EXTENSIONS | {".strm"}
ACTIVE_STATUSES = ("staging", "queued", "uploading")
EPISODE_RE = re.compile(r"(?i)(?P<prefix>.*?)(?:[.\s_-]+)?s(?P<season>\d{1,2})[.\s_-]*e(?P<episode>\d{1,3})")
INCOMPLETE_RE = re.compile(
    r"(?i)(?P<download>.+\.download)(?:\.part\d+)?$|.+\.(?:partial|crdownload|aria2|tmp|part)$"
)


def is_incomplete_filename(name: str) -> bool:
    """Verifica se o arquivo é um arquivo temporário ou incompleto."""
    name_lower = name.lower()
    return bool(
        INCOMPLETE_RE.search(name)
        or name_lower.endswith(".part")
        or ".part." in name_lower
        or name_lower.endswith(".tmp")
        or name_lower.endswith(".crdownload")
    )

# Prioridade de categorias padrão do Nebula
CATEGORY_PRIORITY = ["filmes", "porno", "series"]

logger = logging.getLogger("STRMDownloader")


@dataclass
class DownloaderStats:
    scanned: int = 0
    skipped_completed: int = 0
    skipped_active: int = 0
    downloaded: int = 0
    reused: int = 0
    queued: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    errors: list[str] = field(default_factory=list)


def setup_logger(verbose: bool = False) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        with contextlib.suppress(Exception):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter("[%(asctime)s][%(levelname)s][STRM] %(message)s", datefmt="%H:%M:%S")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.addHandler(handler)


# =====================================================================
# EXTRAÇÃO DE METADADOS E IDENTIFICAÇÃO DE MÍDIA
# =====================================================================

def extract_media_year(name: str, parent_name: str = "") -> int:
    """Extrai o ano da mídia (1900-2099) a partir do nome do arquivo e/ou pasta."""
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


def normalize_media_title(value: str) -> str:
    """Normaliza título removendo acentos, pontuações e caixa alta para comparação precisa."""
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", ascii_value.casefold()))


def movie_identity(value: str) -> tuple[str, str] | None:
    """Retorna a tupla (titulo_normalizado, ano) para filmes."""
    years = list(re.finditer(r"(?<!\d)((?:19|20)\d{2})(?!\d)", value))
    if not years:
        return None
    year = years[-1]
    title = normalize_media_title(value[:year.start()])
    return (title, year.group(1)) if title else None


def episode_identity(series_name: str, filename: str) -> tuple[str, int, int] | None:
    """Retorna a tupla (nome_serie_normalizado, temporada, episodio)."""
    match = EPISODE_RE.search(Path(filename).stem)
    if not match:
        return None
    prefix = match.group("prefix").strip(" ._-")
    effective_name = series_name if series_name and "season" not in series_name.lower() else prefix
    if not effective_name:
        effective_name = prefix
    title = normalize_media_title(effective_name)
    if not title:
        return None
    return title, int(match.group("season")), int(match.group("episode"))


def get_category_from_path(src: Path, source_root: Path) -> str:
    """Determina a categoria da mídia (filmes/porno/series/outros)."""
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
    return "other"


def read_strm_url(src: Path) -> str:
    """Lê a URL de stream contida dentro do arquivo .strm."""
    text = src.read_text(encoding="utf-8-sig", errors="replace")
    for line in text.splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            return value
    raise ValueError(f"Arquivo .strm vazio ou inválido: {src}")


def guess_media_extension(url: str, content_type: str | None = None) -> str:
    """Infere a extensão de mídia (.mkv, .mp4, etc.) a partir da URL."""
    path_suffix = Path(urlsplit(url).path).suffix.lower()
    if path_suffix in UPLOADABLE_EXTENSIONS:
        return path_suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip().lower())
        if guessed in UPLOADABLE_EXTENSIONS:
            return ".jpg" if guessed == ".jpe" else guessed
    return ".mp4"


def remote_content_size(url: str, timeout: int = 15) -> int | None:
    """Obtém o tamanho remoto do arquivo via Range request 0-0 ou Content-Length."""
    try:
        request = Request(url, headers={"Range": "bytes=0-0", "User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=timeout) as response:
            match = re.match(r"bytes\s+\d+-\d+/(\d+)", response.headers.get("Content-Range", ""))
            if match:
                return int(match.group(1))
            length = response.headers.get("Content-Length")
            return int(length) if length and response.status != 206 else None
    except Exception:
        return None


# =====================================================================
# DESTINOS E ESTRUTURA NO NEBULA / MONGODB
# =====================================================================

def series_path_from_filename(dest_root: Path, src: Path) -> Path | None:
    """Gera o caminho estruturado para séries a partir do nome do arquivo."""
    match = EPISODE_RE.search(src.stem)
    if not match:
        return None
    series_name = match.group("prefix").strip(" ._-")
    if not series_name:
        return None
    series_name = re.sub(r"[._]+", " ", series_name)
    series_name = re.sub(r"\s+", " ", series_name).strip()
    season = int(match.group("season"))
    return dest_root / "Series" / series_name / f"Season {season:02d}" / src.name


def destination_for(source_root: Path, dest_root: Path, src: Path) -> Path:
    """Calcula o caminho final esperado na estrutura do Nebula."""
    try:
        rel = src.relative_to(source_root)
        parts = list(rel.parts)
    except ValueError:
        parts = [src.name]

    while len(parts) >= 2 and parts[0].lower() in ("filmes", "series", "porno") and parts[1].lower() == parts[0].lower():
        parts.pop(0)

    series_by_name = series_path_from_filename(dest_root, src)
    if len(parts) >= 2 and parts[0].lower() == "filmes":
        if series_by_name:
            return series_by_name
        if len(parts) == 2:
            return dest_root / "Filmes" / src.stem / src.name
        return dest_root / "Filmes" / Path(*parts[1:])
    if len(parts) >= 1 and parts[0].lower() == "porno":
        return dest_root / "Porno" / src.name
    if len(parts) >= 2 and parts[0].lower() == "series":
        return dest_root / "Series" / Path(*parts[1:])
    if series_by_name:
        return series_by_name
    return dest_root / Path(*parts)


def mongo_parent_for(dest_root: Path, dst_parent: Path, library_user: str = "raphael") -> str:
    """Calcula o caminho do parent no MongoDB (ex: /raphael/Filmes/NomeDoFilme)."""
    user_root = f"/{library_user.strip('/')}"
    try:
        rel_parts = dst_parent.resolve().relative_to(dest_root.resolve()).parts
        if not rel_parts:
            return user_root
        return user_root + "/" + "/".join(rel_parts).replace("\\", "/")
    except Exception:
        return user_root


# =====================================================================
# GERENCIAMENTO DE STAGE E ESPAÇO EM DISCO
# =====================================================================

def get_configured_staging_dirs() -> list[Path]:
    """Obtém as pastas de stage configuradas a partir do .env e stage_paths.json."""
    dirs: list[Path] = []
    seen: set[str] = set()

    # 1. Tenta ler do .env
    env_stages = os.getenv("STAGING_DIRS", os.getenv("STAGING_DIR", ""))
    if env_stages:
        for chunk in env_stages.split(";"):
            c_str = chunk.strip().strip('"')
            if c_str:
                p = Path(c_str).resolve()
                if str(p).lower() not in seen:
                    seen.add(str(p).lower())
                    dirs.append(p)

    # 2. Tenta ler do stage_paths.json
    stage_json = Path(__file__).resolve().parent.parent / "stage_paths.json"
    if stage_json.exists():
        try:
            data = json.loads(stage_json.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for item in data:
                    p = Path(item).resolve()
                    if str(p).lower() not in seen:
                        seen.add(str(p).lower())
                        dirs.append(p)
        except Exception:
            pass

    if not dirs:
        fallback = (Path(__file__).resolve().parent.parent / "staging").resolve()
        dirs.append(fallback)

    return dirs


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
    min_free_percent: int = 10,
) -> Path:
    """Retorna o diretório de stage por ordem de preferência que tenha espaço livre suficiente."""
    roots = stage_roots or get_configured_staging_dirs()
    if not roots:
        return Path("staging").resolve()

    min_free_percent = min(max(min_free_percent, 1), 90)

    # 1. Tenta a primeira pasta configurada (ordem de prioridade) que tenha espaço suficiente com reserva
    for root in roots:
        try:
            p = root
            while not p.exists() and p.parent != p:
                p = p.parent
            usage = shutil.disk_usage(p)
            reserve = usage.total * min_free_percent // 100
            min_free = max(5 * 1024**3, reserve)  # Mínimo de 5GB de folga ou reserva percentual
            if usage.free - (required_bytes or 0) >= min_free:
                return root
        except Exception:
            continue

    # 2. Fallback: seleciona o disco com maior espaço livre absoluto
    return max(roots, key=get_free_bytes)


def wait_for_disk_capacity(
    target_path: Path,
    required_bytes: int | None,
    min_free_percent: int = 10,
    poll_seconds: int = 10,
) -> None:
    """Bloqueia a execução até que haja espaço suficiente em disco."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    last_log = 0.0
    while True:
        try:
            usage = shutil.disk_usage(target_path.parent)
            reserve = usage.total * min_free_percent // 100
            if usage.free - (required_bytes or 0) >= reserve:
                return
            now = time.time()
            if now - last_log >= 30:
                logger.warning(
                    "Disco aguardando liberação de espaço em %s: Livre=%.1f GB (Reserva=%d%%, Necessário=%.1f GB)",
                    target_path.parent,
                    usage.free / 1024**3,
                    min_free_percent,
                    (required_bytes or 0) / 1024**3,
                )
                last_log = now
        except Exception as exc:
            logger.debug("Erro ao checar espaço em disco: %s", exc)
            return
        time.sleep(poll_seconds)


# =====================================================================
# RASTREAMENTO E QUARENTENA DE FALHAS (.STRM QUEBRADOS / 401 / 404)
# =====================================================================

class FailureTracker:
    """
    Rastreia falhas de download (.strm com links expirados, 401, 404 ou erros)
    para evitar que o programa fique em loop infinito tentando baixar a mesma
    mídia corrompida sempre que for iniciado.
    """

    def __init__(self, cache_file: Path | None = None, cooldown_seconds: int = 7200):
        if cache_file is None:
            cache_file = Path.home() / ".nebula_strm_failures.json"
        self.cache_file = cache_file
        self.cooldown_seconds = cooldown_seconds
        self.failures: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self.cache_file.exists():
            try:
                with self.cache_file.open("r", encoding="utf-8") as f:
                    self.failures = json.load(f)
            except Exception as exc:
                logger.debug("Falha ao carregar tracker de falhas: %s", exc)
                self.failures = {}

    def _save(self) -> None:
        try:
            with self.cache_file.open("w", encoding="utf-8") as f:
                json.dump(self.failures, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.debug("Falha ao salvar tracker de falhas: %s", exc)

    def should_skip(self, file_path: Path) -> tuple[bool, str]:
        key = str(file_path.resolve())
        with self._lock:
            info = self.failures.get(key)
            if not info:
                return False, ""
            last_time = info.get("timestamp", 0)
            count = info.get("count", 1)
            last_error = info.get("error", "Erro desconhecido")
            now = time.time()

            # Se o erro for permanente (401, 403, 404, 410) ou se falhou mais de 2 vezes
            is_fatal = any(code in last_error for code in ("401", "403", "404", "410"))
            effective_cooldown = self.cooldown_seconds if not is_fatal else self.cooldown_seconds * 2

            if (now - last_time) < effective_cooldown:
                return True, f"{last_error} (Tentativas: {count}, Cooldown ativo)"
            return False, ""

    def record_failure(self, file_path: Path, error: str) -> None:
        key = str(file_path.resolve())
        with self._lock:
            info = self.failures.get(key, {"count": 0})
            info["count"] = info.get("count", 0) + 1
            info["timestamp"] = time.time()
            info["error"] = str(error)[:200]
            self.failures[key] = info
            self._save()

    def record_success(self, file_path: Path) -> None:
        key = str(file_path.resolve())
        with self._lock:
            if key in self.failures:
                del self.failures[key]
                self._save()

    def clear(self) -> None:
        with self._lock:
            self.failures.clear()
            self._save()


# =====================================================================
# VALIDAÇÃO CONTRA MONGODB (MÍDIAS CONCLUÍDAS OU ATIVAS)
# =====================================================================

class MediaValidator:
    """Validador central de mídias concluídas ou ativas no MongoDB."""

    def __init__(self, mongo_uri: str, db_name: str = "ftp", library_user: str = "raphael"):
        self.mongo_uri = mongo_uri
        self.db_name = db_name
        self.library_user = library_user
        self.completed_movies: set[tuple[str, str]] = set()
        self.completed_episodes: set[tuple[str, int, int]] = set()
        self.exact_completed: set[tuple[str, str]] = set()
        self.stem_completed: set[tuple[str, str]] = set()
        self.active_paths_or_names: set[str] = set()
        self.last_cache_time = 0.0

    def refresh_cache(self, force: bool = False) -> None:
        """Atualiza os índices de mídias concluídas e em andamento do MongoDB."""
        now = time.time()
        if not force and now - self.last_cache_time < 120:
            return

        if not self.mongo_uri or "mongodb://test" in self.mongo_uri:
            return

        try:
            client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=3000)
            db = client[self.db_name]

            exact_set: set[tuple[str, str]] = set()
            stem_set: set[tuple[str, str]] = set()
            movies: set[tuple[str, str]] = set()
            episodes: set[tuple[str, int, int]] = set()
            active: set[str] = set()

            for doc in db.files.find(
                {"type": "file"},
                {"parent": 1, "name": 1, "status": 1, "local_path": 1},
            ):
                parent = str(doc.get("parent", "")).casefold()
                name = str(doc.get("name", ""))
                status = doc.get("status")
                local_path = doc.get("local_path")

                if not name:
                    continue

                name_lower = name.casefold()
                stem_lower = Path(name).stem.casefold()

                if status == "completed":
                    exact_set.add((parent, name_lower))
                    stem_set.add((parent, stem_lower))

                    parts = [p for p in parent.split("/") if p]
                    if "filmes" in parts:
                        idx = parts.index("filmes")
                        if len(parts) > idx + 1:
                            m_id = movie_identity(parts[idx + 1])
                            if m_id:
                                movies.add(m_id)
                        m_id = movie_identity(stem_lower)
                        if m_id:
                            movies.add(m_id)
                    elif "series" in parts:
                        idx = parts.index("series")
                        if len(parts) > idx + 1:
                            ep_id = episode_identity(parts[idx + 1], name)
                            if ep_id:
                                episodes.add(ep_id)
                elif status in ACTIVE_STATUSES:
                    active.add(name_lower)
                    active.add(stem_lower)
                    if local_path:
                        active.add(str(local_path).lower())

            self.exact_completed = exact_set
            self.stem_completed = stem_set
            self.completed_movies = movies
            self.completed_episodes = episodes
            self.active_paths_or_names = active
            self.last_cache_time = now
            client.close()
            logger.debug(
                "Índice Mongo atualizado: %d filmes concl., %d eps concl., %d ativos.",
                len(movies), len(episodes), len(active)
            )
        except Exception as exc:
            logger.warning("Falha ao atualizar índice Mongo: %s", exc)

    def is_already_completed_or_active(
        self,
        strm_path: Path,
        url: str | None = None,
        destination: Path | None = None,
        dest_root: Path | None = None,
    ) -> tuple[bool, str]:
        """
        Verifica se a mídia do .strm já foi concluída no Telegram ou se já está na fila/stage.
        Retorna (is_dupe, motivo).
        """
        self.refresh_cache()

        # 1. Verificação por ID contido na URL do stream
        if url:
            try:
                parsed = urlsplit(url)
                params = parse_qs(parsed.query)
                file_id_str = params.get("id", [None])[0]
                if not file_id_str:
                    match_url_id = re.search(r"/(\d+)\.(?:mkv|mp4|avi|ts)$", parsed.path)
                    if match_url_id:
                        file_id_str = match_url_id.group(1)

                if file_id_str and self.mongo_uri and "mongodb://test" not in self.mongo_uri:
                    client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=2000)
                    try:
                        db = client[self.db_name]
                        doc = None
                        try:
                            doc = db.files.find_one({"_id": ObjectId(file_id_str), "status": "completed"}, {"_id": 1})
                        except Exception:
                            pass
                        if not doc:
                            doc = db.files.find_one({"_id": file_id_str, "status": "completed"}, {"_id": 1})
                        if doc:
                            return True, f"ID de stream {file_id_str} já concluído no MongoDB"
                    finally:
                        client.close()
            except Exception:
                pass

        # 2. Verificação de status ativo (em fila/download/upload)
        strm_stem_lower = strm_path.stem.casefold()
        if strm_stem_lower in self.active_paths_or_names:
            return True, f"Mídia '{strm_path.stem}' já está ativa/em fila no Nebula"

        # 3. Verificação por identidade de Filme (Título + Ano)
        m_id = movie_identity(strm_path.stem) or movie_identity(strm_path.parent.name)
        if m_id and m_id in self.completed_movies:
            return True, f"Filme '{m_id[0]} ({m_id[1]})' já concluído no Nebula"

        # 4. Verificação por identidade de Série (Nome + Temporada + Episódio)
        parent_name = strm_path.parent.name
        grandparent_name = strm_path.parent.parent.name if strm_path.parent.parent else ""
        s_candidate = parent_name if "season" not in parent_name.lower() else grandparent_name
        ep_id = episode_identity(s_candidate, strm_path.name)
        if ep_id and ep_id in self.completed_episodes:
            return True, f"Episódio '{ep_id[0]} S{ep_id[1]:02d}E{ep_id[2]:02d}' já concluído no Nebula"

        # 5. Verificação por destino exato no Mongo (parent, name)
        if destination and dest_root:
            parent = mongo_parent_for(dest_root, destination.parent, self.library_user).casefold()
            name_lower = destination.name.casefold()
            stem_lower = destination.stem.casefold()

            if (parent, name_lower) in self.exact_completed or (parent, stem_lower) in self.stem_completed:
                return True, f"Destino '{parent}/{destination.name}' já concluído no Nebula"

        return False, ""


# =====================================================================
# DOWNLOAD MULTIPART RESILIENTE
# =====================================================================

def download_strm_multipart(
    url: str,
    target_path: Path,
    parts_count: int = 20,
    read_timeout: int = 20,
    max_retries: int = 4,
    min_free_percent: int = 10,
) -> Path:
    """
    Realiza o download de uma URL com suporte a Range requests em múltiplas partes,
    com retomada (resume) de partes baixadas e retentativas automáticas.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.parent / f".{target_path.stem}.download"
    part_paths: list[Path] = []

    # Checar se já existe no destino
    if target_path.exists() and target_path.stat().st_size > 0:
        return target_path

    socket.setdefaulttimeout(read_timeout)

    try:
        probe = Request(url, headers={"Range": "bytes=0-0", "User-Agent": "Mozilla/5.0"})
        with urlopen(probe, timeout=read_timeout) as response:
            content_range = response.headers.get("Content-Range", "")
            match = re.match(r"bytes\s+0-0/(\d+)", content_range)

            if not match:
                # Servidor não suporta Range: download sequencial direto
                logger.info("Download direto sem suporte a Range: %s", target_path.name)
                with tmp_path.open("wb") as out:
                    shutil.copyfileobj(response, out, length=1024 * 1024)
                total = tmp_path.stat().st_size
            else:
                total = int(match.group(1))
                wait_for_disk_capacity(target_path, total, min_free_percent)

                parts_count = min(max(parts_count, 1), 32)
                part_size = (total + parts_count - 1) // parts_count
                part_paths = [Path(f"{tmp_path}.part{index}") for index in range(parts_count)]

                progress_lock = threading.Lock()
                progress = {"downloaded": 0, "next_percent": 1, "last_logged_bytes": 0, "start_time": time.time()}

                # Verificar partes válidas existentes para Resume
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
                    logger.info(
                        "Resumindo %s: %.1f MB / %.1f MB (%d%%) já no disco.",
                        target_path.name,
                        progress["downloaded"] / (1024 * 1024),
                        total / (1024 * 1024),
                        percent,
                    )
                    progress["next_percent"] = percent + 1
                    progress["last_logged_bytes"] = progress["downloaded"]

                def download_part(index: int) -> None:
                    start = index * part_size
                    end = min(start + part_size, total) - 1
                    expected = end - start + 1
                    part_file = part_paths[index]

                    if part_file.exists() and part_file.stat().st_size == expected:
                        return

                    part_file.unlink(missing_ok=True)
                    part_tmp = Path(f"{part_file}.tmp")
                    part_tmp.unlink(missing_ok=True)

                    last_error = None
                    for attempt in range(1, max_retries + 1):
                        attempt_downloaded = 0
                        try:
                            part_tmp.unlink(missing_ok=True)
                            req = Request(
                                url,
                                headers={"Range": f"bytes={start}-{end}", "User-Agent": "Mozilla/5.0"},
                            )
                            with urlopen(req, timeout=read_timeout) as part_resp:
                                if getattr(part_resp, "status", None) and part_resp.status not in (200, 206):
                                    raise IOError(f"HTTP status {part_resp.status} na parte {index + 1}")
                                if start > 0 and getattr(part_resp, "status", None) == 200:
                                    raise IOError(f"Servidor ignorou Range na parte {index + 1}")

                                with part_tmp.open("wb") as out:
                                    while chunk := part_resp.read(64 * 1024):
                                        out.write(chunk)
                                        attempt_downloaded += len(chunk)
                                        with progress_lock:
                                            progress["downloaded"] += len(chunk)
                                            percent = min(100, int(progress["downloaded"] * 100 / total))
                                            bytes_since = progress["downloaded"] - progress["last_logged_bytes"]
                                            if percent >= progress["next_percent"] or bytes_since >= 25 * 1024 * 1024:
                                                elapsed = max(0.1, time.time() - progress["start_time"])
                                                speed_mb = (progress["downloaded"] / (1024 * 1024)) / elapsed
                                                logger.info(
                                                    "Baixando %s: %.1f MB / %.1f MB (%d%%) - Vel: %.1f MB/s",
                                                    target_path.name,
                                                    progress["downloaded"] / (1024 * 1024),
                                                    total / (1024 * 1024),
                                                    percent,
                                                    speed_mb,
                                                )
                                                progress["next_percent"] = percent + 1
                                                progress["last_logged_bytes"] = progress["downloaded"]

                            if not part_tmp.exists() or part_tmp.stat().st_size != expected:
                                actual = part_tmp.stat().st_size if part_tmp.exists() else 0
                                raise IOError(f"Parte {index + 1} incompleta ({actual}/{expected} bytes)")

                            part_tmp.replace(part_file)
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
                            if attempt < max_retries:
                                time.sleep(attempt * 2)

                    raise IOError(f"Parte {index + 1} falhou após {max_retries} tentativas: {last_error}")

                with ThreadPoolExecutor(max_workers=parts_count) as executor:
                    list(executor.map(download_part, range(parts_count)))

                logger.info("Unindo partes do arquivo: %s", target_path.name)
                with tmp_path.open("wb") as out:
                    for part_path in part_paths:
                        with part_path.open("rb") as part:
                            shutil.copyfileobj(part, out, length=1024 * 1024)
                        part_path.unlink(missing_ok=True)

                if tmp_path.stat().st_size != total:
                    raise IOError(f"Arquivo final com tamanho incorreto ({tmp_path.stat().st_size} != {total})")

        tmp_path.replace(target_path)
        # Limpar sobras de arquivos temporários
        for leftover in target_path.parent.glob(f".{target_path.stem}.*.download*"):
            with contextlib.suppress(Exception):
                leftover.unlink(missing_ok=True)
        return target_path

    except Exception:
        # Limpar apenas os arquivos temporários (.tmp), PRESERVANDO as partes já baixadas (.part)
        # para que a retomada (resume) funcione imediatamente ao reiniciar ou retentar!
        with contextlib.suppress(Exception):
            tmp_path.unlink(missing_ok=True)
        for part_path in part_paths:
            with contextlib.suppress(Exception):
                Path(f"{part_path}.tmp").unlink(missing_ok=True)
        raise


# =====================================================================
# ENFILEIRAMENTO NO MONGODB DO NEBULA
# =====================================================================

def ensure_mongo_parent_structure(
    mongo_uri: str,
    db_name: str,
    library_user: str,
    dest_root: Path,
    dst_parent: Path,
) -> None:
    """Garante que todos os diretórios pais (type: 'dir') existam no MongoDB."""
    try:
        rel_parts = dst_parent.resolve().relative_to(dest_root.resolve()).parts
    except Exception:
        return

    if not rel_parts or not mongo_uri or "mongodb://test" in mongo_uri:
        return

    try:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
        files = client[db_name].files
        parent = f"/{library_user.strip('/')}"
        now = int(time.time())

        for name in rel_parts:
            if not files.find_one({"parent": parent, "name": name}, {"_id": 1}):
                doc = {
                    "type": "dir",
                    "name": name,
                    "parent": parent,
                    "size": 0,
                    "ctime": now,
                    "mtime": now,
                }
                with contextlib.suppress(DuplicateKeyError):
                    files.insert_one(doc)
            parent = f"{parent}/{name}" if parent != "/" else f"/{name}"
        client.close()
    except Exception as exc:
        logger.warning("Falha ao criar diretórios pai no Mongo: %s", exc)


def register_in_nebula_queue(
    downloaded_file: Path,
    destination: Path,
    dest_root: Path,
    mongo_uri: str,
    db_name: str = "ftp",
    library_user: str = "raphael",
    delete_source: bool = True,
) -> bool:
    """
    Registra a mídia baixada na coleção files do MongoDB do Nebula com status='queued'
    e delete_source=True, permitindo que o Nebula assuma o envio e a limpeza.
    """
    if not mongo_uri or "mongodb://test" in mongo_uri:
        return True

    size = downloaded_file.stat().st_size
    parent_path = mongo_parent_for(dest_root, destination.parent, library_user)
    ensure_mongo_parent_structure(mongo_uri, db_name, library_user, dest_root, destination.parent)

    now = int(time.time())
    try:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
        files = client[db_name].files

        # 1. Verifica se já existe por local_path
        existing = files.find_one({"local_path": str(downloaded_file)}, {"status": 1})
        if existing:
            files.update_one(
                {"_id": existing["_id"]},
                {
                    "$set": {
                        "name": destination.name,
                        "parent": parent_path,
                        "size": size,
                        "status": "queued",
                        "mtime": now,
                        "delete_source": bool(delete_source),
                    }
                },
            )
        else:
            doc = {
                "type": "file",
                "name": destination.name,
                "parent": parent_path,
                "size": size,
                "status": "queued",
                "local_path": str(downloaded_file),
                "mtime": now,
                "ctime": now,
                "parts": [],
                "delete_source": bool(delete_source),
                "search_name": destination.name.strip().casefold(),
                "search_parent": parent_path.strip().casefold(),
            }
            files.update_one(
                {"parent": parent_path, "name": destination.name},
                {"$set": doc},
                upsert=True,
            )

        client.close()
        logger.info(
            "Enfileirado no Nebula com sucesso: %s -> %s (tamanho=%.1f MB, delete_source=%s)",
            downloaded_file.name,
            parent_path,
            size / (1024 * 1024),
            delete_source,
        )
        return True
    except Exception as exc:
        logger.error("Erro ao registrar na fila do MongoDB: %s", exc)
        return False


# =====================================================================
# VARREDURA E ORDENAÇÃO POR PRIORIDADE & ANO
# =====================================================================

def iter_strm_files_prioritized(
    sources: list[Path],
    exclude_dirs: set[str] | None = None,
) -> list[tuple[Path, Path, str, int]]:
    """
    Varre as fontes e retorna lista de tuplas (source_root, media_path, category, year)
    ordenadas por:
      1. Categoria: Filmes -> Porno -> Series -> Outros
      2. Filmes: Ano decrescente (2026 -> 2025 -> ...) e depois título alfabético
      3. Séries: Nome da série -> Temporada -> Episódio
    Suporta arquivos .strm e arquivos de mídia prontos (.mkv, .mp4, etc).
    """
    exclude = {d.lower() for d in (exclude_dirs or set())}
    categorized: dict[str, list[tuple[Path, Path, str, int]]] = {cat: [] for cat in CATEGORY_PRIORITY}
    categorized["other"] = []

    for source in sources:
        if not source.exists():
            continue
        for root, dirs, files in os.walk(source):
            root_path = Path(root)
            try:
                rel_parts = root_path.relative_to(source).parts
                if rel_parts and rel_parts[0].lower() in exclude:
                    continue
            except ValueError:
                pass

            for name in files:
                ext = Path(name).suffix.lower()
                if ext not in MONITORED_EXTENSIONS:
                    continue
                if is_incomplete_filename(name):
                    continue

                media_path = root_path / name
                cat = get_category_from_path(media_path, source)
                year = extract_media_year(media_path.name, media_path.parent.name)

                item = (source, media_path, cat, year)
                if cat in categorized:
                    categorized[cat].append(item)
                else:
                    categorized["other"].append(item)

    # 1. Ordenação de Filmes: Ano mais recente primeiro (ex: 2026 -> 2025), desempate por nome
    categorized["filmes"].sort(
        key=lambda it: (-it[3], it[1].name.lower())
    )

    # 2. Ordenação de Séries: Nome da pasta/série, temporada e episódio
    def series_sort_key(it: tuple[Path, Path, str, int]):
        src_path = it[1]
        ep_info = episode_identity(src_path.parent.name, src_path.name)
        if ep_info:
            return (ep_info[0], ep_info[1], ep_info[2])
        return (src_path.parent.name.lower(), src_path.name.lower(), 0)

    categorized["series"].sort(key=series_sort_key)

    # Monta a fila combinada por prioridade
    results: list[tuple[Path, Path, str, int]] = []
    for cat in CATEGORY_PRIORITY:
        results.extend(categorized[cat])
    results.extend(categorized["other"])

    return results


# =====================================================================
# DELEÇÃO E LIMPEZA DE .STRM / MÍDIAS E PASTAS VAZIAS
# =====================================================================

def delete_strm_and_empty_parents(
    strm_path: Path,
    source_root: Path,
    dry_run: bool = False,
) -> None:
    """
    Deleta o arquivo .strm ou mídia de origem e, em seguida, remove recursivamente
    as pastas pai que ficarem vazias até a raiz source_root (sem apagar a raiz).
    """
    try:
        resolved_strm = strm_path.resolve()
        resolved_source = source_root.resolve()

        if not dry_run:
            resolved_strm.unlink(missing_ok=True)
            logger.info("Arquivo de origem removido: %s", resolved_strm)
        else:
            logger.info("[DRY-RUN] Arquivo de origem seria removido: %s", resolved_strm)

        current = resolved_strm.parent
        while current and current != resolved_source and resolved_source in current.parents:
            if not current.exists():
                current = current.parent
                continue

            # Verificar se ainda existem arquivos de mídia
            has_media = False
            try:
                for _, _, files in os.walk(current):
                    if any(Path(f).suffix.lower() in MONITORED_EXTENSIONS for f in files):
                        has_media = True
                        break
            except Exception:
                break

            if has_media:
                break

            # Se não há mais nenhuma mídia na pasta, remove o diretório
            folder_name = current.name.lower()
            if folder_name in ("filmes", "series", "porno", "midias", "strm", "strm_library"):
                # Para pastas raízes conhecidas de categoria, remove apenas se totalmente vazia
                try:
                    if not any(current.iterdir()):
                        if not dry_run:
                            current.rmdir()
                            logger.info("Pasta raiz de categoria vazia removida: %s", current)
                        else:
                            logger.info("[DRY-RUN] Pasta raiz de categoria vazia seria removida: %s", current)
                except Exception:
                    pass
                break

            try:
                if not dry_run:
                    shutil.rmtree(current, ignore_errors=True)
                    if not current.exists():
                        logger.info("Pasta sem mídias removida: %s", current)
                    else:
                        break
                else:
                    logger.info("[DRY-RUN] Pasta sem mídias seria removida: %s", current)
                    break
            except Exception as exc:
                logger.debug("Falha ao remover pasta %s: %s", current, exc)
                break

            current = current.parent
    except Exception as exc:
        logger.warning("Erro ao remover arquivo ou pasta pai %s: %s", strm_path, exc)


def prune_completed_strm_files(
    sources: list[Path],
    validator: MediaValidator,
    dest_root: Path,
    dry_run: bool = False,
) -> int:
    """Remove arquivos locais cujas mídias já foram concluídas no Telegram."""
    logger.info("Varrendo arquivos concluídos para remoção...")
    validator.refresh_cache(force=True)
    removed = 0

    for source in sources:
        for root, _, files in os.walk(source):
            for name in files:
                ext = Path(name).suffix.lower()
                if ext not in MONITORED_EXTENSIONS:
                    continue
                media_path = Path(root) / name
                try:
                    if ext == ".strm":
                        url = read_strm_url(media_path)
                        media_ext = guess_media_extension(url)
                        dest_file = destination_for(source, dest_root, media_path.with_suffix(media_ext))
                        is_dupe, _ = validator.is_already_completed_or_active(media_path, url, dest_file, dest_root)
                    else:
                        dest_file = destination_for(source, dest_root, media_path)
                        is_dupe, _ = validator.is_already_completed_or_active(media_path, "", dest_file, dest_root)
                    if is_dupe:
                        delete_strm_and_empty_parents(media_path, source, dry_run=dry_run)
                        removed += 1
                except Exception:
                    continue

    logger.info("Total de mídias concluídas limpas: %d", removed)
    return removed


# =====================================================================
# LOOP PRINCIPAL DO STRM / MEDIA DOWNLOADER
# =====================================================================

DOWNLOAD_LOCK = threading.Lock()


def process_strm_item(
    source_root: Path,
    strm_path: Path,
    category: str,
    year: int,
    dest_root: Path,
    staging_dirs: list[Path],
    validator: MediaValidator,
    mongo_uri: str,
    db_name: str,
    library_user: str,
    parts_count: int = 20,
    min_free_percent: int = 10,
    dry_run: bool = False,
    failure_tracker: FailureTracker | None = None,
) -> bool:
    """
    Processa um único arquivo de mídia (seja .strm ou mídia pronta local) por vez:
    - Se for .strm: validação, download multipart sequencial para o stage livre,
      enfileiramento no MongoDB e deleção do .strm de origem.
    - Se for mídia pronta (.mkv, .mp4, etc): validação, movimentação para o stage livre,
      enfileiramento no MongoDB e limpeza de pastas pai vazias na origem.
    """
    with DOWNLOAD_LOCK:
        is_strm = strm_path.suffix.lower() == ".strm"

        if is_strm:
            try:
                url = read_strm_url(strm_path)
            except Exception as exc:
                logger.warning("Falha ao ler URL do .strm %s: %s", strm_path, exc)
                if failure_tracker:
                    failure_tracker.record_failure(strm_path, f"Erro de leitura: {exc}")
                return False

            media_ext = guess_media_extension(url)
            media_source = strm_path.with_suffix(media_ext)
            destination = destination_for(source_root, dest_root, media_source)

            # 1. Validação de Mídia Concluída ou Ativa
            is_dupe, reason = validator.is_already_completed_or_active(
                strm_path=strm_path,
                url=url,
                destination=destination,
                dest_root=dest_root,
            )
            if is_dupe:
                logger.info("Mídia já concluída ou ativa no Nebula (%s). Removendo .strm: %s", reason, strm_path.name)
                delete_strm_and_empty_parents(strm_path, source_root, dry_run=dry_run)
                if failure_tracker:
                    failure_tracker.record_success(strm_path)
                return False

            # 2. Obter tamanho remoto e selecionar melhor pasta de stage
            try:
                remote_size = remote_content_size(url)
            except Exception as exc:
                logger.warning("Falha ao consultar tamanho remoto de %s: %s", strm_path.name, exc)
                if failure_tracker:
                    failure_tracker.record_failure(strm_path, str(exc))
                return False

            best_stage = get_best_staging_root(staging_dirs, remote_size, min_free_percent)
            target_stage_file = best_stage / "strm" / destination.name

            logger.info(
                "[1 MÍDIA POR VEZ] Iniciando download: %s [%s%s] -> Stage: %s",
                strm_path.name,
                category.upper(),
                f"/{year}" if year else "",
                target_stage_file.parent,
            )

            if dry_run:
                logger.info("[DRY-RUN] Simulação de download para: %s", target_stage_file)
                delete_strm_and_empty_parents(strm_path, source_root, dry_run=True)
                return True

            # 3. Executar Download Multipart (exclusivo para esta mídia)
            try:
                downloaded = download_strm_multipart(
                    url=url,
                    target_path=target_stage_file,
                    parts_count=parts_count,
                    min_free_percent=min_free_percent,
                )
            except Exception as exc:
                logger.error("Erro no download de %s: %s", strm_path.name, exc)
                if failure_tracker:
                    failure_tracker.record_failure(strm_path, str(exc))
                return False

            # 4. Registrar na fila do MongoDB do Nebula
            registered = register_in_nebula_queue(
                downloaded_file=downloaded,
                destination=destination,
                dest_root=dest_root,
                mongo_uri=mongo_uri,
                db_name=db_name,
                library_user=library_user,
                delete_source=True,
            )

            # 5. Se registrado com sucesso, remove .strm e pasta pai vazia
            if registered:
                delete_strm_and_empty_parents(strm_path, source_root, dry_run=dry_run)
                if failure_tracker:
                    failure_tracker.record_success(strm_path)
                logger.info("[1 MÍDIA POR VEZ] Conclusão do processamento de: %s. Pronto para a próxima mídia.", strm_path.name)

            return registered

        else:
            # Arquivo de Mídia Pronta Local (.mkv, .mp4, .avi, etc)
            destination = destination_for(source_root, dest_root, strm_path)

            # 1. Validação se já concluído ou ativo
            is_dupe, reason = validator.is_already_completed_or_active(
                strm_path=strm_path,
                url="",
                destination=destination,
                dest_root=dest_root,
            )
            if is_dupe:
                logger.info("Mídia local pronta já concluída ou ativa no Nebula (%s). Removendo: %s", reason, strm_path.name)
                delete_strm_and_empty_parents(strm_path, source_root, dry_run=dry_run)
                if failure_tracker:
                    failure_tracker.record_success(strm_path)
                return False

            # 2. Obter tamanho do arquivo local e alocar melhor pasta de stage
            file_size = strm_path.stat().st_size
            best_stage = get_best_staging_root(staging_dirs, file_size, min_free_percent)
            target_stage_file = best_stage / "strm" / destination.name

            logger.info(
                "[1 MÍDIA POR VEZ] Mídia pronta detectada: %s [%s%s] -> Movendo para Stage: %s",
                strm_path.name,
                category.upper(),
                f"/{year}" if year else "",
                target_stage_file.parent,
            )

            if dry_run:
                logger.info("[DRY-RUN] Simulação de envio para stage: %s", target_stage_file)
                delete_strm_and_empty_parents(strm_path, source_root, dry_run=True)
                return True

            wait_for_disk_capacity(target_stage_file, file_size, min_free_percent)
            target_stage_file.parent.mkdir(parents=True, exist_ok=True)

            try:
                if strm_path.resolve() != target_stage_file.resolve():
                    shutil.move(str(strm_path), str(target_stage_file))
                logger.info("Mídia movida com sucesso para o Stage: %s", target_stage_file)
            except Exception as exc:
                logger.error("Erro ao mover mídia pronta para o stage %s: %s", target_stage_file, exc)
                if failure_tracker:
                    failure_tracker.record_failure(strm_path, str(exc))
                return False

            # Limpar pastas pai vazias na origem
            delete_strm_and_empty_parents(strm_path, source_root, dry_run=dry_run)

            # 3. Registrar na fila do MongoDB do Nebula
            registered = register_in_nebula_queue(
                downloaded_file=target_stage_file,
                destination=destination,
                dest_root=dest_root,
                mongo_uri=mongo_uri,
                db_name=db_name,
                library_user=library_user,
                delete_source=True,
            )

            if registered:
                if failure_tracker:
                    failure_tracker.record_success(strm_path)
                logger.info("[1 MÍDIA POR VEZ] Mídia pronta movida para Stage: %s -> %s e enfileirada no Nebula.", destination.name, target_stage_file)
                logger.info("[1 MÍDIA POR VEZ] Conclusão do processamento de: %s. Pronto para a próxima mídia.", destination.name)

            return registered


def run_downloader(args: argparse.Namespace) -> int:
    setup_logger(args.verbose)
    logger.info("=== STRM Downloader & Feeder para NebulaFTP ===")

    mongo_uri = args.mongo or os.getenv("MONGODB", "mongodb://localhost:27017")
    db_name = args.db_name or os.getenv("MONGO_DATABASE", "ftp")
    library_user = args.user or os.getenv("NEBULA_LIBRARY_USER", "raphael")
    dest_root = Path(args.dest or os.getenv("DESTINATION_ROOT", "D:/midias")).resolve()

    sources = [
        Path(p.strip()).resolve()
        for raw in (args.sources or [os.getenv("STRM_SOURCE_DIR", "strm_library")])
        for p in str(raw).split(";")
        if p.strip()
    ]
    exclude_dirs = set(args.exclude.split(",")) if args.exclude else set()

    staging_dirs = get_configured_staging_dirs()
    logger.info("Pastas de Stage detectadas: %s", [str(d) for d in staging_dirs])
    logger.info("Fontes STRM: %s", [str(s) for s in sources])

    validator = MediaValidator(mongo_uri=mongo_uri, db_name=db_name, library_user=library_user)
    validator.refresh_cache(force=True)

    failure_tracker = FailureTracker()
    if getattr(args, "reset_failures", False):
        failure_tracker.clear()
        logger.info("Histórico de falhas de download redefinido.")

    if args.prune_completed:
        prune_completed_strm_files(sources, validator, dest_root, dry_run=args.dry_run)
        if not args.watch:
            return 0

    stats = DownloaderStats()

    while True:
        try:
            logger.info("Escaneando e priorizando arquivos .strm...")
            items = iter_strm_files_prioritized(sources, exclude_dirs)
            logger.info("Total de arquivos .strm encontrados: %d", len(items))

            processed_any = False
            for source_root, strm_path, category, year in items:
                stats.scanned += 1

                # Checagem contra loop de mídias que falharam recentemente (401/404/erros)
                should_skip, skip_reason = failure_tracker.should_skip(strm_path)
                if should_skip:
                    logger.debug("Pulando mídia com falha recente: %s (%s)", strm_path.name, skip_reason)
                    continue

                success = process_strm_item(
                    source_root=source_root,
                    strm_path=strm_path,
                    category=category,
                    year=year,
                    dest_root=dest_root,
                    staging_dirs=staging_dirs,
                    validator=validator,
                    mongo_uri=mongo_uri,
                    db_name=db_name,
                    library_user=library_user,
                    parts_count=args.parts,
                    min_free_percent=args.min_free_percent,
                    dry_run=args.dry_run,
                    failure_tracker=failure_tracker,
                )
                if success:
                    stats.downloaded += 1
                    stats.queued += 1
                    processed_any = True
                    # Breve pausa para o Nebula consumir/respirar
                    time.sleep(1)
                else:
                    stats.failed += 1

            if not args.watch:
                break

            logger.info("Ciclo concluído. Aguardando %d segundos para próxima varredura...", args.interval)
            time.sleep(args.interval)

        except KeyboardInterrupt:
            logger.info("Interrompido pelo usuário.")
            break
        except Exception as exc:
            logger.error("Erro no loop principal: %s", exc)
            if not args.watch:
                return 1
            time.sleep(args.interval)

    logger.info(
        "Resumo Final: Escaneados=%d | Baixados/Enfileirados=%d | Falhas=%d",
        stats.scanned, stats.queued, stats.failed
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="STRM Downloader & Feeder autônomo para NebulaFTP com ordenação por ano e stage automático.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--sources", "-s",
        nargs="+",
        help="Caminhos de diretórios de origem contendo arquivos .strm",
    )
    parser.add_argument(
        "--dest", "-d",
        help="Diretório raiz de destino lógico da biblioteca (ex: D:/midias)",
    )
    parser.add_argument(
        "--mongo",
        help="URI de conexão ao MongoDB (padrão: env MONGODB ou mongodb://localhost:27017)",
    )
    parser.add_argument(
        "--db-name",
        default="ftp",
        help="Nome do banco de dados no MongoDB",
    )
    parser.add_argument(
        "--user", "-u",
        default="raphael",
        help="Usuário da biblioteca no NebulaFTP",
    )
    parser.add_argument(
        "--parts", "-p",
        type=int,
        default=int(os.getenv("STRM_DOWNLOAD_PARTS", "20")),
        help="Quantidade de partes simultâneas por download HTTP Range",
    )
    parser.add_argument(
        "--min-free-percent",
        type=int,
        default=int(os.getenv("STRM_MIN_FREE_PERCENT", "10")),
        help="Percentual mínimo de espaço livre em disco de stage",
    )
    parser.add_argument(
        "--watch", "-w",
        action="store_true",
        help="Executar em modo contínuo (daemon/watcher)",
    )
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=60,
        help="Intervalo em segundos entre varreduras no modo --watch",
    )
    parser.add_argument(
        "--prune-completed",
        action="store_true",
        help="Remover arquivos .strm de mídias que já foram concluídas no Telegram",
    )
    parser.add_argument(
        "--exclude",
        help="Diretórios a excluir da varredura, separados por vírgula",
    )
    parser.add_argument(
        "--reset-failures",
        action="store_true",
        help="Limpar o histórico de falhas e tentar baixar novamente mídias que falharam anteriormente",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simular downloads e deleções sem alterar disco ou banco",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Logs detalhados de depuração",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return run_downloader(args)


if __name__ == "__main__":
    sys.exit(main())
