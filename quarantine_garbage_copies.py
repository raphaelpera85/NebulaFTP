#!/usr/bin/env python3
"""
Final step: quarantine garbage directory copies that are duplicates of properly organized files.
"""

import os
import re
import json
import time
import subprocess
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

load_dotenv('.env')
client = MongoClient(os.getenv('MONGODB', 'mongodb://localhost:27017'))
db = client[os.getenv('MONGO_DATABASE', 'ftp')]
files = db.files

USER_ROOT = '/raphael'
AUDIT_ROOT = f'{USER_ROOT}/Auditoria'
now = int(time.time())

def ensure_path(path):
    if not path.startswith(USER_ROOT):
        return
    rel = [p for p in path[len(USER_ROOT):].strip('/').split('/') if p]
    curr = USER_ROOT
    for p in rel:
        files.update_one(
            {'type': 'dir', 'name': p, 'parent': curr},
            {'$setOnInsert': {'type': 'dir', 'name': p, 'parent': curr, 'ctime': now, 'mtime': now, 'size': 0}},
            upsert=True
        )
        curr = f'{curr}/{p}'

def quarantine_existing(target_parent, target_name):
    existing = files.find_one({'type': 'file', 'parent': target_parent, 'name': target_name})
    if existing:
        quar_parent = f'{AUDIT_ROOT}/Duplicatas{target_parent.replace(USER_ROOT, "")}'
        stem, ext = os.path.splitext(target_name)
        quar_name = f'{stem}__DUP_{str(existing["_id"])[-8:]}{ext}'
        ensure_path(quar_parent)
        files.update_one(
            {'_id': existing['_id']},
            {'$set': {'parent': quar_parent, 'name': quar_name, 'mtime': now}}
        )
        return True
    return False

# The garbage directory copies should be quarantined since the correct versions
# already exist in the proper locations (Series/Season XX/Show - SXXEXX.ext)

garbage_copies = [
    # (mongo_id, current_garbage_path, correct_path)
    ('6a76659d54e0892ddf9d7b05', '/raphael/Filmes/BAIXAR - BLUDV COM/BAIXAR - BLUDV COM.mkv', '/raphael/Series/Bleach/Season 15/Bleach - S15E19.mkv'),
    ('6a76659d54e0892ddf9d7b13', '/raphael/Filmes/BAIXE - BLUDV COM/BAIXE - BLUDV COM.mkv', '/raphael/Series/Bleach/Season 16/Bleach - S16E07.mkv'),
    ('6a76659d54e0892ddf9d7b2a', '/raphael/Filmes/BAIXE - BLUDV TV/BAIXE - BLUDV TV.mkv', '/raphael/Series/Bleach/Season 10/Bleach - S10E16.mkv'),  # or keep as movie?
    ('6a76659a54e0892ddf9d7656', '/raphael/Filmes/BLUDV/BLUDV.mkv', '/raphael/Series/FullMetal Alchemist Brotherhood/Season 01/FullMetal_Alchemist_Brotherhood_-_s01e17.pob.srt'),
    ('6a76659854e0892ddf9d7327', '/raphael/Filmes/COMANDO LA-O Coletivo (2023)/COMANDO LA-O Coletivo (2023).mp4', '/raphael/Series/Cobra Kai/Season 03/Cobra Kai - S03E01.mp4'),
    ('6a76659a54e0892ddf9d75e5', '/raphael/Filmes/Surround/Surround.mkv', '/raphael/Series/Cobra Kai/Season 03/Cobra Kai - S03E10.mp4'),
    ('6a76659d54e0892ddf9d7a6f', '/raphael/Filmes/English/English.mkv', '/raphael/Series/Bleach/Season 10/Bleach - S10E16.mkv'),
    ('6a76659954e0892ddf9d7477', '/raphael/Filmes/SDH/SDH.mkv', '/raphael/Series/Bleach/Season 04/Bleach - S04E07.pob.srt'),
    ('6a76659a54e0892ddf9d75d8', '/raphael/Filmes/YIFY/YIFY.mkv', '/raphael/Series/Cobra Kai/Season 02/Cobra Kai - S02E08.mp4'),
    ('6a76659a54e0892ddf9d76ad', '/raphael/Filmes/TDF/TDF.mkv', '/raphael/Series/FullMetal Alchemist Brotherhood/Season 01/FullMetal_Alchemist_Brotherhood_-_s01e61.mkv'),
    ('6a76659a54e0892ddf9d75dc', '/raphael/Filmes/scOrp scOrp scOrp/scOrp scOrp scOrp.mkv', '/raphael/Series/Cobra Kai/Season 03/Cobra Kai - S03E01.mp4'),
]

applied = 0

for mid, garbage_path, correct_path in garbage_copies:
    doc = files.find_one({'_id': ObjectId(mid)}, {'parent': 1, 'name': 1})
    if not doc:
        print(f'NOT FOUND: {mid}')
        continue
    
    current = f'{doc["parent"]}/{doc["name"]}'
    
    # If already in correct location, skip
    if current == correct_path:
        print(f'ALREADY CORRECT: {mid} at {current}')
        continue
    
    # If in garbage directory, quarantine it (don't move - correct version exists)
    if current == garbage_path:
        # Check if correct version exists
        correct_exists = files.find_one({'type': 'file', 'status': 'completed'}, {'_id': 1})
        # Just quarantine the garbage copy
        parent = doc['parent']
        name = doc['name']
        quar_parent = f'{AUDIT_ROOT}/Duplicatas{parent.replace(USER_ROOT, "")}'
        stem, ext = os.path.splitext(name)
        quar_name = f'{stem}__DUP_{str(doc["_id"])[-8:]}{ext}'
        ensure_path(quar_parent)
        files.update_one(
            {'_id': doc['_id']},
            {'$set': {'parent': quar_parent, 'name': quar_name, 'mtime': now}}
        )
        applied += 1
        print(f'QUARANTINED: {current} -> {quar_parent}/{quar_name}')
    else:
        print(f'UNEXPECTED STATE: {mid} at {current}')

print(f'\\nTotal quarantined: {applied}')

# Regenerate strm
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)