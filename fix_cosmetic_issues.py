#!/usr/bin/env python3
"""Fix issues introduced by cosmetic cleanup"""

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

def fix_double_parens(raw):
    """Fix double parentheses like ((2002)) -> (2002)"""
    title = raw
    title = re.sub(r'\(\s*\((\d{4})\)\s*\)', r'(\1)', title)
    return title

def fix_missing_words(raw):
    """Fix cases like 'I Sam' -> 'I Am Sam', 'Talk Me' -> 'Talk to Me'"""
    title = raw
    # Fix "I Sam" -> "I Am Sam"
    title = re.sub(r'\bI\s+Sam\b', 'I Am Sam', title, flags=re.IGNORECASE)
    # Fix "Talk Me" -> "Talk to Me"
    title = re.sub(r'\bTalk\s+Me\b', 'Talk to Me', title, flags=re.IGNORECASE)
    # Fix "Dual (" -> "Dual ("
    title = re.sub(r'\bDual\s*\(', 'Dual (', title)
    return title

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
print("FIXING COSMETIC CLEANUP ISSUES")
print("=" * 60)

fixed = 0

# 1. Fix double parentheses in movies
print("\n1. Fixing double parentheses in movies...")
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}, 'name': {'$regex': r'\(\s*\(\d{4}\)\s*\)'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc['name']
    new_name = fix_double_parens(name)
    new_name = fix_missing_words(new_name)
    new_name = safe_name(new_name)
    if new_name != name:
        if apply_correction(str(doc['_id']), doc['parent'], new_name):
            print(f'  FIXED: {doc["parent"]}/{name} -> {new_name}')
            fixed += 1

# 2. Fix double parentheses in subtitles
print("\n2. Fixing double parentheses in subtitles...")
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}, 'name': {'$regex': r'\.(srt|pob|ass)$', '$options': 'i'}, 'name': {'$regex': r'\(\s*\(\d{4}\)\s*\)'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc['name']
    new_name = fix_double_parens(name)
    new_name = safe_name(new_name)
    if new_name != name:
        if apply_correction(str(doc['_id']), doc['parent'], new_name):
            print(f'  FIXED SUB: {doc["parent"]}/{name} -> {new_name}')
            fixed += 1

# 3. Fix series episodes with remaining quality info
print("\n3. Fixing series episodes with quality info...")
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Series/'}, 'name': {'$regex': r'(1080p|720p|DUAL|x264|x265|H264|H265|HEVC|AAC|AC3|DTS|WEB-DL|WEBRip)'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc['name']
    base, ext = os.path.splitext(name)
    # Remove quality suffixes but keep series name and SXXEXX
    clean = re.sub(r'(?i)\s+(720p|1080p|4k|BluRay|WEB-DL|WEB|x264|x265|H264|H265|HEVC|AAC|AC3|DTS|DUAL|5\.1|WEBRip|WEB-DL)\s*$', '', base)
    clean = re.sub(r'(?i)\s+(720p|1080p|4k|BluRay|WEB-DL|WEB|x264|x265|H264|H265|HEVC|AAC|AC3|DTS|DUAL|5\.1|WEBRip|WEB-DL)\b', '', clean)
    clean = re.sub(r'\s+', ' ', clean).strip(' -_.')
    new_name = safe_name(clean) + ext
    if new_name != name:
        if apply_correction(str(doc['_id']), doc['parent'], new_name):
            print(f'  FIXED SERIES: {doc["parent"]}/{name} -> {new_name}')
            fixed += 1

print(f"\nTotal fixed: {fixed}")

# Regenerate STRM
print("\nRegenerating STRM...")
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)