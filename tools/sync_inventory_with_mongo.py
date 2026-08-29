import json
import os
import sys
import pymongo

def main():
    client = pymongo.MongoClient(os.getenv("MONGODB", "mongodb://localhost:27017"))
    db = client[os.getenv("MONGO_DATABASE", "ftp")]
    live_docs = {str(d["_id"]): d for d in db.files.find({"type": "file"})}

    http_inventory_path = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/http_probe/ffprobe_inventory.json"
    output_inventory_path = "d:/Users/rapha/Documents/Projetos/nebula/media_audit/ffprobe_inventory.json"

    with open(http_inventory_path, "r", encoding="utf-8") as f:
        items = json.load(f)

    updated = []
    matched = 0
    for item in items:
        mid = item["mongo_id"]
        if mid in live_docs:
            doc = live_docs[mid]
            item["mongo_parent"] = doc["parent"]
            item["mongo_name"] = doc["name"]
            item["mongo_size"] = doc.get("size")
            item["obfuscated_id"] = doc.get("obfuscated_id")
            parent_rel = doc["parent"].replace("/raphael/", "").replace("/", "\\")
            item["mounted_path"] = f"N:\\{parent_rel}\\{doc['name']}"
            updated.append(item)
            matched += 1

    with open(output_inventory_path, "w", encoding="utf-8") as f:
        json.dump(updated, f, ensure_ascii=False, indent=2)

    print(f"Successfully synced {matched} items with live MongoDB records.")

if __name__ == "__main__":
    main()
