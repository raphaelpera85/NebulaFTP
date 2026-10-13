import os
import re
import stat
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from dotenv import load_dotenv
from pymongo import MongoClient

if os.path.exists(".env"):
    load_dotenv()

mongo_uri = os.getenv("MONGODB", "mongodb://localhost:27017")
host = os.getenv("STREAM_HOST", "127.0.0.1")
port = os.getenv("STREAM_PORT", "2122")
root_dir = Path(__file__).resolve().parent / "strm_library"
WINDOWS_INVALID_NAME = re.compile(r'[<>:"/\\|?*]')
GENERIC_MOVIE_INFO_NAME = re.compile(r"^movie\.info\.[0-9a-f]{8,64}$", re.IGNORECASE)
NFO_REFERENCE_NAME = re.compile(r"\.nfo(?:\.|$)", re.IGNORECASE)
EXECUTABLE_MEDIA_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".webm",
    ".flv", ".mpeg", ".mpg", ".m2ts", ".3gp", ".mp3", ".m4a", ".flac",
    ".wav", ".aac", ".ogg", ".opus", ".oga",
}
# .strm files target native media-center players (VLC, Kodi, Jellyfin, Plex,
# Emby), which decode any container/codec locally. They must point at
# /stream (raw bytes, Accept-Ranges/Content-Length) so the player can seek.
# /transcode has no Range support (chunked, no Content-Length) and is only
# meant for the HTML5 <video> fallback used by browsers in /play — routing
# .strm files through it is why seeking silently did nothing in VLC.
DIRECT_STREAM_EXTENSIONS = None  # unused: kept for backwards-compat imports


def safe_windows_name(value):
    cleaned = WINDOWS_INVALID_NAME.sub(" - ", str(value))
    cleaned = re.sub(r"\s+", " ", cleaned).rstrip(" .")
    return cleaned or "_"


def stream_endpoint(name):
    return "stream"


def strm_document_id(path):
    """Return the MongoDB ID from a valid Nebula raw-stream link."""
    try:
        url = urlsplit(path.read_text(encoding="utf-8-sig").strip())
        ids = parse_qs(url.query).get("id", [])
    except (OSError, UnicodeError, ValueError):
        return None

    if (
        url.scheme.lower() not in {"http", "https"}
        or not url.netloc
        or url.path.rstrip("/").rsplit("/", 1)[-1].casefold() != "stream"
        or len(ids) != 1
        or not ids[0].strip()
    ):
        return None
    return ids[0].strip()


def write_strm_file(target, url, *, replace_existing=False, eligible_ids=()):
    if not replace_existing:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(url)
        return True

    temp = target.with_name(f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        temp.write_text(url, encoding="utf-8")
        if strm_document_id(target) in eligible_ids:
            return False
        temp.replace(target)
        return True
    finally:
        temp.unlink(missing_ok=True)


def eligible_strm_targets(completed_files, output_root, library_user):
    targets = {}
    for doc in completed_files:
        name = str(doc.get("name", ""))
        # Sidecar NFO documents are metadata, not playable media. Keep only
        # records whose final suffix is an executable media format if the
        # filename itself contains an NFO marker (e.g. movie.nfo.<hash>.partial).
        if (
            NFO_REFERENCE_NAME.search(name)
            and Path(name).suffix.casefold() not in EXECUTABLE_MEDIA_EXTENSIONS
        ):
            continue
        parts = list(PurePosixPath(str(doc.get("parent", ""))).parts)
        if parts and parts[0] == "/":
            parts.pop(0)
        if not parts or parts[0] != library_user:
            continue
        target_dir = output_root / Path(*(safe_windows_name(part) for part in parts[1:]))
        stem = Path(name).stem
        if (
            len(parts) >= 3
            and parts[1].casefold() == "filmes"
            and GENERIC_MOVIE_INFO_NAME.fullmatch(stem)
        ):
            # Some imported movie files carry a generated movie.info.<hash>
            # filename. The Mongo parent already contains the library title.
            stem = parts[-1]
        target = target_dir / f"{safe_windows_name(stem)}.strm"
        targets[str(doc["_id"])] = (doc, target)
    return targets


def create_completed_media_strm(
    doc,
    *,
    output_root,
    stream_base_url,
    library_user,
):
    """Create one STRM after a playable media document is fully uploaded.

    Existing targets are preserved unless they already point to this document.
    The exclusive create prevents concurrent uploads from overwriting a link.
    """
    if (
        doc.get("type") != "file"
        or doc.get("status") != "completed"
        or not doc.get("parts")
        or Path(str(doc.get("name", ""))).suffix.casefold() not in EXECUTABLE_MEDIA_EXTENSIONS
    ):
        return "ineligible", None

    target_info = eligible_strm_targets([doc], Path(output_root), library_user).get(str(doc.get("_id")))
    if not target_info:
        return "ineligible", None

    completed_doc, target = target_info
    document_id = str(completed_doc["_id"])
    expected_url = (
        f"{stream_base_url.rstrip('/')}/"
        f"{stream_endpoint(completed_doc.get('name', ''))}?id={document_id}"
    )
    if target.exists() or target.is_symlink():
        outcome = (
            "skipped"
            if not target.is_symlink() and target.is_file() and strm_document_id(target) == document_id
            else "conflict"
        )
        return outcome, target

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_strm_file(target, expected_url)
    except FileExistsError:
        outcome = (
            "skipped"
            if not target.is_symlink() and target.is_file() and strm_document_id(target) == document_id
            else "conflict"
        )
        return outcome, target
    return "generated", target


def relocate_generic_movie_strm(paths, target, document_id, expected_url):
    """Move one verified generic movie link to its title-based filename."""
    if len(paths) != 1 or target.exists() or target.is_symlink():
        return False
    source = paths[0]
    if (
        source.parent != target.parent
        or not GENERIC_MOVIE_INFO_NAME.fullmatch(source.stem)
        or source.is_symlink()
        or not source.is_file()
        or strm_document_id(source) != document_id
    ):
        return False

    try:
        write_strm_file(target, expected_url)
    except FileExistsError:
        return False
    if strm_document_id(target) != document_id:
        target.unlink(missing_ok=True)
        return False
    try:
        source.unlink()
    except OSError:
        # Keep the verified original if cleanup fails; both links remain valid.
        return False
    return True


def index_existing_strm_links(output_root, eligible_ids):
    existing_ids = set()
    existing_paths = {}
    invalid_count = 0
    if not output_root.exists():
        return existing_ids, existing_paths, invalid_count

    for path in output_root.rglob("*"):
        if path.suffix.casefold() != ".strm":
            continue
        if path.is_symlink():
            invalid_count += 1
            continue
        if not path.is_file():
            continue
        document_id = strm_document_id(path)
        if document_id in eligible_ids:
            existing_ids.add(document_id)
            existing_paths.setdefault(document_id, []).append(path)
        else:
            invalid_count += 1
    return existing_ids, existing_paths, invalid_count


def write_strm_delta(target, document_id, expected_url, existing_ids, eligible_ids):
    target_exists = target.exists() or target.is_symlink()
    if document_id in existing_ids:
        target_matches = not target_exists or (
            not target.is_symlink()
            and target.is_file()
            and strm_document_id(target) == document_id
        )
        return "skipped" if target_matches else "conflict"

    if target_exists and (
        target.is_symlink()
        or not target.is_file()
        or strm_document_id(target) in eligible_ids
    ):
        return "conflict"

    target.parent.mkdir(parents=True, exist_ok=True)
    if target_exists:
        written = write_strm_file(
            target, expected_url, replace_existing=True, eligible_ids=eligible_ids
        )
        return "updated" if written else "conflict"
    try:
        write_strm_file(target, expected_url)
    except FileExistsError:
        return "skipped" if strm_document_id(target) == document_id else "conflict"
    return "generated"


def remove_stale_strm_files(output_root, generated_files):
    removed = 0
    for path in output_root.rglob("*.strm"):
        if path not in generated_files:
            path.unlink()
            removed += 1

    directories = sorted(
        (path for path in output_root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        if any(directory.iterdir()):
            continue
        try:
            directory.rmdir()
        except PermissionError:
            directory.chmod(directory.stat().st_mode | stat.S_IWRITE)
            directory.rmdir()
        except OSError:
            pass
    return removed


def generate_strm_files(
    *,
    mongo_url=None,
    database=None,
    output_root=None,
    stream_base_url=None,
    library_user=None,
    prune=False,
):
    client = MongoClient(mongo_url or mongo_uri)
    db = client[database or os.getenv("MONGO_DATABASE", "ftp")]
    output_root = Path(output_root or os.getenv("STRM_OUTPUT_DIR", root_dir)).resolve()
    base_url = (stream_base_url or f"http://{host}:{port}").rstrip("/")
    library_user = library_user or os.getenv("NEBULA_LIBRARY_USER", "raphael")
    generated_files = set()
    counts = {"generated": 0, "updated": 0, "skipped": 0, "renamed": 0, "conflict": 0}
    removed_count = 0
    invalid_link_count = 0

    try:
        completed_files = list(
            db.files.find(
                {"type": "file", "status": "completed", "parts.0": {"$exists": True}},
                {"_id": 1, "name": 1, "parent": 1},
            )
        )
        eligible_files = eligible_strm_targets(completed_files, output_root, library_user)
        eligible_ids = set(eligible_files)
        existing_ids, existing_paths, invalid_link_count = index_existing_strm_links(
            output_root, eligible_ids
        )
        for paths in existing_paths.values():
            generated_files.update(paths)
        for document_id, (doc, target) in eligible_files.items():
            generated_files.add(target)
            expected_url = f"{base_url}/{stream_endpoint(doc.get('name', ''))}?id={document_id}"
            if relocate_generic_movie_strm(
                existing_paths.get(document_id, []), target, document_id, expected_url
            ):
                counts["renamed"] += 1
                continue
            outcome = write_strm_delta(target, document_id, expected_url, existing_ids, eligible_ids)
            counts[outcome] += 1

        if prune:
            removed_count = remove_stale_strm_files(output_root, generated_files)
    finally:
        client.close()

    result = {
        **counts,
        "validated": invalid_link_count + sum(len(paths) for paths in existing_paths.values()),
        "invalid_links": invalid_link_count,
        "conflicts": counts["conflict"],
        "removed": removed_count,
        "output": str(output_root),
    }
    print(
        f"STRM concluido: gerados={result['generated']} atualizados={result['updated']} "
        f"preservados={result['skipped']} renomeados={result['renamed']} "
        f"validados={result['validated']} "
        f"links_invalidos={result['invalid_links']} conflitos={result['conflicts']} "
        f"removidos={result['removed']} "
        f"saida={result['output']}"
    )
    return result


if __name__ == "__main__":
    generate_strm_files()
