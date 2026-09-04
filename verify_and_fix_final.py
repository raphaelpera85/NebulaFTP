#!/usr/bin/env python3
"""Final verification and cleanup of remaining issues"""

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
    """Clean filename - remove quality info, release groups, etc."""
    if not raw:
        return ''
    title = raw.strip()
    
    # Remove quality/codec suffixes
    SUFFIX_PATTERNS = [
        r'(?i)\s+(?:720p|1080p|4k|2160p|bdrip|brrip|webrip|web-dl|webdl|hdtv|x264|x265|h264|h265|hevc|aac|ac3|ddp?5\.?1?|dts|truehd|atmos|dd\+?)\s*$',
        r'(?i)\s+(?:bluray|web|web\.|remux|repack|proper|internal|limited|extended|uncut|directors?\.?cut|theatrical)\s*$',
        r'(?i)\s+(?:dual|dublado|legendado|portuguese|portugues|ptbr|pt-br|eng|english|spa|spanish)\s*$',
        r'(?i)\s+[-_\.]+$',
        r'(?i)\s+[-\s]+$',
    ]
    for pattern in SUFFIX_PATTERNS:
        title = re.sub(pattern, '', title)
    
    # Remove release groups
    RELEASE_GROUPS = [
        'YIFY', 'BLUDV', 'COMANDO', 'WOLVERDON', 'LAPUMIA', 'GALAXYRG', 'GALAXYTV',
        'RARBG', 'PSA', 'THEPIRATEFILMES', 'TORRENTDOSFILMES', 'COMANDOTORRENTS',
        'WWW', 'ENGLISH', 'SDH', 'TDF', 'FORCED', 'BAIXAR', 'BAIXE', 'ACESSE',
        'TO', 'TV', 'LA', 'COM', 'YTS', 'AM', 'MX', 'AG', 'MLD',
        'BOKUTOX', 'TIOKENNEDY', 'TIO', 'KENNEDY',
    ]
    for group in RELEASE_GROUPS:
        title = re.sub(rf'(?i)\b{re.escape(group)}\b', '', title)
    
    # Clean separators
    title = re.sub(r'[._]+', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip(' -_.')
    
    # Remove trailing dash
    title = re.sub(r'\s+-\s*$', '', title)
    
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
print("FINAL CLEANUP - REMAINING ISSUES")
print("=" * 60)

fixed = 0

# 1. Fix duplicate year in movie filenames (2023 - (2023) -> (2023))
print("\n1. Fixing duplicate year in movie filenames...")
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc['name']
    # Fix "Title 2023 - (2023).mkv" -> "Title (2023).mkv"
    new_name = re.sub(r'\s+\d{4}\s*-\s*\(\d{4}\)', ' (\g<0>)', name)  # This won't work as expected
    # Better: remove the first year occurrence before the dash
    new_name = re.sub(r'^(.+?)\s+\d{4}\s*-\s*(\(\d{4}\))\s*(\.\w+)$', r'\1 \2\3', name)
    if new_name != name:
        new_name = safe_name(new_name)
        if apply_correction(str(doc['_id']), doc['parent'], new_name):
            print(f'  FIXED: {doc["parent"]}/{name} -> {new_name}')
            fixed += 1

# 2. Fix subtitle files with quality info
print("\n2. Fixing subtitle files with quality info...")
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}, 'name': {'$regex': r'\.(srt|pob|ass)$', '$options': 'i'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc['name']
    base, ext = os.path.splitext(name)
    clean = clean_filename(base)
    new_name = safe_name(clean) + ext
    if new_name != name:
        if apply_correction(str(doc['_id']), doc['parent'], new_name):
            print(f'  FIXED SUB: {doc["parent"]}/{name} -> {new_name}')
            fixed += 1

# 3. Fix series with TO prefix
print("\n3. Fixing series with TO prefix...")
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Series/'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc['name']
    if name.startswith('TO - '):
        new_name = name[5:]  # Remove "TO - "
        new_name = clean_filename(os.path.splitext(new_name)[0]) + os.path.splitext(name)[1]
        new_name = safe_name(new_name)
        if apply_correction(str(doc['_id']), doc['parent'], new_name):
            print(f'  FIXED SERIES: {doc["parent"]}/{name} -> {new_name}')
            fixed += 1

# 4. Fix garbage series directory names
print("\n4. Fixing garbage series directory names...")
garbage_series_dirs = [
    '12X10 - BAIXE OUTROS EPS. NO COMANDOTORRENTS.COM',
    '12X10 - OUTROS EPS NO COM',
    '12x01 - [ OUTROS EPS NO COM]',
    '12x01 - [BAIXE OUTROS EPS. NO COMANDOTORRENTS.COM]',
]

for dir_name in garbage_series_dirs:
    dir_doc = files.find_one({'type': 'dir', 'parent': '/raphael/Series', 'name': dir_name})
    if dir_doc:
        # Determine the correct series name
        if '12X10' in dir_name or '12x01' in dir_name:
            new_dir_name = 'Dexter'  # Based on the episode pattern
        else:
            new_dir_name = clean_filename(dir_name)
        
        new_dir_name = safe_name(new_dir_name)
        old_path = f'/raphael/Series/{dir_name}'
        new_path = f'/raphael/Series/{new_dir_name}'
        
        # Move all files in this directory (and subdirectories)
        dir_files = list(files.find({'parent': {'$regex': f'^{re.escape(old_path)}'}}, {'_id': 1, 'name': 1, 'parent': 1, 'type': 1}))
        for f in dir_files:
            if f.get('type') == 'dir':
                continue  # Skip directories for now
            # Clean the filename too
            base, ext = os.path.splitext(f['name'])
            clean = clean_filename(base)
            new_name = safe_name(clean) + ext
            
            # Calculate new parent path
            rel_parent = f['parent'][len(old_path):].lstrip('/')
            new_parent = f'{new_path}/{rel_parent}' if rel_parent else new_path
            
            if apply_correction(str(f['_id']), new_parent, new_name):
                print(f'  MOVED FILE: {f["parent"]}/{f["name"]} -> {new_parent}/{new_name}')
                fixed += 1
        
        print(f'  FIXED DIR: {dir_name} -> {new_dir_name}')

# 5. Fix A Bruxa directory
print("\n5. Fixing A Bruxa directory...")
dir_doc = files.find_one({'type': 'dir', 'parent': '/raphael/Filmes', 'name': {'$regex': 'A Bruxa.*1080p'}})
if dir_doc:
    old_name = dir_doc['name']
    new_name = 'A Bruxa (2016)'
    old_path = f'/raphael/Filmes/{old_name}'
    new_path = f'/raphael/Filmes/{new_name}'
    
    dir_files = list(files.find({'parent': old_path}, {'_id': 1, 'name': 1}))
    for f in dir_files:
        base, ext = os.path.splitext(f['name'])
        clean = clean_filename(base)
        new_fname = safe_name(clean) + ext
        if apply_correction(str(f['_id']), new_path, new_fname):
            print(f'  MOVED FILE: {old_path}/{f["name"]} -> {new_path}/{new_fname}')
            fixed += 1
    print(f'  FIXED DIR: {old_name} -> {new_name}')

# 6. Fix Alexander directory (has extra [The Final Cut] in dir but not in name)
print("\n6. Fixing Alexander directory...")
dir_doc = files.find_one({'type': 'dir', 'parent': '/raphael/Filmes', 'name': {'$regex': 'Alexander.*Final Cut'}})
if dir_doc:
    old_name = dir_doc['name']
    # Should be "Alexander [Revisited The Final Cut] (2004)"
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