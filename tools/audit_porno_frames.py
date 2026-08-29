import json
import os
import sys
import subprocess
import urllib.request
import pymongo
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

def main():
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    
    porno_docs = list(db.files.find({"type": "file", "parent": {"$regex": "^/raphael/Porno"}}))
    print(f"Found {len(porno_docs)} docs in Porno to verify frame by frame...")

    ffmpeg_path = r"C:\Users\rapha\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe"
    out_dir = Path("d:/Users/rapha/Documents/Projetos/nebula/media_audit/porno_frames")
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []

    for idx, doc in enumerate(porno_docs, 1):
        mid = str(doc["_id"])
        url = f"http://127.0.0.1:2122/stream?id={mid}"
        frame_file = out_dir / f"{mid}.jpg"

        cmd = [
            ffmpeg_path, "-hide_banner", "-loglevel", "error",
            "-ss", "300.0", "-i", url, "-frames:v", "1",
            "-vf", "scale='min(960,iw)':-2", "-q:v", "3", "-y", str(frame_file)
        ]
        
        try:
            subprocess.run(cmd, check=True, timeout=20)
            print(f"[{idx}/{len(porno_docs)}] Extracted frame for {doc['name'][:40]}... (size: {frame_file.stat().st_size} bytes)")
            
            # Query trace.moe
            req = urllib.request.Request(
                "https://api.trace.moe/search?anilistInfo",
                data=frame_file.read_bytes(),
                headers={"Content-Type": "image/jpeg", "User-Agent": "NebulaAudit/1.0"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                results.append({"mongo_id": mid, "name": doc["name"], "trace_moe": data})
        except Exception as e:
            print(f"[{idx}/{len(porno_docs)}] Error frame extraction for {mid}: {e}")

    with open("d:/Users/rapha/Documents/Projetos/nebula/media_audit/porno_visual_audit.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print("\nFINISHED extracting and querying frames for all Porno files!")

if __name__ == "__main__":
    main()
