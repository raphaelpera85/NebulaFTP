#!/usr/bin/env python3
"""Fix the last two directories with TorrentDosFilmes2"""

import os
import re
import time
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

def clean_title(raw):
    if not raw:
        return ''
    title = raw.strip()
    
    multi_groups = [
        'BLUDV TO', 'BLUDV COM', 'BLUDV TV',
        'COMANDO TO', 'COMANDO LA', 'COMANDO TORRENTS',
        'WWW BLUDV', 'WWW BLUDV COM', 'WWW BLUDV TV',
        'TORRENTDOSFILMES TO', 'TORRENTDOSFILMES TV', 'TORRENTDOSFILMES2', 'TORRENTDOSFILMES 2',
        'WOLVERDON FILMES', 'LAPUMIA FILMES', 'LAPUMIAFILMES', 'LAPUMIAFILMES COM', 'LAPUMIA COM', 'LAPUMIAFILMES COM',
        'GALAXY RG', 'GALAXY TV', 'RARBG', 'PSA',
        'THE PIRATE FILMES', 'COMANDO TORRENTS',
        'TIO KENNEDY', 'BOKU TOX',
        'TORRENT DOS FILMES', 'COMANDO TORRENTS',
        'ENGLISH SDH', 'ENGLISH', 'SDH', 'YIFY', 'TDF',
        'FORCED', 'BAIXAR', 'BAIXE', 'ACESSE',
        'WOLVERDON FILMES', 'LAPUMIA FILMES', 'LAPUMIA',
        'GALAXY RG', 'GALAXY TV', 'THE PIRATE FILMES',
        'FILMES COM', 'FILMES COM MKV', 'FILEMES COM'
    ]
    
    title = raw.strip()
    for group in multi_groups:
        title = re.sub(rf'(?i)^{re.escape(group)}[\s_\-\.]+', '', title)
        title = re.sub(rf'(?i)\b{re.escape(group)}\b', '', title)
    
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
        'THEPIRATEFILMES', 'TORRENTDOSFILMES', 'COMANDOTORRENTS',
        'WWW', 'ENGLISH', 'SDH', 'TDF', 'FORCED',
        'BAIXAR', 'BAIXE', 'ACESSE', 'COMANDO', 'TORRENTS',
        'TO', 'TV', 'LA',
    ]
    
    for group in single_groups:
        title = re.sub(rf'(?i)\b{re.escape(group)}\b', '', title)
    
    title = re.sub(r'[._]+', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip(' -_.')
    
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

# Fix directories
dirs_to_fix = [
    '/raphael/Filmes/Eduardo e Mônica 2021 - TorrentDosFilmes2 (2021)',
    '/raphael/Filmes/O Último Jogo 2021 - TorrentDosFilmes2 (2021)'
]

for dir_path in dirs_to_fix:
    dir_doc = files.find_one({'type': 'dir', 'parent': '/raphael/Filmes', 'name': os.path.basename(dir_path)})
    if not dir_doc:
        print(f'Directory not found: {dir_path}')
        continue
    
    clean_dir = clean_title(dir_doc['name'])
    new_dir_name = safe_name(clean_dir)
    new_dir_path = f'/raphael/Filmes/{new_dir_name}'
    
    print(f'Dir: {dir_doc["name"]} -> {new_dir_name}')
    
    # Move files in this directory
    dir_files = list(files.find({'parent': dir_path}, {'_id': 1, 'name': 1}))
    for f in dir_files:
        name = f['name']
        base_name, ext = os.path.splitext(name)
        clean = clean_title(base_name)
        new_name = safe_name(clean) + ext
        
        print(f'  File: {name} -> {new_name}')
        if apply_correction(str(f['_id']), new_dir_path, new_name):
            print(f'    FIXED')
        else:
            print(f'    FAILED')
    
    print()

# Regenerate STRM
import subprocess
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)