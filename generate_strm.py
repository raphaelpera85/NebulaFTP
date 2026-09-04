import os
import re
import stat
from pathlib import Path, PurePosixPath

from dotenv import load_dotenv
from pymongo import MongoClient

if os.path.exists(".env"):
    load_dotenv()

mongo_uri = os.getenv("MONGODB", "mongodb://localhost:27017")
host = os.getenv("STREAM_HOST", "127.0.0.1")
port = os.getenv("STREAM_PORT", "2122")
root_dir = Path(__file__).resolve().parent / "strm_library"
WINDOWS_INVALID_NAME = re.compile(r'[<>:"/\\|?*]')
DIRECT_STREAM_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}


def safe_windows_name(value):
    cleaned = WINDOWS_INVALID_NAME.sub(" - ", str(value))
    cleaned = re.sub(r"\s+", " ", cleaned).rstrip(" .")
    return cleaned or "_"


def stream_endpoint(name):
    return "stream" if Path(name).suffix.lower() in DIRECT_STREAM_EXTENSIONS else "transcode"


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
    removed_count = 0

    try:
        completed_files = list(
            db.files.find(
                {"type": "file", "status": "completed", "parts.0": {"$exists": True}},
                {"_id": 1, "name": 1, "parent": 1},
            )
        )
        for doc in completed_files:
            name = str(doc.get("name", ""))
            parts = list(PurePosixPath(str(doc.get("parent", ""))).parts)
            if parts and parts[0] == "/":
                parts.pop(0)
            if not parts or parts.pop(0) != library_user:
                continue

            target_dir = output_root / Path(*(safe_windows_name(part) for part in parts))
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{safe_windows_name(Path(name).stem)}.strm"
            temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            try:
                temp.write_text(
                    f"{base_url}/{stream_endpoint(name)}?id={doc['_id']}",
                    encoding="utf-8",
                )
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
            generated_files.add(target)

        if prune:
            removed_count = remove_stale_strm_files(output_root, generated_files)
    finally:
        client.close()

    result = {
        "generated": len(generated_files),
        "removed": removed_count,
        "output": str(output_root),
    }
    print(
        f"STRM concluido: gerados={result['generated']} removidos={result['removed']} "
        f"saida={result['output']}"
    )
    return result


if __name__ == "__main__":
    generate_strm_files()
