"""
m3u_strm_generator.py
~~~~~~~~~~~~~~~~~~~~~
Fetches an IPTV M3U playlist, creates/refreshes .strm files in a media root,
and optionally fixes MongoDB recovery_required items.

STRM files are plain-text files containing a single stream URL.
They are played directly by Jellyfin/VLC — no download is needed.

Usage (standalone):
    python -m tools.m3u_strm_generator --url <m3u_url> --dest D:\\midias

Public API (used from gui.pyw):
    run_m3u_sync(m3u_url, dest_root, mongo_uri, db_name, *, fix_recovery) -> dict
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category mapping: M3U group-title → D:\midias subfolder
# ---------------------------------------------------------------------------

# Exact matches (normalised to upper before lookup)
_GROUP_EXACT: dict[str, str] = {
    "ANIMES": "Animações",
    "ANIME": "Animações",
    "ANIMAÇÕES": "Animações",
    "ANIMACOES": "Animações",
    "DORAMAS": "Doramas",
    "DORAMA": "Doramas",
    "NOVELAS": "Novelas",
    "NOVELA": "Novelas",
}

_GROUP_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("DORAMA", "K-DRAMA", "KDRAMA", "DRAMA COREANO"), "Doramas"),
    (("NOVELA", "TELENOVELA", "SOAP"), "Novelas"),
    (("ANIME", "ANIMES", "ANIMA", "DESENHO", "CARTOON", "INFANTIL"), "Animações"),
    (("SERIE", "SERIES", "SÉRIE", "SÉRIES", "SERIADO", "TV SHOW"), "Series"),
    (("FILME", "FILMES", "MOVIE", "MOVIES", "CINEMA"), "Filmes"),
)

# Prefix matches (normalised to upper, checked in order)
_GROUP_PREFIX: list[tuple[str, str]] = [
    ("FILMES", "Filmes"),
    ("FILME", "Filmes"),
    ("MOVIES", "Filmes"),
    ("MOVIE", "Filmes"),
    ("SERIES", "Series"),
    ("SERIE", "Series"),
    ("SÉRIES", "Series"),
]

# Platform names that map to Series when the URL path contains /series/
_PLATFORM_SERIES: frozenset[str] = frozenset({
    "NETFLIX", "HBO MAX", "HBO", "DISNEY+", "DISNEY PLUS", "APPLE TV+",
    "APPLE TV PLUS", "PRIME VIDEO", "AMAZON PRIME", "PARAMOUNT+",
    "PARAMOUNT PLUS", "GLOBOPLAY", "HULU", "PEACOCK", "MAX",
    "STAR+", "STAR PLUS", "LIONSGATE+", "MGMPLUS",
})

# Default fallback for /series/ URLs with unknown group-title
_DEFAULT_SERIES = "Series"
_DEFAULT_MOVIE = "Filmes"


def normalize_m3u_url(value: str) -> str:
    """Aceita URL copiada como ``@url:`...``` sem manter o prefixo."""
    value = value.strip()
    if value.startswith("@url:"):
        value = value[5:].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "`\"'":
        value = value[1:-1].strip()
    return value


def redact_url(value: str) -> str:
    """Remove credenciais da URL antes de escrevê-la em logs."""
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        secret_keys = {"username", "password", "pass", "token", "api_key", "apikey", "key"}
        safe_query = "&".join(
            f"{urllib.parse.quote(key, safe='')}="
            f"{urllib.parse.quote('[REDACTED]' if key.casefold() in secret_keys else item, safe='[]')}"
            for key, item in query
        )
        return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, safe_query, parsed.fragment))
    except ValueError:
        return "<URL inválida>"


def group_to_category(group_title: str, url: str = "", media_name: str = "") -> str | None:
    """
    Map M3U group-title to a D:\\midias category folder name.

    Returns None for live-TV entries that should be skipped.
    """
    norm = unicodedata.normalize("NFKD", group_title.strip()).encode("ascii", "ignore").decode().upper()

    # --- Exact match ---
    if norm in _GROUP_EXACT:
        return _GROUP_EXACT[norm]

    # --- Prefix match ---
    for prefix, folder in _GROUP_PREFIX:
        prefix_norm = unicodedata.normalize("NFKD", prefix).encode("ascii", "ignore").decode()
        if norm.startswith(prefix_norm):
            return folder

    for keywords, folder in _GROUP_KEYWORDS:
        if any(keyword in norm for keyword in keywords):
            return folder

    name_norm = unicodedata.normalize("NFKD", media_name).encode("ascii", "ignore").decode().upper()
    for keywords, folder in _GROUP_KEYWORDS:
        if any(re.search(rf"\b{re.escape(keyword)}\b", name_norm) for keyword in keywords):
            return folder

    # --- Platform → infer from URL ---
    if norm in _PLATFORM_SERIES:
        lower_url = url.lower()
        if "/series/" in lower_url:
            return "Series"
        if "/movie/" in lower_url:
            return "Filmes"
        # live TV under a known platform name — skip
        return None

    # --- Fallback: infer from URL structure ---
    lower_url = url.lower()
    if "/series/" in lower_url:
        return _DEFAULT_SERIES
    if "/movie/" in lower_url:
        return _DEFAULT_MOVIE

    # Live TV or unknown — skip
    return None


# ---------------------------------------------------------------------------
# M3U entry
# ---------------------------------------------------------------------------

@dataclass
class M3UEntry:
    url: str
    tvg_name: str
    group_title: str
    media_type: str  # "movie" | "series" | "live"
    tvg_id: str = ""
    extra: dict = field(default_factory=dict)


def _detect_media_type(url: str) -> str:
    lower = url.lower()
    parsed = urllib.parse.urlparse(lower)
    path = parsed.path
    if "/series/" in path:
        return "series"
    if "/movie/" in path:
        return "movie"
    return "live"


# ---------------------------------------------------------------------------
# M3U parser (streaming, line-by-line)
# ---------------------------------------------------------------------------

_EXTINF_ATTRS = re.compile(
    r'([\w-]+)\s*=\s*(?:"([^"]*?)"|\'([^\']*?)\'|(\S+))'
)


def _parse_extinf_attrs(extinf_line: str) -> dict[str, str]:
    return {
        m.group(1): next(
            (value for value in (m.group(2), m.group(3), m.group(4)) if value is not None),
            "",
        )
        for m in _EXTINF_ATTRS.finditer(extinf_line)
    }


def _decode_line(raw: bytes) -> str:
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_m3u(url: str, timeout: int = 60) -> Generator[M3UEntry, None, None]:
    """
    Stream-parse an M3U playlist URL and yield M3UEntry for movie/series entries.
    Live-TV entries (no /movie/ or /series/ in URL) are silently skipped.
    """
    url = normalize_m3u_url(url)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "VLC/3.0.20 LibVLC/3.0.20",
            "Accept": "application/vnd.apple.mpegurl,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        pending_extinf: str | None = None
        pending_attrs: dict[str, str] = {}

        for raw_line in resp:
            line = _decode_line(raw_line).rstrip("\r\n")
            if not line:
                continue

            if line.startswith("#EXTM3U"):
                continue

            if line.startswith("#EXTINF"):
                pending_extinf = line
                pending_attrs = _parse_extinf_attrs(line)
                continue

            if line.startswith("#"):
                continue

            # This is the URL line following an #EXTINF
            stream_url = line.strip()
            if not stream_url:
                pending_extinf = None
                pending_attrs = {}
                continue

            media_type = _detect_media_type(stream_url)
            if media_type == "live":
                pending_extinf = None
                pending_attrs = {}
                continue

            tvg_name = pending_attrs.get("tvg-name", "").strip()
            if not tvg_name:
                # Try to extract display name from comma after #EXTINF:-1
                if pending_extinf:
                    comma_pos = pending_extinf.rfind(",")
                    if comma_pos >= 0:
                        tvg_name = pending_extinf[comma_pos + 1:].strip()

            group_title = pending_attrs.get("group-title", "").strip()
            tvg_id = pending_attrs.get("tvg-id", "").strip()

            if not tvg_name:
                tvg_name = os.path.splitext(os.path.basename(
                    urllib.parse.urlparse(stream_url).path
                ))[0]

            yield M3UEntry(
                url=stream_url,
                tvg_name=tvg_name,
                group_title=group_title,
                media_type=media_type,
                tvg_id=tvg_id,
                extra=dict(pending_attrs),
            )

            pending_extinf = None
            pending_attrs = {}


# ---------------------------------------------------------------------------
# Title / path parsing
# ---------------------------------------------------------------------------

_SERIES_PATTERN = re.compile(
    r"^(?P<title>.+?)\s+[Ss](?P<season>\d{1,2})\s*[Ee](?P<ep>\d{1,3})\b",
    re.UNICODE,
)

# Match trailing quality/format tags like [L], [FHD], [HD], [SD], [4K]
_TRAILING_TAGS = re.compile(r"\s*\[[^\]]*\]\s*$")

# Match a trailing year like (2023) or [2023]
_YEAR_PATTERN = re.compile(r"[\(\[]((?:19|20)\d{2})[\)\]]")


def _strip_tags(name: str) -> str:
    return _TRAILING_TAGS.sub("", name).strip()


def _sanitize_filename(name: str) -> str:
    """Remove/replace characters illegal in Windows filenames."""
    illegal = r'\/:*?"<>|'
    for ch in illegal:
        name = name.replace(ch, "_")
    return name.strip(". ")


def parse_series_name(tvg_name: str) -> tuple[str, int, int] | None:
    """
    Parse 'Title S01 E01' or 'Title S01E01' into (title, season, episode).
    Returns None if no match.
    """
    clean = _strip_tags(tvg_name)
    m = _SERIES_PATTERN.match(clean)
    if not m:
        return None
    title = m.group("title").strip()
    season = int(m.group("season"))
    ep = int(m.group("ep"))
    return title, season, ep


def parse_movie_name(tvg_name: str) -> tuple[str, int | None]:
    """
    Parse 'Title (YYYY)' into (title, year).
    Year is None if not found.
    """
    clean = _strip_tags(tvg_name)
    years = _YEAR_PATTERN.findall(clean)
    if years:
        year = int(years[-1])
        # Strip the last year match from the title
        clean_title = _YEAR_PATTERN.sub("", clean).strip().rstrip("-– ").strip()
        return clean_title, year
    return clean, None


def canonical_strm_path(dest_root: Path, entry: M3UEntry) -> Path | None:
    """
    Compute the canonical .strm path for a given M3UEntry.

    Returns None if the entry should be skipped (live TV, no category match).

    Examples:
      Series → <dest_root>/Series/1 Contra Todos/Season 01/1 Contra Todos - S01E01.strm
      Movies → <dest_root>/Filmes/Um Bom Advogado (2026)/Um Bom Advogado (2026).strm
    """
    category = group_to_category(entry.group_title, entry.url, entry.tvg_name)
    if category is None:
        return None

    if entry.media_type == "series":
        parsed = parse_series_name(entry.tvg_name)
        if not parsed:
            # Can't parse season/episode — put in root of category/title
            title = _sanitize_filename(_strip_tags(entry.tvg_name))
            if not title:
                return None
            return dest_root / category / title / f"{title}.strm"

        title, season, ep = parsed
        title = _sanitize_filename(title)
        if not title:
            return None
        season_dir = f"Season {season:02d}"
        filename = f"{title} - S{season:02d}E{ep:02d}.strm"
        return dest_root / category / title / season_dir / filename

    # movie
    title, year = parse_movie_name(entry.tvg_name)
    title = _sanitize_filename(title)
    if not title:
        return None
    folder_name = f"{title} ({year})" if year else title
    folder_name = _sanitize_filename(folder_name)
    return dest_root / category / folder_name / f"{folder_name}.strm"


# ---------------------------------------------------------------------------
# STRM file writer
# ---------------------------------------------------------------------------

def write_strm_if_new(path: Path, url: str) -> bool:
    """
    Write a .strm file containing *url* at *path*.

    - Creates parent directories automatically.
    - Skips if file already exists with the same URL (idempotent).
    - Updates if file exists with a different URL.

    Returns True if the file was created or updated, False if skipped.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing == url.strip():
                return False
        except OSError:
            pass
    path.write_text(url.strip() + "\n", encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Normalisation helpers for recovery matching
# ---------------------------------------------------------------------------

def _normalise_for_match(text: str) -> str:
    """Lowercase, strip accents, collapse whitespace — for fuzzy matching."""
    nfd = unicodedata.normalize("NFD", text)
    ascii_text = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    ascii_text = ascii_text.lower()
    ascii_text = re.sub(r"[^a-z0-9\s]", " ", ascii_text)
    return re.sub(r"\s+", " ", ascii_text).strip()


def _media_match_key(name: str, entry: M3UEntry | None = None) -> tuple:
    """Cria uma identidade estável para filme ou episódio entre M3U e Mongo."""
    clean = os.path.splitext(name)[0]
    series = parse_series_name(clean)
    if series:
        title, season, episode = series
        return ("series", _normalise_for_match(title), season, episode)
    episode_match = re.search(r"^(?P<title>.+?)\s*[._ -]*[Ss](\d{1,2})\s*[._ -]*[Ee](\d{1,3})\b", clean)
    if episode_match:
        return (
            "series",
            _normalise_for_match(episode_match.group("title")),
            int(episode_match.group(2)),
            int(episode_match.group(3)),
        )
    title, year = parse_movie_name(clean)
    if entry and entry.media_type == "series":
        parsed = parse_series_name(entry.tvg_name)
        if parsed:
            title, season, episode = parsed
            return ("series", _normalise_for_match(title), season, episode)
    return ("movie", _normalise_for_match(title), year or 0)


def _virtual_parent_to_disk(dest_root: Path, parent: object) -> Path | None:
    """Converte ``/raphael/Categoria/...`` em caminho dentro da raiz monitorada."""
    if not isinstance(parent, str) or not parent.startswith("/"):
        return None
    parts = [part for part in parent.split("/") if part]
    if len(parts) < 2:
        return None
    return dest_root.joinpath(*parts[1:])


# ---------------------------------------------------------------------------
# Recovery: fix MongoDB recovery_required items
# ---------------------------------------------------------------------------

# Module-level import so tests can patch it
try:
    from pymongo import MongoClient
except ImportError:  # pragma: no cover
    MongoClient = None  # type: ignore[assignment,misc]


def _fix_recovery_items(
    entries: list[M3UEntry],
    dest_root: Path,
    mongo_uri: str,
    db_name: str,
) -> int:
    """
    For each MongoDB doc with recovery_required=True, find the matching M3U entry,
    write the .strm and clear only the stale local-path recovery flag. The partial
    download file and its byte offset remain untouched for resume.

    Returns number of items fixed.
    """
    if MongoClient is None:
        logger.warning("pymongo not available — skipping recovery fix")
        return 0

    try:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        db = client[db_name]
        files = db["files"]
    except Exception as exc:
        logger.error("MongoDB connection failed: %s", exc)
        return 0

    # Index by media identity; unlike plain text matching this handles
    # ``S01 E01`` versus ``S01E01`` and different punctuation.
    norm_map: dict[tuple, list[M3UEntry]] = {}
    for e in entries:
        norm_map.setdefault(_media_match_key(e.tvg_name, e), []).append(e)

    fixed = 0
    try:
        docs = list(files.find({
            "recovery_required": True,
            "failed_reason": "local_path_missing",
        }))
    except Exception as exc:
        logger.error("MongoDB query failed: %s", exc)
        return 0

    for doc in docs:
        name: str = str(doc.get("name", ""))
        parent = doc.get("parent", "")
        candidates = norm_map.get(_media_match_key(name)) or []
        matched_entry = candidates[0] if len(candidates) == 1 else None

        if matched_entry is None:
            logger.info("Recovery: no M3U match for '%s'", name)
            continue

        parent_path = _virtual_parent_to_disk(dest_root, parent)
        if parent_path:
            strm_name = _sanitize_filename(os.path.splitext(name)[0]) + ".strm"
            strm_path = parent_path / strm_name
        else:
            strm_path = canonical_strm_path(dest_root, matched_entry)
            if strm_path is None:
                logger.info("Recovery: can't compute path for '%s'", name)
                continue

        created = write_strm_if_new(strm_path, matched_entry.url)
        action = "created" if created else "already OK"
        logger.info("Recovery [%s]: %s → %s", action, name, strm_path)

        try:
            files.update_one(
                {"_id": doc["_id"]},
                {"$set": {"recovery_required": False, "failed_reason": None}},
            )
        except Exception as exc:
            logger.error("MongoDB update failed for '%s': %s", name, exc)
            continue
        fixed += 1

    return fixed


# ---------------------------------------------------------------------------
# Main sync entry point
# ---------------------------------------------------------------------------

def run_m3u_sync(
    m3u_url: str,
    dest_root: str | Path,
    mongo_uri: str,
    db_name: str = "ftp",
    *,
    fix_recovery: bool = True,
    timeout: int = 120,
    progress_callback=None,
    catalog_path: str | Path | None = None,
) -> dict:
    """
    Fetch the M3U, create/update .strm files, optionally fix recovery items.

    Args:
        m3u_url:           Full URL of the M3U playlist.
        dest_root:         Root media directory (e.g. D:\\midias).
        mongo_uri:         MongoDB connection URI.
        db_name:           MongoDB database name (default "ftp").
        fix_recovery:      Whether to fix recovery_required items.
        timeout:           HTTP timeout for downloading the playlist.
        progress_callback: Optional callable(message: str) for progress updates.

    Returns:
        dict with creation counts, category totals and recovery candidates.
    """
    dest_root = Path(dest_root)
    stats = {
        "created": 0,
        "skipped": 0,
        "recovery_fixed": 0,
        "errors": 0,
        "total_entries": 0,
        "categories": {},
        "catalog_path": str(catalog_path) if catalog_path else None,
    }
    catalog: list[dict[str, str]] = []

    def _log(msg: str) -> None:
        logger.info(msg)
        if progress_callback:
            try:
                progress_callback(msg)
            except Exception:
                pass

    _log(f"Iniciando sync M3U → {dest_root}")
    _log(f"URL: {redact_url(normalize_m3u_url(m3u_url))}")

    # Collect all entries so we can reuse for recovery matching
    entries: list[M3UEntry] = []

    try:
        for entry in parse_m3u(m3u_url, timeout=timeout):
            entries.append(entry)
            stats["total_entries"] += 1
            strm_path = canonical_strm_path(dest_root, entry)
            if strm_path is None:
                stats["skipped"] += 1
                continue
            category = strm_path.relative_to(dest_root).parts[0]
            stats["categories"][category] = stats["categories"].get(category, 0) + 1
            catalog.append({
                "name": entry.tvg_name,
                "group": entry.group_title,
                "category": category,
                "media_type": entry.media_type,
                "relative_path": str(strm_path.relative_to(dest_root)),
            })
            try:
                created = write_strm_if_new(strm_path, entry.url)
                if created:
                    stats["created"] += 1
                    _log(f"  [+] {strm_path.relative_to(dest_root)}")
                else:
                    stats["skipped"] += 1
            except OSError as exc:
                stats["errors"] += 1
                logger.error("STRM write error for '%s': %s", entry.tvg_name, exc)
    except Exception as exc:
        stats["errors"] += 1
        logger.error("M3U fetch/parse failed: %s", exc)
        _log(f"ERRO ao buscar/parsear M3U: {exc}")
        return stats

    if catalog_path:
        try:
            catalog_file = Path(catalog_path)
            catalog_file.parent.mkdir(parents=True, exist_ok=True)
            catalog_file.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            stats["errors"] += 1
            _log(f"⚠ Catálogo local não foi salvo: {exc}")

    _log(
        f"STRM sync concluído: {stats['created']} criados, "
        f"{stats['skipped']} ignorados, {stats['errors']} erros | "
        f"categorias: {stats['categories']}"
    )

    if fix_recovery and entries:
        _log("Verificando itens de recuperação no MongoDB...")
        try:
            fixed = _fix_recovery_items(entries, dest_root, mongo_uri, db_name)
            stats["recovery_fixed"] = fixed
            _log(f"Recuperação: {fixed} STRMs corrigidos")
        except Exception as exc:
            logger.error("Recovery fix failed: %s", exc)
            _log(f"ERRO na recuperação: {exc}")

    return stats


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="M3U → STRM generator")
    parser.add_argument("--url", required=True, help="M3U playlist URL")
    parser.add_argument("--dest", default="D:\\midias", help="Destination root")
    parser.add_argument("--mongo", default="mongodb://localhost:27017", help="MongoDB URI")
    parser.add_argument("--db", default="ftp", help="MongoDB database name")
    parser.add_argument("--no-recovery", action="store_true", help="Skip recovery fix")
    args = parser.parse_args()

    result = run_m3u_sync(
        m3u_url=args.url,
        dest_root=args.dest,
        mongo_uri=args.mongo,
        db_name=args.db,
        fix_recovery=not args.no_recovery,
        progress_callback=print,
    )
    print(result)
