#!/usr/bin/env python3
"""
Fix remaining garbage keywords - now handling multi-word release groups like "BLUDV TO", "COMANDO TO", etc.
"""

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

def clean_title(raw):
    """Remove release groups - now handles multi-word groups"""
    if not raw:
        return ''
    title = raw.strip()
    
    # Multi-word release groups to remove (with various separators)
    multi_groups = [
        'BLUDV TO', 'BLUDV COM', 'BLUDV TV',
        'COMANDO TO', 'COMANDO LA', 'COMANDO TORRENTS',
        'WWW BLUDV', 'WWW BLUDV COM', 'WWW BLUDV TV',
        'TORRENTDOSFILMES TO', 'TORRENTDOSFILMES TV',
        'WOLVERDON FILMES', 'LAPUMIA ORG', 'LAPUMIA FILMES',
        'GALAXY RG', 'GALAXY TV', 'RARBG', 'PSA',
        'THE PIRATE FILMES', 'COMANDO TORRENTS',
        'THE PIRATE FILMES', 'TIO KENNEDY', 'BOKU TOX',
        'TORRENT DOS FILMES', 'COMANDO TORRENTS',
        'ENGLISH SDH', 'ENGLISH', 'SDH', 'YIFY', 'TDF',
        'FORCED', 'BAIXAR', 'BAIXE', 'ACESSE',
        'WOLVERDON FILMES', 'LAPUMIA FILMES', 'LAPUMIA',
        'GALAXY RG', 'GALAXY TV', 'THE PIRATE FILMES'
    ]
    
    title = raw.strip()
    
    # Remove multi-word groups first
    for group in multi_groups:
        # Match at start with separator
        title = re.sub(rf'(?i)^{re.escape(group)}[\s_\-\.]+', '', title)
        # Match anywhere as whole phrase
        title = re.sub(rf'(?i)\b{re.escape(group)}\b', '', title)
    
    # Single word groups
    single_groups = [
        'YIFY', 'BLUDV', 'COMANDO', 'WOLVERDON', 'LAPUMIA', 'GALAXYRG', 'GALAXYTV',
        'RARBG', 'PSA', 'THEPIRATEFILMES', 'TORRENTDOSFILMES', 'COMANDOTORRENTS',
        'BLUDV', 'WOLVERDON', 'LAPUMIA', 'GALAXYRG', 'GALAXYTV', 'RARBG', 'PSA',
        'THEPIRATEFILMES', 'TORRENTDOSFILMES', 'COMANDOTORRENTS',
        'BLUDV', 'WWW', 'ENGLISH', 'SDH', 'TDF', 'FORCED',
        'BAIXAR', 'BAIXE', 'ACESSE', 'COMANDO', 'TORRENTS',
        'WOLVERDON', 'LAPUMIA', 'GALAXYRG', 'GALAXYTV', 'RARBG', 'PSA',
        'THEPIRATEFILMES', 'TORRENTDOSFILMES', 'COMANDOTORRENTS',
        'WWW', 'ENGLISH', 'SDH', 'TDF', 'FORCED',
        'BAIXAR', 'BAIXE', 'ACESSE', 'COMANDO', 'TORRENTS',
        'WOLVERDON', 'LAPUMIA', 'GALAXYRG', 'GALAXYTV', 'RARBG', 'PSA',
        'THEPIRATEFILMES', 'TORRENTDOSFILMES', 'COMANDOTORRENTS'
    ]
    
    for group in single_groups:
        title = re.sub(rf'(?i)\b{re.escape(group)}\b', '', title)
    
    # Clean separators
    title = re.sub(r'[._]+', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip(' -_.')
    
    # Remove quality/codec suffixes
    SUFFIX_PATTERNS = [
        r'(?i)\s+(?:720p|1080p|4k|2160p|bdrip|brrip|webrip|web-dl|webdl|hdtv|x264|x265|h264|h265|hevc|aac|ac3|ddp?5\.?1?|dts|truehd|atmos|dd\+?)\s*$',
        r'(?i)\s+(?:bluray|web|web\.|remux|repack|proper|internal|limited|extended|uncut|directors?\.?cut|theatrical)\s*$',
        r'(?i)\s+(?:dual|dublado|legendado|portuguese|portugues|ptbr|pt-br|eng|english|spa|spanish)\s*$',
        r'(?i)\s+[-_\.]+$',
    ]
    for pattern in SUFFIX_PATTERNS:
        title = re.sub(pattern, '', title)
    
    title = re.sub(r'\s+', ' ', title).strip(' -_.')
    return title

def safe_name(name):
    name = re.sub(r'[<>:\\"/\\|?*]+', ' - ', name)
    name = re.sub(r'\s+', ' ', name).strip(' .-')
    return name[:180]

def extract_year(title):
    match = re.search(r'\b(19|20)\d{2}\b', title)
    return int(match.group(0)) if match else None

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
        quar_parent = f'/raphael/Auditoria/Duplicatas{target_parent.replace("/raphael", "")}'
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

# Find and fix remaining garbage
suspects = []
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/(Filmes|Series)'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    path = f"{doc['parent']}/{doc['name']}"
    mid = str(doc['_id'])
    
    if re.search(r'(?i)(ACESSE|COMANDO|BLUDV|WOLVERDON|WWW\.|BAIXE|SDH|YIFY|TDF|LAPUMIA|GALAXYRG|GALAXYTV|RARBG|PSA|LAPUMIAFILMES|THEPIRATEFILMES|TORRENTDOSFILMES|COMANDOTORRENTS|COMANDOTORRENTS\.COM|BLUDV\.TO|BLUDV\.COM|WOLVERDONFILMES|COMANDO\.LA|COMANDO\.TO|WWW\.BLUDV|ENGLISH\s+SDH|SCORP|FORCED|BAIXAR|EP\s+\d+|ACESSE\s+COMANDO|BAIXE\s+OUTROS|COMANDO\.TO|COMANDO\.LA|BLUDV\.TO|BLUDV\.COM)', path):
        suspects.append({'id': mid, 'path': path, 'name': doc['name'], 'parent': doc['parent']})

print(f"Fixing {len(suspects)} remaining garbage items...")

applied = 0
for s in suspects:
    name = s['name']
    parent = s['parent']
    base_name, ext = os.path.splitext(name)
    
    # Clean the filename
    clean = clean_title(base_name)
    if clean != base_name:
        desired_name = safe_name(clean) + ext
        desired_parent = parent
        
        # Also clean parent directory name if it has garbage
        parent_parts = parent.strip('/').split('/')
        clean_parts = []
        for part in parent_parts:
            if part and part != 'raphael':
                clean_part = clean_title(part)
                if clean_part != part:
                    clean_parts.append(safe_name(clean_part))
                else:
                    clean_parts.append(part)
            else:
                clean_parts.append(part)
        
        if clean_parts != parent_parts[1:]:
            desired_parent = '/' + '/'.join(['raphael'] + clean_parts)
        
        if apply_correction(s['id'], desired_parent, desired_name):
            applied += 1
            print(f'  FIXED: {s["path"]} -> {desired_parent}/{desired_name}')

# Also fix parent directories that have garbage in their names
print("\nFixing parent directory names...")
movie_dirs = list(files.find({'type': 'dir', 'parent': {'$regex': '^/raphael/Filmes/'}}, {'_id': 1, 'name': 1, 'parent': 1}))
for doc in movie_dirs:
    clean = clean_title(doc['name'])
    if clean != doc['name']:
        new_name = safe_name(clean)
        new_parent = f"{doc['parent']}/{safe_name(clean)}"
        ensure_path(new_parent)
        
        # Move all files in this directory
        dir_files = list(files.find({'type': 'file', 'parent': f"{doc['parent']}/{doc['name']}"}, {'_id': 1, 'name': 1}))
        for f in dir_files:
            apply_correction(str(f['_id']), new_parent, f['name'])
        
        print(f'  FIXED DIR: {doc["parent"]}/{doc["name"]} -> {new_parent}')

print(f"\nTotal applied fixes")

# Regenerate STRM
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)