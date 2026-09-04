#!/usr/bin/env python3
"""Final cosmetic cleanup - remove quality info from movie/series filenames and subtitles"""

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

def clean_filename(raw):
    """Clean filename - remove quality info, release groups, etc. Keep year if present."""
    if not raw:
        return ''
    title = raw.strip()
    
    # Remove quality/codec suffixes (but keep year)
    SUFFIX_PATTERNS = [
        r'(?i)\s+(?:720p|1080p|4k|2160p|bdrip|brrip|webrip|web-dl|webdl|hdtv|x264|x265|h264|h265|hevc|aac|ac3|ddp?5\.?1?|dts|truehd|atmos|dd\+?)\s*$',
        r'(?i)\s+(?:bluray|web|web\.|remux|repack|proper|internal|limited|extended|uncut|directors?\.?cut|theatrical)\s*$',
        r'(?i)\s+(?:dual|dublado|legendado|portuguese|portugues|ptbr|pt-br|eng|english|spa|spanish)\s*$',
        r'(?i)\s+[-_\.]+$',
        r'(?i)\s+[-\s]+$',
        r'(?i)\s+[-\s]+(?:5\.1|7\.1|2\.0|5\.1ch|7\.1ch)\s*$',
    ]
    for pattern in SUFFIX_PATTERNS:
        title = re.sub(pattern, '', title)
    
    # Remove release groups (but keep year)
    RELEASE_GROUPS = [
        'YIFY', 'BLUDV', 'COMANDO', 'WOLVERDON', 'LAPUMIA', 'GALAXYRG', 'GALAXYTV',
        'RARBG', 'PSA', 'THEPIRATEFILMES', 'TORRENTDOSFILMES', 'COMANDOTORRENTS',
        'WWW', 'ENGLISH', 'SDH', 'TDF', 'FORCED', 'BAIXAR', 'BAIXE', 'ACESSE',
        'TO', 'TV', 'LA', 'COM', 'YTS', 'MLD', 'AM', 'MX', 'AG', 'BOKUTOX',
        'TIOKENNEDY', 'TIO', 'KENNEDY', 'MIRCREW', 'NUITA', 'SP33DY94', 'SP33DY',
        'IMAX', 'DSNP', 'DDP', 'WEB-DL', 'WEB', 'BRRIP', 'HDR', 'DV', 'HDR10',
        'DSNP', 'AMZ', 'NF', 'DSNP',
    ]
    for group in RELEASE_GROUPS:
        title = re.sub(rf'(?i)\b{re.escape(group)}\b', '', title)
    
    # Clean separators
    title = re.sub(r'[._]+', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip(' -_.')
    
    # Remove trailing dash
    title = re.sub(r'\s+-\s*$', '', title)
    
    # Remove extra spaces around year
    title = re.sub(r'\s+\(\d{4}\)\s*$', r' (\g<0>)', title)
    
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
print("FINAL COSMETIC CLEANUP - QUALITY INFO IN NAMES")
print("=" * 60)

fixed = 0

# 1. Fix movie files with quality info
print("\n1. Fixing movie files with quality info...")
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}, 'name': {'$regex': r'\.mkv$', '$options': 'i'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc['name']
    if re.search(r'(?i)(720p|1080p|4k|BluRay|WEB-DL|WEB|x264|x265|H264|H265|HEVC|AAC|AC3|DTS|DUAL|5\.1|IMAX|DSNP|AMZ|DDP)', name):
        base, ext = os.path.splitext(name)
        clean = clean_filename(base)
        new_name = safe_name(clean) + ext
        if new_name != name:
            if apply_correction(str(doc['_id']), doc['parent'], new_name):
                print(f'  FIXED: {doc["parent"]}/{name} -> {new_name}')
                fixed += 1

# 2. Fix subtitle files
print("\n2. Fixing subtitle files...")
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}, 'name': {'$regex': r'\.(srt|pob|ass)$', '$options': 'i'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc['name']
    base, ext = os.path.splitext(name)
    clean = clean_filename(base)
    new_name = safe_name(clean) + ext
    if new_name != name:
        if apply_correction(str(doc['_id']), doc['parent'], new_name):
            print(f'  FIXED SUB: {doc["parent"]}/{name} -> {new_name}')
            fixed += 1

# 3. Fix series files with quality info
print("\n3. Fixing series files with quality info...")
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Series/'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc['name']
    if re.search(r'(?i)(720p|1080p|4k|BluRay|WEB-DL|WEB|x264|x265|H264|H265|HEVC|AAC|AC3|DTS|DUAL|WEBRip|WEB-DL)', name):
        base, ext = os.path.splitext(name)
        clean = clean_filename(base)
        new_name = safe_name(clean) + ext
        if new_name != name:
            if apply_correction(str(doc['_id']), doc['parent'], new_name):
                print(f'  FIXED SERIES: {doc["parent"]}/{name} -> {new_name}')
                fixed += 1

# 4. Fix Alexander directory
print("\n4. Fixing Alexander directory...")
dir_doc = files.find_one({'type': 'dir', 'parent': '/raphael/Filmes', 'name': {'$regex': 'Alexander.*Final Cut'}})
if dir_doc:
    old_name = dir_doc['name']
    if 'Revisited' not in old_name:
        new_name = 'Alexander [Revisited The Final Cut] (2004)'
        old_path = f'/raphael/Filmes/{old_name}'
        new_path = f'/raphael/Filmes/{new_name}'
        
        dir_files = list(files.find({'parent': old_path}, {'_id': 1, 'name': 1}))
        for f in dir_files:
            if apply_correction(str(f['_id']), new_path, f['name']):
                print(f'  MOVED FILE: {old_path}/{f["name"]} -> {new_path}/{f["name"]}')
                fixed += 1
        print(f'  FIXED DIR: {old_name} -> {new_name}')

print(f"\nTotal fixed: {fixed}")

# Regenerate STRM
print("\nRegenerating STRM...")
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)