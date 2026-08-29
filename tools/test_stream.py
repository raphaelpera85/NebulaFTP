import os
import urllib.request
from dotenv import load_dotenv
import pymongo

load_dotenv()

mongo_uri = os.getenv("MONGODB", "mongodb://localhost:27017")
db_name = os.getenv("MONGO_DATABASE", "ftp")

client = pymongo.MongoClient(mongo_uri)
db = client[db_name]

doc = db.files.find_one({"type": "file", "parts.tg_file": {"$ne": "restored"}})
if not doc:
    print("No valid doc found!")
else:
    doc_id = str(doc["_id"])
    doc_name = doc["name"]
    print(f"Testing stream for doc_id={doc_id}, name={doc_name}")
    url = f"http://127.0.0.1:2122/stream?id={doc_id}"
    print(f"Requesting URL: {url}")

    req = urllib.request.Request(url, headers={"Range": "bytes=0-1024"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
            print(f"HTTP Stream Response Status: {resp.status}")
            print(f"Bytes received: {len(data)}")
    except Exception as e:
        print(f"HTTP Stream Exception: {type(e).__name__} -> {e}")
