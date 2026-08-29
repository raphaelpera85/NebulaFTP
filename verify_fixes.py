import os
from dotenv import load_dotenv
load_dotenv('.env')
from pymongo import MongoClient

client = MongoClient(os.getenv('MONGODB', 'mongodb://localhost:27017'))
db = client[os.getenv('MONGO_DATABASE', 'ftp')]

# Check Spy x Family
spy = list(db.files.find({'parent': '/raphael/Series/Spy x Family/Season 01'}, {'name': 1, 'size': 1, 'parts': 1}).sort('name', 1))
print(f"Spy x Family S01: {len(spy)} files")
for s in spy:
    print(f"  {s['_id']} | {s['name']} | size: {s.get('size', 0)/(1024*1024):.1f}MB | parts: {len(s.get('parts', []))}")

# Check Bleach
bleach = list(db.files.find({'parent': '/raphael/Series/Bleach/Season 04'}, {'name': 1, 'size': 1, 'parts': 1}).sort('name', 1))
print(f"\nBleach S04: {len(bleach)} files")
for b in bleach[:10]:
    print(f"  {b['_id']} | {b['name']} | size: {b.get('size', 0)/(1024*1024):.1f}MB | parts: {len(b.get('parts', []))}")

# Check Strm output
strm_dir = 'strm_library'
if os.path.exists(strm_dir):
    strm_files = list(Path(strm_dir).rglob('*.strm'))
    print(f"\nSTRM files generated: {len(strm_files)}")
    for sf in strm_files[:5]:
        with open(sf, 'r') as f:
            print(f"  {sf}: {f.read().strip()}")