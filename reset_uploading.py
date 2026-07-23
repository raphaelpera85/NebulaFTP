import os
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()
uri = os.getenv("MONGODB", "mongodb://localhost:27017")
client = MongoClient(uri)
db = client.ftp

res = db.files.update_many({"status": {"$in": ["uploading", "staging"]}}, {"$set": {"status": "queued"}})
print(f"Arquivos resetados de 'uploading/staging' para 'queued': {res.modified_count}")
client.close()
