import argparse
import concurrent.futures
import json
import os
from pathlib import Path, PurePosixPath

from dotenv import load_dotenv
from pymediainfo import MediaInfo
from pymongo import MongoClient


VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".ts", ".wmv"}
DEFAULT_MEDIAINFO_DLL = Path(
    r"C:\Program Files (x86)\K-Lite Codec Pack\MPC-HC64\mediainfo.dll"
)


def mounted_path(doc: dict, drive: str) -> Path:
    parent = PurePosixPath(doc.get("parent", "/"))
    parts = list(parent.parts)
    if parts and parts[0] == "/":
        parts = parts[1:]
    if parts and parts[0].casefold() == "raphael":
        parts = parts[1:]
    return Path(f"{drive}\\", *parts, doc["name"])


def inspect_one(doc: dict, drive: str, library_file: str) -> dict:
    path = mounted_path(doc, drive)
    result = {
        "doc_id": str(doc["_id"]),
        "mongo_path": f"{doc.get('parent', '')}/{doc['name']}",
        "mounted_path": str(path),
        "db_size": doc.get("size", 0),
        "first_tg_message": (
            doc.get("parts", [{}])[0].get("tg_message") if doc.get("parts") else None
        ),
    }
    try:
        info = MediaInfo.parse(
            str(path),
            library_file=library_file,
            cover_data=False,
            parse_speed=0.0,
            full=False,
            buffer_size=64 * 1024,
        )
        general = next((track for track in info.tracks if track.track_type == "General"), None)
        if general is None:
            result["error"] = "no_general_track"
            return result
        result.update(
            {
                "embedded_title": getattr(general, "title", None),
                "movie_name": getattr(general, "movie_name", None),
                "format": getattr(general, "format", None),
                "duration_ms": getattr(general, "duration", None),
                "reported_file_size": getattr(general, "file_size", None),
                "encoded_application": getattr(general, "encoded_application", None),
            }
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def stratified_sample(docs: list[dict], per_extension: int) -> list[dict]:
    selected = []
    for extension in (".mkv", ".mp4", ".avi", ".m4v"):
        matches = [doc for doc in docs if Path(doc["name"]).suffix.casefold() == extension]
        if not matches:
            continue
        if len(matches) <= per_extension:
            selected.extend(matches)
            continue
        step = max(1, len(matches) // per_extension)
        selected.extend(matches[::step][:per_extension])
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drive", default="N:")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--per-extension", type=int, default=6)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--output", default="embedded_media_title_audit.json")
    parser.add_argument("--mediainfo-dll", default=str(DEFAULT_MEDIAINFO_DLL))
    args = parser.parse_args()

    load_dotenv()
    files = MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))[
        os.getenv("MONGO_DATABASE", "ftp")
    ].files
    docs = list(
        files.find(
            {
                "type": "file",
                "name": {
                    "$regex": r"\.(?:avi|m4v|mkv|mov|mp4|ts|wmv)$",
                    "$options": "i",
                },
            },
            {"name": 1, "parent": 1, "size": 1, "parts": 1},
        ).sort([("parent", 1), ("name", 1)])
    )
    selected = docs if args.all else stratified_sample(docs, args.per_extension)
    print(f"media_docs={len(docs)} selected={len(selected)} workers={args.workers}", flush=True)

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(inspect_one, doc, args.drive, args.mediainfo_dll): doc
            for doc in selected
        }
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            title = result.get("embedded_title") or result.get("movie_name")
            print(
                f"[{index}/{len(selected)}] {result['mongo_path']} -> "
                f"{title or result.get('error') or '<no title>'}",
                flush=True,
            )

    results.sort(key=lambda item: item["mongo_path"].casefold())
    output_path = Path(args.output)
    output_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    titled = sum(bool(item.get("embedded_title") or item.get("movie_name")) for item in results)
    errors = sum("error" in item for item in results)
    print(
        f"complete={len(results)} titled={titled} errors={errors} output={output_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
