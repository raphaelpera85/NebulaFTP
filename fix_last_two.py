#!/usr/bin/env python3
"""Fix the last two LAPUMiA items"""

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
        'TORRENTDOSFILMES TO', 'TORRENTDOSFILMES TV',
        'WOLVERDON FILMES', 'LAPUMIA FILMES',
        'GALAXY RG', 'GALAXY TV', 'RARBG', 'PSA',
        'THE PIRATE FILMES', 'COMANDO TORRENTS',
        'TIO KENNEDY', 'BOKU TOX',
        'TORRENT DOS FILMES', 'COMANDO TORRENTS',
        'ENGLISH SDH', 'ENGLISH', 'SDH', 'YIFY', 'TDF',
        'FORCED', 'BAIXAR', 'BAIXE', 'ACESSE',
        'WOLVERDON FILMES', 'LAPUMIA FILMES', 'LAPUMIA',
        'GALAXY RG', 'GALAXY TV', 'THE PIRATE FILMES'
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

# Main
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
        'TORRENTDOSFILMES TO', 'TORRENTDOSFILMES TV',
        'WOLVERDON FILMES', 'LAPUMIA FILMES',
        'GALAXY RG', 'GALAXY TV', 'RARBG', 'PSA',
        'THE PIRATE FILMES', 'COMANDO TORRENTS',
        'TIO KENNEDY', 'BOKU TOX',
        'TORRENT DOS FILMES', 'COMANDO TORRENTS',
        'ENGLISH SDH', 'ENGLISH', 'SDH', 'YIFY', 'TDF',
        'FORCED', 'BAIXAR', 'BAIXE', 'ACESSE',
        'WOLVERDON FILMES', 'LAPUMIA FILMES', 'LAPUMIA',
        'GALAXY RG', 'GALAXY TV', 'THE PIRATE FILMES'
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

def clean_title(raw):
    if not raw:
        return ''
    title = raw.strip()
    
    multi_groups = [
        'BLUDV TO', 'BLUDV COM', 'BLUDV TV',
        'COMANDO TO', 'COMANDO LA', 'COMANDO TORRENTS',
        'WWW BLUDV', 'WWW BLUDV COM', 'WWW BLUDV TV',
        'TORRENTDOSFILMES TO', 'TORRENTDOSFILMES TV',
        'WOLVERDON FILMES', 'LAPUMIA FILMES',
        'GALAXY RG', 'GALAXY TV', 'RARBG', 'PSA',
        'THE PIRATE FILMES', 'COMANDO TORRENTS',
        'TIO KENNEDY', 'BOKU TOX',
        'TORRENT DOS FILMES', 'COMANDO TORRENTS',
        'ENGLISH SDH', 'ENGLISH', 'SDH', 'YIFY', 'TDF',
        'FORCED', 'BAIXAR', 'BAIXE', 'ACESSE',
        'WOLVERDON FILMES', 'LAPUMIA FILMES', 'LAPUMIA',
        'GALAXY RG', 'GALAXY TV', 'THE PIRATE FILMES'
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

# Fix the two remaining items
items_to_fix = [
    ('6a76659a54e0892ddf9d7653', '/raphael/Filmes/LAPUMiA Org/Org.mkv'),
    ('6a76659a54e0892ddf9d766e', '/raphael/Filmes/LAPUMiAFiLMES COM/FiLMES COM.mkv'),
]

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

for mid in ['6a76659a54e0892ddf9d7653', '6a76659a54e0892ddf9d766e']:
    doc = files.find_one({'_id': ObjectId(mid)}, {'parent': 1, 'name': 1})
    if not doc:
        print(f'NOT FOUND: {mid}')
        continue
    
    name = doc['name']
    parent = doc['parent']
    base_name, ext = os.path.splitext(name)
    
    # Clean the filename
    clean = clean_title(base_name)
    desired_name = safe_name(clean) + ext
    
    # Clean the parent directory
    parent = doc['parent']
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
    else:
        desired_parent = parent
    
    desired = f'{desired_parent}/{safe_name(clean_title(name))}{ext}'
    current = f'{parent}/{name}'
    
    print(f'Current: {current}')
    print(f'Desired: {desired_parent}/{safe_name(clean_title(name))}{ext}')
    
    if apply_correction(mid, desired_parent, safe_name(clean_title(name)) + ext):
        print(f'FIXED: {mid}')
    else:
        print(f'FAILED: {mid}')

# Regenerate STRM
import subprocess
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)