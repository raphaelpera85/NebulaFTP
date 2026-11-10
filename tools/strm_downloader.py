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
import filecmp
import json
import logging
import mimetypes
import os
import queue
import re
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
import urllib.error

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# --- RESILIÊNCIA DE REDE / DNS / SSL ---
# Evita DNS hijacking / sinkholing IPv6 praticado por operadoras/roteadores em domínios de
# streaming/CDN (que causam [SSL: TLSV1_UNRECOGNIZED_NAME]) forçando preferência por IPv4.
_ORIGINAL_GETADDRINFO = socket.getaddrinfo


def _ipv4_preferred_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family == 0:
        try:
            return _ORIGINAL_GETADDRINFO(host, port, socket.AF_INET, type, proto, flags)
        except socket.gaierror:
            pass
    return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)


socket.getaddrinfo = _ipv4_preferred_getaddrinfo

# Contexto SSL permissivo para CDNs / streamings com certificados SNI flexíveis
try:
    _ssl_ctx = ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = ssl.CERT_NONE
    ssl._create_default_https_context = lambda: _ssl_ctx
except Exception:
    pass

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

# Prioridade de categorias do Nebula: Animações > Filmes > Séries > Doramas > Novelas > Porno (A–Z)
CATEGORY_PRIORITY = ["animacoes", "filmes", "series", "doramas", "novelas", "porno"]
CATEGORY_ALIASES = {
    "anime": "animacoes",
    "animes": "animacoes",
    "animacao": "animacoes",
    "animacoes": "animacoes",
    "dorama": "doramas",
    "doramas": "doramas",
    "filme": "filmes",
    "filmes": "filmes",
    "novela": "novelas",
    "novelas": "novelas",
    "porno": "porno",
    "series": "series",
}

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


def load_m3u_priority_config() -> dict[str, list[str]]:
    """Lê prioridades da GUI sem depender de Mongo ou de estado em memória."""
    configured = os.getenv("NEBULA_M3U_PRIORITY_FILE")
    path = Path(configured) if configured else Path(__file__).resolve().parent.parent / "m3u_priority.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"paths": [], "terms": []}
    if not isinstance(value, dict):
        return {"paths": [], "terms": []}
    return {
        "paths": [str(item) for item in value.get("paths", [])],
        "terms": [str(item) for item in value.get("terms", [])],
    }


def is_priority_item(source_root: Path, media_path: Path, config: dict[str, list[str]] | None = None) -> bool:
    """Confere caminho relativo e título para priorizar download e upload."""
    config = config or load_m3u_priority_config()
    try:
        relative = media_path.relative_to(source_root).as_posix()
    except ValueError:
        relative = media_path.name
    relative_key = normalize_media_title(relative)
    path_keys = {normalize_media_title(item) for item in config.get("paths", [])}
    if relative_key in path_keys:
        return True
    haystack = normalize_media_title(f"{relative} {media_path.stem}")
    return any(normalize_media_title(term) and normalize_media_title(term) in haystack for term in config.get("terms", []))


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


def category_dir_priority(dirname: str) -> int:
    norm = unicodedata.normalize("NFKD", dirname).encode("ascii", "ignore").decode().lower()
    for idx, cat in enumerate(CATEGORY_PRIORITY):
        if cat in norm or (cat == "animacoes" and any(k in norm for k in ["animac", "anime"])) \
           or (cat == "filmes" and any(k in norm for k in ["filme", "movie"])) \
           or (cat == "doramas" and any(k in norm for k in ["dorama", "kdrama", "k-drama"])) \
           or (cat == "novelas" and any(k in norm for k in ["novela", "soap"])) \
           or (cat == "porno" and any(k in norm for k in ["porno", "porn", "xxx", "hentai", "adulto"])) \
           or (cat == "series" and any(k in norm for k in ["serie", "season", "tv"])):
            return idx
    return len(CATEGORY_PRIORITY)


def get_category_from_path(src: Path, source_root: Path) -> str:
    """Determina a categoria da mídia (animacoes/filmes/series/doramas/novelas/porno/other)."""
    try:
        rel = src.relative_to(source_root)
        parts = [unicodedata.normalize("NFKD", p).encode("ascii", "ignore").decode().lower() for p in rel.parts]
        for p in parts:
            if any(k in p for k in ["animac", "anime"]):
                return "animacoes"
            if any(k in p for k in ["filme", "movie"]):
                return "filmes"
            if any(k in p for k in ["dorama", "kdrama", "k-drama"]):
                return "doramas"
            if any(k in p for k in ["novela", "soap"]):
                return "novelas"
            if any(k in p for k in ["porno", "porn", "xxx", "hentai", "adulto"]):
                return "porno"
            if any(k in p for k in ["serie", "season"]) or p in ("tv", "show"):
                return "series"
    except ValueError:
        pass
    src_str = unicodedata.normalize("NFKD", str(src)).encode("ascii", "ignore").decode().lower()
    if any(k in src_str for k in ["animac", "anime"]):
        return "animacoes"
    if any(k in src_str for k in ["\\filmes\\", "/filmes/", "filme", "movie"]):
        return "filmes"
    if any(k in src_str for k in ["\\doramas\\", "/doramas/", "dorama", "kdrama", "k-drama"]):
        return "doramas"
    if any(k in src_str for k in ["\\novelas\\", "/novelas/", "novela", "soap"]):
        return "novelas"
    if any(k in src_str for k in ["\\porno\\", "/porno/", "porn", "xxx", "hentai", "adulto"]):
        return "porno"
    if any(k in src_str for k in ["\\series\\", "/series/", "serie", "season"]) or "\\tv\\" in src_str or "/tv/" in src_str:
        return "series"
    return "other"


def normalize_category_name(value: str) -> str | None:
    """Return a known library category, ignoring accents and case."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    return CATEGORY_ALIASES.get(normalized)


def relocate_strm_to_mongo_parent(
    strm_path: Path,
    source_root: Path,
    mongo_parent: str,
    library_user: str = "raphael",
    *,
    dry_run: bool = False,
) -> Path | None:
    """Move a category-misfiled STRM to its Mongo parent without overwriting files."""
    if strm_path.suffix.casefold() != ".strm" or strm_path.is_symlink():
        return strm_path

    root = source_root.resolve()
    try:
        source = strm_path.resolve(strict=True)
        relative_parts = source.relative_to(root).parts
    except (OSError, ValueError):
        logger.warning("STRM fora da raiz monitorada ou inacessível; preservado: %s", strm_path)
        return None

    parent_parts = [part for part in mongo_parent.replace("\\", "/").split("/") if part]
    if parent_parts and parent_parts[0].casefold() == library_user.strip("/").casefold():
        parent_parts = parent_parts[1:]
    if not parent_parts or any(part in {".", ".."} for part in parent_parts):
        return strm_path

    source_category = normalize_category_name(relative_parts[0]) if relative_parts else None
    target_category = normalize_category_name(parent_parts[0])
    if not source_category or not target_category or source_category == target_category:
        return strm_path

    target = root.joinpath(*parent_parts, strm_path.name)
    try:
        target.resolve().relative_to(root)
    except ValueError:
        logger.warning("Destino Mongo fora da raiz monitorada; STRM preservado: %s", strm_path)
        return None

    if target.exists():
        logger.warning(
            "Conflito de categoria: destino já existe; ambos STRMs foram preservados: %s -> %s",
            strm_path,
            target,
        )
        return None
    if dry_run:
        logger.info("[DRY-RUN] STRM seria reclassificado: %s -> %s", strm_path, target)
        return target

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Hard-link creation is atomic and fails if the destination exists.
        # Both paths stay on the monitored library; unlink only after linking succeeds.
        os.link(source, target)
        source.unlink()
    except FileExistsError:
        logger.warning(
            "Conflito de categoria durante movimentação; STRM preservado: %s -> %s",
            strm_path,
            target,
        )
        return None
    except OSError as exc:
        with contextlib.suppress(OSError):
            if target.exists() and os.path.samefile(source, target):
                target.unlink()
        logger.warning("Falha ao reclassificar STRM; original preservado: %s (%s)", strm_path, exc)
        return None

    logger.info("STRM reclassificado conforme Mongo: %s -> %s", strm_path, target)
    return target


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
        request = Request(url, headers={"Range": "bytes=0-0", "User-Agent": "VLC/3.0.21 LibVLC/3.0.21"})
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

    if parts and parts[0].casefold() in {"animações", "animacoes", "doramas", "novelas"}:
        return dest_root / Path(*parts)

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
    staging_dirs: list[Path] | None = None,
) -> Path:
    """Escolhe um Stage com espaço e preserva parciais ao trocar de volume."""
    target_path = Path(target_path)
    roots = [Path(root).resolve() for root in (staging_dirs or [target_path.parent])]
    relative_target = None
    current_root = None
    for root in roots:
        try:
            relative_target = target_path.resolve().relative_to(root)
            current_root = root
            break
        except ValueError:
            continue
    if relative_target is None:
        roots = [target_path.parent.resolve()]
        relative_target = Path(target_path.name)
        current_root = roots[0]

    min_free_percent = min(max(min_free_percent, 1), 90)
    roots = [current_root, *(root for root in roots if root != current_root)]
    last_log = 0.0
    while True:
        for root in roots:
            candidate = root / relative_target
            try:
                probe = candidate.parent
                while not probe.exists() and probe.parent != probe:
                    probe = probe.parent
                usage = shutil.disk_usage(probe)
                reserve = usage.total * min_free_percent // 100
                if usage.free - (required_bytes or 0) < reserve:
                    continue
                if candidate.resolve() != target_path.resolve():
                    logger.info(
                        "Stage %s com capacidade insuficiente (livre: %.1f GB, reserva %d%%). Transferindo parciais para %s...",
                        target_path.parent,
                        usage.free / 1024**3,
                        min_free_percent,
                        candidate.parent,
                    )
                    if not relocate_staging_partials(target_path, candidate):
                        logger.warning("Não foi possível transferir parciais para o Stage alternativo: %s", candidate.parent)
                        continue
                    logger.info("Stage sem capacidade suficiente; retomando parciais em: %s", candidate.parent)
                candidate.parent.mkdir(parents=True, exist_ok=True)
                return candidate
            except Exception as exc:
                logger.debug("Erro ao verificar Stage %s: %s", candidate.parent, exc)

        now = time.time()
        if now - last_log >= 30:
            logger.warning(
                "Discos de Stage aguardando espaço (%s): necessário=%.1f GB, reserva=%d%%",
                ", ".join(str(root) for root in roots),
                (required_bytes or 0) / 1024**3,
                min_free_percent,
            )
            last_log = now
        time.sleep(poll_seconds)


def relocate_staging_partials(source_target: Path, destination_target: Path) -> bool:
    """Copia parciais para outro Stage, mantendo os originais até conclusão."""
    source_parent = Path(source_target).parent
    stem = Path(source_target).stem
    partials = sorted({
        path
        for pattern in (f".{stem}.download*", f".{stem}.*.download*")
        for path in source_parent.glob(pattern)
    })
    if any(path.is_symlink() or not path.is_file() for path in partials):
        return False
    if not partials:
        return True

    destination_parent = Path(destination_target).parent
    destination_parent.mkdir(parents=True, exist_ok=True)

    # Limpar arquivos temporários de migrações anteriores
    for leftover in destination_parent.glob(".nebula-stage-migration-*"):
        with contextlib.suppress(OSError):
            leftover.unlink()

    pairs = [(source, destination_parent / source.name) for source in partials]
    logger.info("Migrando %d arquivos de partes para %s...", len(partials), destination_parent)

    created: list[Path] = []
    temporary: list[Path] = []
    source_stats = {}
    try:
        for source, destination in pairs:
            source_stat = source.stat()
            source_stats[source] = source_stat

            if destination.exists():
                if destination.is_symlink() or not destination.is_file():
                    destination.unlink(missing_ok=True)
                elif destination.stat().st_size == source_stat.st_size:
                    # Já existe com tamanho exato, reutiliza sem gastar I/O
                    continue
                else:
                    logger.warning("Parcial conflitante preservada: %s", destination)
                    return False

            fd, temporary_name = tempfile.mkstemp(prefix=".nebula-stage-migration-", dir=destination_parent)
            os.close(fd)
            temp_path = Path(temporary_name)
            temporary.append(temp_path)
            shutil.copy2(source, temp_path)
            current_source_stat = source.stat()
            if (
                temp_path.stat().st_size != source_stat.st_size
                or current_source_stat.st_size != source_stat.st_size
                or current_source_stat.st_mtime_ns != source_stat.st_mtime_ns
            ):
                raise OSError(f"Cópia parcial incompleta: {source.name}")
            try:
                if destination.exists():
                    if destination.is_symlink() or not destination.is_file():
                        destination.unlink(missing_ok=True)
                    elif destination.stat().st_size != source_stat.st_size:
                        temp_path.unlink(missing_ok=True)
                        temporary.remove(temp_path)
                        logger.warning("Parcial conflitante preservada: %s", destination)
                        return False
                    else:
                        temp_path.unlink(missing_ok=True)
                        temporary.remove(temp_path)
                        continue
                os.link(temp_path, destination)
            except OSError:
                temp_path.replace(destination)
            else:
                temp_path.unlink(missing_ok=True)
            temporary.remove(temp_path)
            created.append(destination)
    except Exception as exc:
        for path in temporary + created:
            with contextlib.suppress(OSError):
                path.unlink()
        logger.warning("Falha ao copiar parciais para Stage alternativo: %s", exc)
        return False

    logger.info("Migração de parciais concluída com sucesso para %s.", destination_parent)
    return True


def cleanup_staging_partials(target_path: Path, staging_dirs: list[Path] | None = None) -> None:
    """Remove parciais dos Stages só depois de existir arquivo final validado."""
    target_path = Path(target_path)
    roots = [Path(root).resolve() for root in (staging_dirs or [target_path.parent])]
    relative_target = None
    for root in roots:
        try:
            relative_target = target_path.resolve().relative_to(root)
            break
        except ValueError:
            continue
    if relative_target is None:
        roots = [target_path.parent.resolve()]
        relative_target = Path(target_path.name)

    for root in roots:
        parent = (root / relative_target).parent
        if not parent.is_dir():
            continue
        stem = Path(relative_target).stem
        partials = {
            path
            for pattern in (f".{stem}.download*", f".{stem}.*.download*")
            for path in parent.glob(pattern)
        }
        for path in partials:
            with contextlib.suppress(OSError):
                path.unlink()


def find_existing_staging_target(
    dest_root: Path,
    destination: Path,
    staging_dirs: list[Path],
) -> tuple[Path | None, bool]:
    """
    Verifica se a mídia ou suas partes já existem em algum dos diretórios de stage configurados.
    Retorna (target_stage_file, has_existing_data).
    Prioriza estágios com maior espaço livre quando múltiplos contiverem partes da mídia.
    """
    try:
        rel_dest = destination.resolve().relative_to(dest_root.resolve())
    except Exception:
        rel_dest = Path(destination.name)

    stem = destination.stem
    candidates_with_data = []

    for stage_dir in staging_dirs:
        target_cand = stage_dir / rel_dest
        parent_cand = target_cand.parent
        if not parent_cand.exists():
            continue

        # 1. Se o arquivo final já existe
        if target_cand.exists() and target_cand.stat().st_size > 0:
            return target_cand, True

        has_data = False
        tmp_base = parent_cand / f".{stem}.download"
        if tmp_base.exists() and tmp_base.stat().st_size > 0:
            has_data = True

        if not has_data:
            try:
                if any(parent_cand.glob(f".{stem}.download*")) or any(parent_cand.glob(f".{stem}.*.download*")):
                    has_data = True
            except Exception:
                pass

        if has_data:
            try:
                free_space = shutil.disk_usage(stage_dir).free
            except Exception:
                free_space = 0
            candidates_with_data.append((free_space, target_cand))

    if candidates_with_data:
        candidates_with_data.sort(key=lambda x: x[0], reverse=True)
        return candidates_with_data[0][1], True

    return None, False


def has_partial_download_on_disk(file_stem: str, staging_dirs: list[Path]) -> bool:
    """Verifica se existem partes ou arquivos de download parcial no disco de stage para esta mídia."""
    clean_stem = file_stem.lower().strip()
    for stage_dir in staging_dirs:
        if not stage_dir.exists():
            continue
        try:
            for root, _, files in os.walk(stage_dir):
                for name in files:
                    nl = name.lower()
                    if clean_stem in nl and (".download" in nl or ".part" in nl):
                        return True
        except Exception:
            continue
    return False


def get_series_key(file_path: Path) -> str:
    """Retorna uma chave única normalizada para a série/pasta da mídia."""
    parent_name = file_path.parent.name
    if re.match(r"(?i)^season\s*\d+$", parent_name) and file_path.parent.parent != file_path.parent:
        return file_path.parent.parent.name.lower()
    ep_info = episode_identity(parent_name, file_path.name)
    if ep_info:
        return ep_info[0]
    return parent_name.lower()


def get_series_display_name(file_path: Path) -> str:
    """Retorna o nome legível da série para exibição amigável no log."""
    parent_name = file_path.parent.name
    if re.match(r"(?i)^season\s*\d+$", parent_name) and file_path.parent.parent != file_path.parent:
        return file_path.parent.parent.name
    ep_info = episode_identity(parent_name, file_path.name)
    if ep_info:
        return ep_info[0].title()
    return parent_name


def format_download_error(exc: Exception) -> str:
    """Extrai informações diagnósticas detalhadas de erros de download / HTTP."""
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        headers = getattr(exc, "headers", None) or {}
        asn_allowed = headers.get("X-Debug-ASNAllowed")
        asn_org = headers.get("X-Debug-ASNOrg") or headers.get("X-Debug-ASN")
        if code == 406:
            if asn_allowed == "0" or asn_org:
                return (
                    f"HTTP Error 406: Bloqueio de Operadora/ASN pelo servidor IPTV "
                    f"({asn_org or 'TELEFONICA/VIVO'}). Requer VPN/Proxy na máquina."
                )
            return "HTTP Error 406: Not Acceptable (servidor IPTV rejeitou formato/origem da requisição)"
        if code == 403:
            return "HTTP Error 403: Forbidden (acesso negado pelo servidor IPTV)"
        if code == 401:
            return "HTTP Error 401: Unauthorized (link ou credenciais da conta IPTV expirados)"
        if code == 404:
            return "HTTP Error 404: Not Found (arquivo de vídeo não existe no servidor remoto)"
        reason = getattr(exc, "reason", "")
        return f"HTTP Error {code}: {reason}".strip()
    return str(exc)


# =====================================================================
# RASTREAMENTO E QUARENTENA DE FALHAS (.STRM QUEBRADOS / 401 / 404 / 406)
# =====================================================================

class FailureTracker:
    """
    Rastreia falhas de download (.strm com links expirados, 401, 404, 406 ou erros)
    para evitar que o programa fique em loop infinito tentando baixar a mesma
    mídia corrompida ou bloqueada sempre que for iniciado.
    """

    def __init__(self, cache_file: Path | None = None, cooldown_seconds: int = 7200):
        if cache_file is None:
            cache_file = Path.home() / ".nebula_strm_failures.json"
        self.cache_file = cache_file
        self.cooldown_seconds = cooldown_seconds
        self.failures: dict[str, dict[str, Any]] = {}
        self.series_streak: dict[str, int] = {}
        self.broken_series: set[str] = set()
        self._lock = threading.Lock()
        self._load()

    def reset_cycle(self) -> None:
        """Reseta o circuit breaker de séries para permitir reavaliação no início de cada ciclo."""
        with self._lock:
            self.series_streak.clear()
            self.broken_series.clear()

    def is_series_broken(self, file_path: Path) -> tuple[bool, str]:
        """Verifica se a série desta mídia foi pausada no ciclo atual após falhas repetidas."""
        series_k = get_series_key(file_path)
        with self._lock:
            if series_k in self.broken_series:
                display_name = get_series_display_name(file_path)
                return True, f"Série '{display_name}' pausada neste ciclo após falhas repetidas no servidor"
            return False, ""

    def _load(self) -> None:
        if self.cache_file.exists():
            try:
                with self.cache_file.open("r", encoding="utf-8") as f:
                    self.failures = json.load(f)
                # Purga automática de erros de SSL/TLS antigos que foram sanados pelo patch de IPv4/SSL
                cleaned = {
                    k: v for k, v in self.failures.items()
                    if not any(token in v.get("error", "").lower() for token in ("ssl", "tlsv1", "unrecognized_name"))
                }
                if len(cleaned) != len(self.failures):
                    self.failures = cleaned
                    self._save()
            except Exception as exc:
                logger.debug("Falha ao carregar tracker de falhas: %s", exc)
                self.failures = {}

    def _save(self) -> None:
        try:
            with self.cache_file.open("w", encoding="utf-8") as f:
                json.dump(self.failures, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.debug("Falha ao salvar tracker de falhas: %s", exc)

    def should_skip(self, file_path: Path, staging_dirs: list[Path] | None = None) -> tuple[bool, str]:
        # 1. Se a série inteira foi pausada pelo Circuit Breaker deste ciclo:
        series_broken, s_reason = self.is_series_broken(file_path)
        if series_broken:
            # Mas se esse arquivo específico já tem parte baixada no stage, SEMPRE tenta terminar!
            if staging_dirs and has_partial_download_on_disk(file_path.stem, staging_dirs):
                pass
            else:
                return True, s_reason

        # 2. Verificação individual do arquivo no cache persistido
        key = str(file_path.resolve())
        with self._lock:
            info = self.failures.get(key)
            if not info:
                return False, ""
            last_time = info.get("timestamp", 0)
            count = info.get("count", 1)
            last_error = info.get("error", "Erro desconhecido")
            now = time.time()

            # Se existirem partes parciais baixadas no stage, SEMPRE retoma na nova sessão!
            if staging_dirs and has_partial_download_on_disk(file_path.stem, staging_dirs):
                return False, ""

            # Se o erro for permanente (401, 403, 404, 406, 410)
            is_fatal = any(code in last_error for code in ("401", "403", "404", "406", "410"))
            if is_fatal:
                effective_cooldown = self.cooldown_seconds * 2
                if (now - last_time) < effective_cooldown:
                    return True, f"{last_error} (Tentativas: {count}, Cooldown ativo)"
                return False, ""

            # Para erros transitórios (interrupções inesperadas, quedas de conexão):
            # Não pula se teve menos de 3 tentativas, permitindo retomar de onde parou em outra sessão!
            if count < 3:
                return False, ""

            if (now - last_time) < self.cooldown_seconds:
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

            # Circuit breaker por série para erros fatais de servidor remoto
            # 406 can reject one URL without indicating that sibling episodes are unavailable.
            is_fatal = any(code in str(error) for code in ("401", "403", "404", "410"))
            if is_fatal:
                series_k = get_series_key(file_path)
                streak = self.series_streak.get(series_k, 0) + 1
                self.series_streak[series_k] = streak
                if streak >= 3 and series_k not in self.broken_series:
                    self.broken_series.add(series_k)
                    display_name = get_series_display_name(file_path)
                    logger.warning(
                        "Série/pasta '%s' pausada neste ciclo após 3 falhas consecutivas no servidor (%s). "
                        "Pulando demais episódios desta série para continuar o download das outras mídias.",
                        display_name,
                        str(error)[:120],
                    )

    def record_success(self, file_path: Path) -> None:
        key = str(file_path.resolve())
        series_k = get_series_key(file_path)
        with self._lock:
            if key in self.failures:
                del self.failures[key]
                self._save()
            self.series_streak.pop(series_k, None)

    def clear(self) -> None:
        with self._lock:
            self.failures.clear()
            self.series_streak.clear()
            self.broken_series.clear()
            self._save()



# =====================================================================
# VALIDAÇÃO CONTRA MONGODB (MÍDIAS CONCLUÍDAS OU ATIVAS)
# =====================================================================

def extract_stream_file_id(url: str | None) -> str | None:
    """Return a Mongo media ID embedded in a Nebula stream URL, if present."""
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        for key in ("id", "file_id", "media_id"):
            value = params.get(key, [None])[0]
            if value:
                return str(value)
        match = re.search(r"/([a-fA-F0-9]{24}|\d+)\.(?:mkv|mp4|avi|ts)$", parsed.path)
        return match.group(1) if match else None
    except Exception:
        return None


def is_object_id_parent(parent: object) -> bool:
    """True quando o ``parent`` é um ObjectId (ou sua forma hexadecimal), não um caminho."""
    if isinstance(parent, str):
        return bool(re.fullmatch(r"[a-fA-F0-9]{24}", parent))
    return parent is not None and type(parent).__name__ == "ObjectId"


def resolve_directory_path(files, parent: object, memo: dict[str, str], _depth: int = 0) -> str:
    """Reconstrói o caminho virtual de um diretório cujo ``parent`` é ObjectId."""
    key = str(parent)
    if key in memo:
        return memo[key]
    path = ""
    if _depth < 64:
        try:
            node = files.find_one(
                {"_id": ObjectId(parent) if isinstance(parent, str) else parent},
                {"name": 1, "parent": 1},
            )
        except Exception:
            node = None
        if node:
            up = node.get("parent", "")
            base = (
                resolve_directory_path(files, up, memo, _depth + 1)
                if is_object_id_parent(up)
                else str(up)
            )
            path = f"{base.rstrip('/')}/{node.get('name', '')}"
    memo[key] = path
    return path


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
        self.recovery_ids: set[str] = set()
        self.recovery_targets: set[tuple[str, str]] = set()
        self.recovery_records: list[tuple[str, str, str]] = []
        self.last_parent_lookup_available = True
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
            recovery_ids: set[str] = set()
            recovery_targets: set[tuple[str, str]] = set()
            recovery_records: list[tuple[str, str, str]] = []
            dir_path_memo: dict[str, str] = {}

            for doc in db.files.find(
                {"type": "file"},
                {
                    "parent": 1,
                    "name": 1,
                    "status": 1,
                    "local_path": 1,
                    "recovery_required": 1,
                    "failed_reason": 1,
                },
            ):
                parent_raw = doc.get("parent", "")
                name = str(doc.get("name", ""))
                status = doc.get("status")
                local_path = doc.get("local_path")
                is_recovery = (
                    status in ACTIVE_STATUSES
                    and doc.get("recovery_required") is True
                    and doc.get("failed_reason") == "local_path_missing"
                )
                if is_recovery and is_object_id_parent(parent_raw):
                    # Pai armazenado como ObjectId: reconstrói o caminho virtual.
                    parent_raw = resolve_directory_path(db.files, parent_raw, dir_path_memo)
                parent = str(parent_raw).casefold()

                if not name:
                    continue

                name_lower = name.casefold()
                stem_lower = Path(name).stem.casefold()

                if is_recovery:
                    recovery_ids.add(str(doc.get("_id", "")))
                    recovery_targets.add((parent, name_lower))
                    recovery_targets.add((parent, stem_lower))
                    recovery_records.append((parent, name, str(doc.get("_id", ""))))
                    continue

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
            self.recovery_ids = recovery_ids
            self.recovery_targets = recovery_targets
            self.recovery_records = recovery_records
            self.last_cache_time = now
            client.close()
            logger.debug(
                "Índice Mongo atualizado: %d filmes concl., %d eps concl., %d ativos.",
                len(movies), len(episodes), len(active)
            )
        except Exception as exc:
            logger.warning("Falha ao atualizar índice Mongo: %s", exc)

    def canonical_parent_for_stream(self, url: str) -> str | None:
        """Fetch the current persisted parent for a completed or recovery stream ID."""
        file_id = extract_stream_file_id(url)
        if not file_id or not self.mongo_uri or "mongodb://test" in self.mongo_uri:
            self.last_parent_lookup_available = False
            return None

        client = None
        try:
            client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=3000)
            files = client[self.db_name].files
            eligible_status = {
                "$or": [
                    {"status": "completed"},
                    {
                        "status": {"$in": list(ACTIVE_STATUSES)},
                        "recovery_required": True,
                        "failed_reason": "local_path_missing",
                    },
                ]
            }
            candidates: list[Any] = []
            with contextlib.suppress(Exception):
                candidates.append(ObjectId(file_id))
            candidates.append(file_id)
            for candidate_id in candidates:
                doc = files.find_one(
                    {"_id": candidate_id, "type": "file", **eligible_status},
                    {"parent": 1},
                )
                if doc:
                    self.last_parent_lookup_available = True
                    return str(doc.get("parent", "")) or None
            self.last_parent_lookup_available = True
            return None
        except Exception as exc:
            self.last_parent_lookup_available = False
            logger.warning("Consulta do parent Mongo falhou; categoria do STRM não será alterada: %s", exc)
            return None
        finally:
            if client is not None:
                client.close()

    def is_recovery_candidate(
        self,
        strm_path: Path,
        url: str | None = None,
        destination: Path | None = None,
        dest_root: Path | None = None,
    ) -> bool:
        """Match a STRM to an active Mongo record whose local source is missing."""
        self.refresh_cache()
        file_id = extract_stream_file_id(url) if url else None
        if file_id and file_id in self.recovery_ids:
            return True

        if destination and dest_root:
            parent = mongo_parent_for(dest_root, destination.parent, self.library_user).casefold()
            if (parent, destination.name.casefold()) in self.recovery_targets:
                return True
            if (parent, destination.stem.casefold()) in self.recovery_targets:
                return True
        return False

    def find_recovery_strm_items(
        self,
        sources: list[Path],
        dest_root: Path,
    ) -> list[tuple[Path, Path, str, int]]:
        """Locate recovery STRMs directly from their persisted Mongo destinations."""
        self.refresh_cache()
        found: list[tuple[Path, Path, str, int]] = []
        seen: set[str] = set()
        unresolved: list[tuple[str, str, str]] = []
        for parent, name, file_id in self.recovery_records:
            parent_parts = [part for part in parent.split("/") if part]
            if parent_parts and parent_parts[0].casefold() == self.library_user.casefold():
                parent_parts = parent_parts[1:]
            if not parent_parts:
                continue

            strm_name = f"{Path(name).stem}.strm"
            found_direct = False
            for source in sources:
                candidate = source.joinpath(*parent_parts, strm_name)
                try:
                    resolved = str(candidate.resolve()).casefold()
                    if resolved in seen or not candidate.is_file():
                        continue
                    try:
                        linked_id = extract_stream_file_id(read_strm_url(candidate))
                    except (OSError, UnicodeError, ValueError):
                        continue
                    # Só links do próprio Nebula carregam um ObjectId Mongo; IDs numéricos
                    # de provedores externos (IPTV) não são comparáveis ao registro.
                    if (
                        linked_id
                        and file_id
                        and re.fullmatch(r"[a-fA-F0-9]{24}", linked_id)
                        and linked_id.casefold() != file_id.casefold()
                    ):
                        logger.warning(
                            "STRM de recuperação ignorado: ID do link não corresponde ao registro Mongo (%s)",
                            candidate,
                        )
                        continue
                    media_destination = destination_for(
                        source,
                        dest_root,
                        candidate.with_suffix(Path(name).suffix or ".mkv"),
                    )
                    if not self.is_recovery_candidate(
                        candidate,
                        destination=media_destination,
                        dest_root=dest_root,
                    ):
                        continue
                except OSError:
                    continue

                seen.add(resolved)
                found.append((
                    source,
                    candidate,
                    get_category_from_path(candidate, source),
                    extract_media_year(candidate.name, candidate.parent.name),
                ))
                found_direct = True
                break
            if not found_direct:
                unresolved.append((parent, name, file_id))

        misplaced: dict[str, tuple[Path, Path]] = {}
        unresolved_ids = {file_id.casefold() for _, _, file_id in unresolved if file_id}
        if unresolved_ids:
            for source in sources:
                for root, _, filenames in os.walk(source):
                    for filename in filenames:
                        if Path(filename).suffix.casefold() != ".strm":
                            continue
                        candidate = Path(root) / filename
                        try:
                            stream_id = extract_stream_file_id(read_strm_url(candidate))
                        except (OSError, UnicodeError, ValueError):
                            continue
                        if stream_id and stream_id.casefold() in unresolved_ids:
                            misplaced.setdefault(stream_id.casefold(), (source, candidate))

        for parent, name, file_id in unresolved:
            match = misplaced.get(file_id.casefold())
            if not match:
                continue
            source, candidate = match
            seen_key = str(candidate.resolve()).casefold()
            if seen_key in seen:
                continue
            seen.add(seen_key)
            found.append((
                source,
                candidate,
                get_category_from_path(candidate, source),
                extract_media_year(name, candidate.parent.name),
            ))
        return sort_strm_batch(found)

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

        if self.is_recovery_candidate(strm_path, url, destination, dest_root):
            return False, "STRM prioritário: registro Mongo requer recuperação do arquivo local"

        # 1. Verificação por ID contido na URL do stream
        if url:
            try:
                file_id_str = extract_stream_file_id(url)

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
    staging_dirs: list[Path] | None = None,
) -> Path:
    """
    Realiza o download de uma URL com suporte a Range requests em múltiplas partes,
    com retomada (resume) de partes baixadas e retentativas automáticas.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.parent / f".{target_path.stem}.download"
    meta_path = Path(f"{tmp_path}.meta")
    part_paths: list[Path] = []

    socket.setdefaulttimeout(read_timeout)

    try:
        probe = Request(url, headers={"Range": "bytes=0-0", "User-Agent": "VLC/3.0.21 LibVLC/3.0.21"})
        with urlopen(probe, timeout=read_timeout) as response:
            content_range = response.headers.get("Content-Range", "")
            match = re.match(r"bytes\s+0-0/(\d+)", content_range)

            if not match:
                # Checar se já existe no destino
                if target_path.exists() and target_path.stat().st_size > 0:
                    return target_path
                # Servidor não suporta Range: download sequencial direto
                logger.info("Download direto sem suporte a Range: %s", target_path.name)
                with tmp_path.open("wb") as out:
                    shutil.copyfileobj(response, out, length=1024 * 1024)
                total = tmp_path.stat().st_size
            else:
                total = int(match.group(1))

                # Checar se já existe no destino completo
                if target_path.exists():
                    if target_path.stat().st_size == total:
                        cleanup_staging_partials(target_path, staging_dirs)
                        return target_path
                    logger.warning(
                        "Arquivo de destino com tamanho divergente (%d != %d). Reiniciando: %s",
                        target_path.stat().st_size, total, target_path.name,
                    )
                    target_path.unlink(missing_ok=True)

                # Checar se o arquivo já foi completamente unido antes de uma parada inesperada
                if tmp_path.exists() and tmp_path.stat().st_size == total:
                    logger.info("Arquivo unido já existente completo (%d bytes): %s", total, target_path.name)
                    tmp_path.replace(target_path)
                    cleanup_staging_partials(target_path, staging_dirs)
                    return target_path

                target_path = wait_for_disk_capacity(
                    target_path,
                    total,
                    min_free_percent,
                    staging_dirs=staging_dirs,
                ) or target_path
                tmp_path = target_path.parent / f".{target_path.stem}.download"
                meta_path = Path(f"{tmp_path}.meta")

                # Recupera parts_count original caso salvo em sessão anterior
                if meta_path.exists():
                    try:
                        with meta_path.open("r", encoding="utf-8") as f:
                            saved_meta = json.load(f)
                        if saved_meta.get("total") == total and saved_meta.get("parts_count"):
                            parts_count = int(saved_meta["parts_count"])
                    except Exception:
                        pass

                parts_count = min(max(parts_count, 1), 32)
                part_size = (total + parts_count - 1) // parts_count
                part_paths = [Path(f"{tmp_path}.part{index}") for index in range(parts_count)]

                try:
                    with meta_path.open("w", encoding="utf-8") as f:
                        json.dump({"total": total, "parts_count": parts_count, "part_size": part_size}, f)
                except Exception:
                    pass

                progress_lock = threading.Lock()
                progress = {"downloaded": 0, "next_percent": 1, "last_logged_bytes": 0, "start_time": time.time()}

                # Verificar partes válidas existentes e temporários parciais para Resume
                for index, p_path in enumerate(part_paths):
                    start = index * part_size
                    end = min(start + part_size, total) - 1
                    expected = end - start + 1
                    if p_path.exists():
                        if p_path.stat().st_size == expected:
                            progress["downloaded"] += expected
                        else:
                            p_path.unlink(missing_ok=True)
                    else:
                        p_tmp = Path(f"{p_path}.tmp")
                        if p_tmp.exists():
                            if p_tmp.stat().st_size == expected:
                                p_tmp.replace(p_path)
                                progress["downloaded"] += expected
                            elif 0 < p_tmp.stat().st_size < expected:
                                progress["downloaded"] += p_tmp.stat().st_size
                            else:
                                p_tmp.unlink(missing_ok=True)

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

                    part_tmp = Path(f"{part_file}.tmp")
                    last_error = None
                    for attempt in range(1, max_retries + 1):
                        attempt_downloaded = 0
                        try:
                            existing = part_tmp.stat().st_size if part_tmp.exists() else 0
                            if existing == expected:
                                part_tmp.replace(part_file)
                                return
                            if existing > expected:
                                part_tmp.unlink(missing_ok=True)
                                existing = 0

                            req_start = start + existing
                            req = Request(
                                url,
                                headers={"Range": f"bytes={req_start}-{end}", "User-Agent": "VLC/3.0.21 LibVLC/3.0.21"},
                            )
                            with urlopen(req, timeout=read_timeout) as part_resp:
                                status = getattr(part_resp, "status", None)
                                if status and status not in (200, 206):
                                    raise IOError(f"HTTP status {status} na parte {index + 1}")
                                if req_start > 0 and status == 200:
                                    with progress_lock:
                                        progress["downloaded"] = max(0, progress["downloaded"] - existing)
                                    existing = 0
                                    mode = "wb"
                                else:
                                    mode = "ab" if existing > 0 else "wb"

                                with part_tmp.open(mode) as out:
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
                            if part_tmp.exists() and part_tmp.stat().st_size > expected:
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

                if tmp_path.stat().st_size != total:
                    raise IOError(f"Arquivo final com tamanho incorreto ({tmp_path.stat().st_size} != {total})")

                # Deleta partes e meta apenas APÓS o arquivo final estar 100% íntegro
                for part_path in part_paths:
                    part_path.unlink(missing_ok=True)
                    Path(f"{part_path}.tmp").unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)

        tmp_path.replace(target_path)
        cleanup_staging_partials(target_path, staging_dirs)
        return target_path

    except Exception:
        # Limpar apenas arquivo final parcial (.download), PRESERVANDO as partes já baixadas (.part)
        # e fragmentos (.tmp) válidos para que a retomada funcione na próxima sessão ou retentativa!
        with contextlib.suppress(Exception):
            tmp_path.unlink(missing_ok=True)
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
    recovery_file_id: str | None = None,
    priority: int = 0,
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

        recovery_filter = {
            "type": "file",
            "status": {"$in": list(ACTIVE_STATUSES)},
            "recovery_required": True,
            "failed_reason": "local_path_missing",
        }
        recovery_doc = None
        if recovery_file_id:
            id_candidates = []
            try:
                id_candidates.append(ObjectId(recovery_file_id))
            except Exception:
                pass
            id_candidates.append(recovery_file_id)
            for candidate_id in id_candidates:
                recovery_doc = files.find_one(
                    {"_id": candidate_id, **recovery_filter},
                    {"_id": 1},
                )
                if recovery_doc:
                    break
        if not recovery_doc:
            recovery_doc = files.find_one(
                {
                    **recovery_filter,
                    "parent": parent_path,
                    "name": destination.name,
                },
                {"_id": 1},
            )

        update_values = {
            "type": "file",
            "name": destination.name,
            "parent": parent_path,
            "size": size,
            "status": "queued",
            "local_path": str(downloaded_file),
            "mtime": now,
            "delete_source": bool(delete_source),
            "priority": int(priority),
            "search_name": destination.name.strip().casefold(),
            "search_parent": parent_path.strip().casefold(),
        }

        if recovery_doc:
            result = files.update_one(
                {
                    "_id": recovery_doc["_id"],
                    **recovery_filter,
                },
                {
                    "$set": {
                        **update_values,
                        "recovery_required": False,
                        "failed_reason": None,
                    },
                    "$unset": {"failed_at": ""},
                },
            )
            if getattr(result, "matched_count", 1) == 0:
                logger.warning(
                    "Recuperação mudou durante o download; STRM preservado e fila não sobrescrita: %s",
                    destination.name,
                )
                return False
            logger.info(
                "Registro de recuperação reativado sem zerar partes/progresso: %s",
                destination.name,
            )
            client.close()
            return True

        # 1. Verifica se já existe por local_path
        existing = files.find_one({"local_path": str(downloaded_file)}, {"_id": 1, "status": 1})
        if existing:
            files.update_one(
                {"_id": existing["_id"]},
                {"$set": update_values},
            )
        else:
            existing_target = files.find_one(
                {"parent": parent_path, "name": destination.name},
                {"_id": 1},
            )
            if existing_target:
                logger.warning(
                    "Destino Mongo já pertence a outro registro; preservando arquivo e STRM: %s -> %s",
                    parent_path,
                    destination.name,
                )
                client.close()
                return False

            doc = {
                **update_values,
                "ctime": now,
                "parts": [],
            }
            files.update_one(
                {"parent": parent_path, "name": destination.name},
                {"$setOnInsert": doc},
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

    # 1. Animações: Séries/Episódios por temporada/episódio, filmes e avulsos A-Z
    def animacoes_sort_key(it: tuple[Path, Path, str, int]):
        src_path = it[1]
        ep_info = episode_identity(src_path.parent.name, src_path.name)
        if ep_info:
            return (0, ep_info[0].lower(), ep_info[1], ep_info[2])
        return (1, normalize_media_title(src_path.name), 0, 0)

    categorized["animacoes"].sort(key=animacoes_sort_key)

    # 2. Ordenação de Filmes: Alfabética A-Z (desempate por ano decrescente se mesmo título)
    categorized["filmes"].sort(
        key=lambda it: (normalize_media_title(it[1].name), -it[3])
    )

    # Função auxiliar para ordenação de séries/doramas/novelas por temporada e episódio
    def series_sort_key(it: tuple[Path, Path, str, int]):
        src_path = it[1]
        ep_info = episode_identity(src_path.parent.name, src_path.name)
        if ep_info:
            return (ep_info[0].lower(), ep_info[1], ep_info[2])
        return (normalize_media_title(src_path.parent.name), normalize_media_title(src_path.name), 0)

    # 3. Séries por nome da pasta/série, temporada e episódio
    categorized["series"].sort(key=series_sort_key)

    # 4. Doramas por nome da pasta/série, temporada e episódio
    categorized["doramas"].sort(key=series_sort_key)

    # 5. Novelas por nome da pasta/série, temporada e episódio
    categorized["novelas"].sort(key=series_sort_key)

    # 6. Porno ordenado estritamente A-Z
    categorized["porno"].sort(key=lambda it: normalize_media_title(it[1].name))

    # Outros ordenados A-Z
    categorized["other"].sort(key=lambda it: normalize_media_title(it[1].name))

    # Monta a fila combinada por prioridade
    results: list[tuple[Path, Path, str, int]] = []
    for cat in CATEGORY_PRIORITY:
        results.extend(categorized[cat])
    results.extend(categorized["other"])

    priority_config = load_m3u_priority_config()
    results.sort(key=lambda item: not is_priority_item(item[0], item[1], priority_config))
    return results


def sort_strm_batch(
    items: list[tuple[Path, Path, str, int]],
) -> list[tuple[Path, Path, str, int]]:
    """Apply the existing category/year order to a bounded scan batch."""
    categorized: dict[str, list[tuple[Path, Path, str, int]]] = {
        cat: [] for cat in CATEGORY_PRIORITY
    }
    categorized["other"] = []
    for item in items:
        category = item[2]
        categorized[category if category in categorized else "other"].append(item)

    def episode_sort_key(item: tuple[Path, Path, str, int]):
        src_path = item[1]
        ep_info = episode_identity(src_path.parent.name, src_path.name)
        if ep_info:
            return (ep_info[0].lower(), ep_info[1], ep_info[2])
        return (src_path.parent.name.lower(), src_path.name.lower(), 0)

    categorized["animacoes"].sort(
        key=lambda item: (
            (0, ep_info[0].lower(), ep_info[1], ep_info[2])
            if (ep_info := episode_identity(item[1].parent.name, item[1].name))
            else (1, normalize_media_title(item[1].name), 0, 0)
        )
    )
    categorized["filmes"].sort(key=lambda item: (normalize_media_title(item[1].name), -item[3]))
    for category in ("series", "doramas", "novelas"):
        categorized[category].sort(key=episode_sort_key)
    categorized["porno"].sort(key=lambda item: normalize_media_title(item[1].name))
    categorized["other"].sort(key=lambda item: normalize_media_title(item[1].name))

    ordered = [item for category in CATEGORY_PRIORITY for item in categorized[category]] + categorized["other"]
    priority_config = load_m3u_priority_config()
    ordered.sort(key=lambda item: not is_priority_item(item[0], item[1], priority_config))
    return ordered


def iter_strm_file_batches_prioritized(
    sources: list[Path],
    exclude_dirs: set[str] | None = None,
    *,
    batch_size: int = 250,
    progress_callback=None,
    progress_interval: float = 10.0,
):
    """Yield sorted bounded batches so a large library need not be fully indexed first.

    Category and year priority is preserved within each batch. This lets the
    downloader start work promptly while the remaining library is still being
    scanned.
    """
    if batch_size < 1:
        raise ValueError("batch_size deve ser positivo")

    exclude = {directory.lower() for directory in (exclude_dirs or set())}
    batch: list[tuple[Path, Path, str, int]] = []
    directories_seen = 0
    files_seen = 0
    candidates_seen = 0
    next_progress = time.monotonic() + progress_interval

    for source in sources:
        if not source.exists():
            continue
        for root, dirs, files in os.walk(source):
            directories_seen += 1
            root_path = Path(root)
            try:
                rel_parts = root_path.relative_to(source).parts
                if rel_parts and rel_parts[0].lower() in exclude:
                    dirs[:] = []
                    continue
            except ValueError:
                pass

            if root_path == source:
                dirs.sort(key=lambda d: (category_dir_priority(d), normalize_media_title(d)))
            else:
                dirs.sort(key=normalize_media_title)

            files.sort(key=normalize_media_title)

            for name in files:
                files_seen += 1
                ext = Path(name).suffix.lower()
                if ext in MONITORED_EXTENSIONS and not is_incomplete_filename(name):
                    media_path = root_path / name
                    category = get_category_from_path(media_path, source)
                    year = extract_media_year(media_path.name, media_path.parent.name)
                    batch.append((source, media_path, category, year))
                    candidates_seen += 1

                    if len(batch) >= batch_size:
                        yield sort_strm_batch(batch)
                        batch = []

                now = time.monotonic()
                if progress_callback and now >= next_progress:
                    progress_callback(directories_seen, files_seen, candidates_seen)
                    next_progress = now + progress_interval

    if batch:
        yield sort_strm_batch(batch)


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
                        if validator.is_recovery_candidate(media_path, url, dest_file, dest_root):
                            continue
                        is_dupe, reason = validator.is_already_completed_or_active(media_path, url, dest_file, dest_root)
                    else:
                        dest_file = destination_for(source, dest_root, media_path)
                        is_dupe, reason = validator.is_already_completed_or_active(media_path, "", dest_file, dest_root)
                    if is_dupe and "ativa/em fila" not in reason:
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
        priority = int(is_priority_item(source_root, strm_path))
        priority_kwargs = {"priority": priority} if priority else {}

        if is_strm:
            try:
                url = read_strm_url(strm_path)
            except Exception as exc:
                logger.warning("Falha ao ler URL do .strm %s: %s", strm_path, exc)
                if failure_tracker:
                    failure_tracker.record_failure(strm_path, f"Erro de leitura: {exc}")
                return False

            canonical_parent = validator.canonical_parent_for_stream(url)
            if extract_stream_file_id(url) and not validator.last_parent_lookup_available:
                logger.warning("Mongo indisponível; download adiado para não usar categoria possivelmente errada: %s", strm_path)
                return False
            if canonical_parent:
                corrected_path = relocate_strm_to_mongo_parent(
                    strm_path,
                    source_root,
                    canonical_parent,
                    library_user,
                    dry_run=dry_run,
                )
                if corrected_path is None:
                    return False
                if corrected_path != strm_path:
                    strm_path = corrected_path
                    category = get_category_from_path(strm_path, source_root)
                    year = extract_media_year(strm_path.name, strm_path.parent.name)

            media_ext = guess_media_extension(url)
            media_source = strm_path.with_suffix(media_ext)
            destination = destination_for(source_root, dest_root, media_source)
            is_recovery = validator.is_recovery_candidate(
                strm_path,
                url,
                destination,
                dest_root,
            )

            # 1. Validação de Mídia Concluída ou Ativa
            is_dupe, reason = validator.is_already_completed_or_active(
                strm_path=strm_path,
                url=url,
                destination=destination,
                dest_root=dest_root,
            )
            if is_dupe:
                if "ativa/em fila" in reason:
                    logger.info(
                        "Mídia já ativa no Nebula (%s); preservando .strm para nova recuperação se necessário: %s",
                        reason,
                        strm_path.name,
                    )
                else:
                    logger.info("Mídia já concluída no Nebula (%s). Removendo .strm: %s", reason, strm_path.name)
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

            existing_stage_target, has_existing = find_existing_staging_target(
                dest_root=dest_root,
                destination=destination,
                staging_dirs=staging_dirs,
            )
            if existing_stage_target and has_existing:
                target_stage_file = existing_stage_target
                logger.info("Retomando download/mídia existente no Stage: %s", target_stage_file.parent)
            else:
                best_stage = get_best_staging_root(staging_dirs, remote_size, min_free_percent)
                try:
                    rel_dest = destination.relative_to(dest_root)
                    target_stage_file = best_stage / rel_dest
                except Exception:
                    target_stage_file = best_stage / destination.name
            target_stage_file.parent.mkdir(parents=True, exist_ok=True)

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
                    staging_dirs=staging_dirs,
                )
            except Exception as exc:
                err_msg = format_download_error(exc)
                logger.error("Erro no download de %s: %s", strm_path.name, err_msg)
                if failure_tracker:
                    failure_tracker.record_failure(strm_path, err_msg)
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
                recovery_file_id=extract_stream_file_id(url) if is_recovery else None,
                **priority_kwargs,
            )

            # Keep a recovery source link until its re-upload completes in Telegram.
            if registered:
                if is_recovery:
                    logger.info(
                        "STRM preservado até o upload recuperado concluir: %s",
                        strm_path.name,
                    )
                    validator.refresh_cache(force=True)
                else:
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
            existing_stage_target, has_existing = find_existing_staging_target(
                dest_root=dest_root,
                destination=destination,
                staging_dirs=staging_dirs,
            )
            if existing_stage_target and has_existing:
                target_stage_file = existing_stage_target
            else:
                best_stage = get_best_staging_root(staging_dirs, file_size, min_free_percent)
                try:
                    rel_dest = destination.relative_to(dest_root)
                    target_stage_file = best_stage / rel_dest
                except Exception:
                    target_stage_file = best_stage / destination.name
            target_stage_file.parent.mkdir(parents=True, exist_ok=True)

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

            target_stage_file = wait_for_disk_capacity(
                target_stage_file,
                file_size,
                min_free_percent,
                staging_dirs=staging_dirs,
            ) or target_stage_file
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
                **priority_kwargs,
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
            if failure_tracker:
                failure_tracker.reset_cycle()
            validator.refresh_cache(force=True)
            # Prioriza mídias que já tenham partes baixadas em stage para que continuem e terminem imediatamente
            def item_has_partial(it):
                _, m_path, _, _ = it
                return not has_partial_download_on_disk(m_path.stem, staging_dirs)

            def item_needs_recovery(it):
                source_root, media_path, _, _ = it
                if media_path.suffix.lower() != ".strm":
                    return False
                try:
                    url = read_strm_url(media_path)
                    media_ext = guess_media_extension(url)
                    destination = destination_for(
                        source_root,
                        dest_root,
                        media_path.with_suffix(media_ext),
                    )
                except Exception:
                    return False
                return validator.is_recovery_candidate(
                    media_path,
                    url,
                    destination,
                    dest_root,
                )

            logger.info(
                "Escaneando .strm em lotes de até 250; a primeira mídia elegível "
                "será processada antes de terminar a varredura da biblioteca."
            )
            scanned_candidates = 0
            batch_number = 0
            processed_any = False
            handled_recovery_paths: set[str] = set()
            priority_config = load_m3u_priority_config()

            # Use Mongo's persisted destinations to find known recovery STRMs
            # directly, so they are retried before ordinary library candidates.
            recovery_items = validator.find_recovery_strm_items(
                sources,
                dest_root,
            )
            if recovery_items:
                recovery_items.sort(key=item_has_partial)
                logger.info(
                    "Prioridade de recuperação: %d STRM(s) com arquivo local ausente serão processados primeiro.",
                    len(recovery_items),
                )
            for recovery_item in recovery_items:
                source_root, strm_path, category, year = recovery_item
                handled_recovery_paths.add(str(strm_path.resolve()).casefold())
                stats.scanned += 1

                should_skip, skip_reason = failure_tracker.should_skip(strm_path, staging_dirs)
                if should_skip:
                    logger.info("Recuperação adiada por proteção de falha/retry: %s (%s)", strm_path.name, skip_reason)
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
                    time.sleep(1)
                else:
                    stats.failed += 1

            def log_scan_progress(directories, files, candidates):
                logger.info(
                    "Varredura em andamento: pastas=%d arquivos_verificados=%d "
                    "candidatos=%d lotes_processados=%d",
                    directories,
                    files,
                    candidates,
                    batch_number,
                )

            for items in iter_strm_file_batches_prioritized(
                sources,
                exclude_dirs,
                batch_size=250,
                progress_callback=log_scan_progress,
            ):
                batch_number += 1
                scanned_candidates += len(items)
                items.sort(key=lambda item: (
                    not item_needs_recovery(item),
                    item_has_partial(item),
                    not is_priority_item(item[0], item[1], priority_config),
                ))
                logger.info(
                    "Lote %d disponível (%d mídias; %d encontradas até agora); "
                    "iniciando processamento.",
                    batch_number,
                    len(items),
                    scanned_candidates,
                )

                for source_root, strm_path, category, year in items:
                    strm_key = str(strm_path.resolve()).casefold()
                    if strm_key in handled_recovery_paths:
                        continue
                    was_recovery = item_needs_recovery(
                        (source_root, strm_path, category, year)
                    )
                    stats.scanned += 1

                    # Checagem contra loop de mídias que falharam recentemente (401/404/erros)
                    should_skip, skip_reason = failure_tracker.should_skip(strm_path, staging_dirs)
                    if should_skip:
                        logger.info("Pulando mídia com falha recente (quarentena ativa): %s (%s)", strm_path.name, skip_reason)
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
                    if was_recovery:
                        handled_recovery_paths.add(strm_key)

            logger.info("Varredura concluída: %d arquivos elegíveis encontrados.", scanned_candidates)

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
