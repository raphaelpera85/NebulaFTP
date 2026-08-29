import json
import os
import sys
import concurrent.futures
from pathlib import Path
import pymongo

sys.path.insert(0, "d:/Users/rapha/Documents/Projetos/nebula/NebulaFTP-master/tools")
from audit_all_media_ffprobe import probe_one

def main():
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    docs = list(db.files.find({"type": "file"}))

    ffprobe_path = Path("C:/Users/rapha/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-9.0-full_build/bin/ffprobe.exe")
    http_base = "http://127.0.0.1:2122"

    inventory_file = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/http_probe/ffprobe_inventory.json"
    existing_inv = {x["mongo_id"]: x for x in json.load(open(inventory_file, encoding="utf-8"))}

    pending = [doc for doc in docs if str(doc["_id"]) not in existing_inv]
    print(f"Total docs={len(docs)}, already in inventory={len(existing_inv)}, pending={len(pending)}")

    if not pending:
        print("All documents are already probed and in inventory!")
        return

    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(probe_one, doc, ffprobe_path, "N:", 10, http_base): doc
            for doc in pending
        }
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            existing_inv[res["mongo_id"]] = res
            completed += 1
            if completed % 25 == 0 or completed == len(pending):
                print(f"Probed [{completed}/{len(pending)}]...")

    with open(inventory_file, "w", encoding="utf-8") as f:
        json.dump(list(existing_inv.values()), f, ensure_ascii=False, indent=2)

    print(f"COMPLETED! Updated inventory file with {len(existing_inv)} total probed files.")

if __name__ == "__main__":
    main()
