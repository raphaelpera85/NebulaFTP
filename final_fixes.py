#!/usr/bin/env python3
"""Final fixes for remaining issues"""

import os
import re
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
    if not path.startswith('/raphael'):
        return
    rel = [p for p in path[len('/raphael'):].strip('/').split('/') if p]
    curr = '/raphael'
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
        quar_parent = f'{AUDIT_ROOT}/Duplicatas{target_parent.replace("/raphael", "")}'
        stem, ext = os.path.splitext(target_name)
        quar_name = f'{stem}__DUP_{str(existing["_id"])[-8:]}{ext}'
        ensure_path(quar_parent)
        files.update_one(
            {'_id': existing['_id']},
            {'$set': {'parent': quar_parent, 'name': quar_name, 'mtime': now}}
        )
        return True
    return False

def safe_name(name):
    name = re.sub(r'[<>:\\"/\\|?*]+', ' - ', name)
    name = re.sub(r'\s+', ' ', name).strip(' .-')
    return name[:180]

def ensure_path(path):
    if not path.startswith('/raphael'):
        return
    rel = [p for p in path[len('/raphael'):].strip('/').split('/') if p]
    curr = '/raphael'
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
        quar_parent = f'{AUDIT_ROOT}/Duplicatas{target_parent.replace("/raphael", "")}'
        stem, ext = os.path.splitext(target_name)
        quar_name = f'{stem}__DUP_{str(existing["_id"])[-8:]}{ext}'
        ensure_path(quar_parent)
        files.update_one(
            {'_id': existing['_id']},
            {'$set': {'parent': quar_parent, 'name': quar_name, 'mtime': now}}
        )
        return True
    return False

def apply_correction(mongo_id, desired_parent, desired_name):
    doc = files.find_one({'_id': ObjectId(mongo_id)}, {'parent': 1, 'name': 1})
    if not doc:
        return False
    current = f'{doc["parent"]}/{doc["name"]}'
    desired = f'{desired_parent}/{desired_name}'
    if current.lower() == desired.lower():
        return True
    quarantine_existing(desired_parent, desired_name)
    ensure_path(desired_parent)
    result = files.update_one(
        {'_id': ObjectId(mongo_id)},
        {'$set': {'parent': desired_parent, 'name': desired_name, 'mtime': now}}
    )
    return result.matched_count == 1

print("=" * 60)
print("FINAL FIXES")
print("=" * 60)

fixed = 0

# 1. Fix Hai to Gensou no Grimgar episodes
print("\n1. Fixing Hai to Gensou no Grimgar episodes...")
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Series/Hai to Gensou no Grimgar'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc['name']
    base, ext = os.path.splitext(name)
    # Extract episode number from the name
    ep_match = re.search(r'S01E(\d+)', name)
    if ep_match:
        ep_num = int(ep_match.group(1))
        new_name = f"Hai to Gensou no Grimgar - S01E{ep_num:02d}{ext}"
    else:
        # Try to extract from the name
        ep_match = re.search(r'(\d+)$', base)
        if ep_match:
            ep_num = int(ep_match.group(1))
            new_name = f"Hai to Gensou no Grimgar - S01E{ep_num:02d}{ext}"
        else:
            new_name = name
    
    if new_name != name:
        if apply_correction(str(doc['_id']), doc['parent'], new_name):
            print(f'  FIXED: {doc["parent"]}/{name} -> {new_name}')
            fixed += 1

# 2. Clean up duplicate movies with "alt" suffix
print("\n2. Cleaning up duplicate movies with 'alt' suffix...")
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}, 'name': {'$regex': r' alt [a-f0-9]+'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc['name']
    # Remove " alt xxxxxx" from filename
    new_name = re.sub(r'\s+alt\s+[a-f0-9]+', '', name)
    if new_name != name:
        if apply_correction(str(doc['_id']), doc['parent'], new_name):
            print(f'  FIXED: {doc["parent"]}/{name} -> {new_name}')
            fixed += 1

# 3. Fix the remaining subtitle garbage (the "filmes com" etc.)
print("\n3. Fixing remaining subtitle garbage...")
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}, 'name': {'$regex': r'\.(srt|pob|ass)$', '$options': 'i'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc['name']
    base, ext = os.path.splitext(name)
    # Remove "filmes com" and similar
    clean = re.sub(r'(?i)\s+-?\s*filmes\s+com\s*$', '', base)
    clean = re.sub(r'(?i)\s+-?\s*filmes\s+com\s+', ' ', clean)
    clean = re.sub(r'(?i)\s+-\s*$', '', clean)
    clean = re.sub(r'\s+', ' ', clean).strip(' -_.')
    new_name = clean + ext
    if new_name != name:
        if apply_correction(str(doc['_id']), doc['parent'], new_name):
            print(f'  FIXED SUB: {doc["parent"]}/{name} -> {new_name}')
            fixed += 1

print(f"\nTotal fixed: {fixed}")

# Regenerate STRM
print("\nRegenerating STRM...")
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)