#!/usr/bin/env python3
"""
Final cleanup: remove release group names from filenames (YIFY, BLUDV, COMANDO, etc.)
and fix movie directories without year in parent.
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

def clean_title(raw: str) -> str:
    if not raw:
        return ''
    title = raw.strip()
    RELEASE_PREFIXES = [
        r'(?i)^(?:galaxyrg(?:265)?|galaxytv|rarbg|psa|yts(?:\.[a-z]+)?|comando(?:\.la|to)?|bludv(?:\.to|tv)?|thepiratefilmes|dual|dublado|legendado|portuguese|720p|1080p|4k|brrip|webrip|videotrack|audiotrack|ingles|espanhol)\s*[-_\.]\s*',
        r'(?i)^(?:www\.[^_\-\s]+|by\s+.+|acesse\s+.+)\s*[-_\.]\s*',
        r'(?i)^encoded by [^-]+-\s*',
    ]
    for pattern in RELEASE_PREFIXES:
        title = re.sub(pattern, '', title)
    title = re.sub(r'[._]+', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip(' -_.')
    
    # SUFFIX_PATTERNS - now includes release groups as suffixes
    SUFFIX_PATTERNS = [
        r'(?i)\s+(?:720p|1080p|4k|2160p|bdrip|brrip|webrip|web-dl|webdl|hdtv|x264|x265|h264|h265|hevc|aac|ac3|ddp?5\.?1?|dts|truehd|atmos|dd\+?)\s*$',
        r'(?i)\s+(?:bluray|web|web\.|remux|repack|proper|internal|limited|extended|uncut|directors?\.?cut|theatrical)\s*$',
        r'(?i)\s+(?:dual|dublado|legendado|portuguese|portugues|ptbr|pt-br|eng|english|spa|spanish)\s*$',
        r'(?i)\s+[-_\.]+$',
        # Release groups as suffixes (case insensitive)
        r'(?i)\s+(?:yify|bludv|comando|wolverdon|tiokennedy|bokutox|galaxyrg|galaxytv|rarbg|psa|thepiratefilmes)\s*$',
    ]
    for pattern in SUFFIX_PATTERNS:
        title = re.sub(pattern, '', title)
    title = re.sub(r'\s+', ' ', title).strip(' -_.')
    return title

def safe_name(name: str) -> str:
    name = re.sub(r'[<>:\\"/\\|?*]+', ' - ', name)
    name = re.sub(r'\s+', ' ', name).strip(' .-')
    return name[:180]

def extract_year(title: str):
    match = re.search(r'\b(19|20)\d{2}\b', title)
    return int(match.group(0)) if match else None

# Fix filenames with garbage in them (in correct directories)
garbage_main = list(files.find({'parent': {'$regex': '^/raphael/(Filmes|Series)/'}}, {'_id': 1, 'name': 1, 'parent': 1}))
corrections = []

for g in garbage_main:
    name = g['name']
    parent = g['parent']
    base_name, ext = os.path.splitext(name)
    
    # Check if name has garbage patterns (release groups, etc.)
    if re.search(r'(?i)(?:YIFY|BLUDV|COMANDO|WOLVERDON|WWW\.|BAIXE|SDH|TDF|COMANDO\.LA|COMANDO\.TO|BLUDV\.TO|BLUDV\.COM|TIOKENNEDY|BOKUTOX|GALAXYRG|GALAXYTV|RARBG|PSA|THEPIRATEFILMES)', base_name):
        clean = clean_title(base_name)
        if clean != base_name:
            desired_name = safe_name(clean) + ext
            if desired_name != name:
                corrections.append({
                    'mongo_id': str(g['_id']),
                    'desired_parent': parent,
                    'desired_name': desired_name,
                    'current_parent': parent,
                    'current_name': name,
                    'sources': ['filename_cleanup'],
                    'priority': 5
                })

print(f'Filename corrections needed: {len(corrections)}')

# Apply
applied = 0
for c in corrections:
    mid = c['mongo_id']
    doc = files.find_one({'_id': ObjectId(mid)}, {'parent': 1, 'name': 1})
    if not doc:
        continue
    current = f'{doc["parent"]}/{doc["name"]}'
    desired = f'{c["desired_parent"]}/{c["desired_name"]}'
    if current.lower() == desired.lower():
        continue
    
    quarantine_existing(c['desired_parent'], c['desired_name'])
    ensure_path(c['desired_parent'])
    result = files.update_one(
        {'_id': ObjectId(mid)},
        {'$set': {'parent': c['desired_parent'], 'name': c['desired_name'], 'mtime': now}}
    )
    if result.matched_count == 1:
        applied += 1
        if applied % 50 == 0 or applied <= 10:
            print(f'  [{applied}] {current} -> {desired}')

print(f'\nTotal applied: {applied}')

result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)