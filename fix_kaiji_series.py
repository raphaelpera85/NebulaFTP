#!/usr/bin/env python3
"""
Fix Kaiji series episodes and other series-in-Filmes items.
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

def extract_season_episode(title):
    m = re.search(r'(?i)S(\d{1,2})[._\-\s]*E(\d{1,3})', title)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r'(?i)(\d{1,2})x(\d{1,3})', title)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r'(?i)[\-\s](\d{1,2})\s*\-\s*(?:Manifesta|Ascen|Desmoron|Martelo|Renascimento|Mensageiro|Alimentando|Assombra|Fria|Conversa|Passatempo|Limite|Almas|Execu|Incomum|Condi|Pulido|Amanhecer|Partida|Abrir|Desespero)', title)
    return None, None

def safe_name(name):
    name = re.sub(r'[<>:\\"/\\|?*]+', ' - ', name)
    name = re.sub(r'\s+', ' ', name).strip(' .-')
    return name[:180]

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

# Fix Kaiji series episodes
kaiji_files = list(files.find({
    'type': 'file', 'status': 'completed',
    'parent': {'$regex': '^/raphael/Filmes/Kaiji|^/raphael/Filmes/Gyakkyou Burai Kaiji'}
}, {'_id': 1, 'name': 1, 'parent': 1}))

print(f'Found {len(kaiji_files)} Kaiji files')
applied = 0

for doc in kaiji_files:
    name = doc['name']
    base_name, ext = os.path.splitext(name)
    
    # Try to extract episode number
    ep_match = re.search(r'(?i)[\-\s](\d{1,2})\s*\-\s*(?:Manifesta|Ascen|Desmoron|Martelo|Renascimento|Mensageiro|Alimentando|Assombra|Fria|Conversa|Passatempo|Limite|Almas|Execu|Incomum|Condi|Pulido|Amanhecer|Partida|Abrir|Desespero)', name)
    if ep_match:
        episode = int(ep_match.group(1))
    else:
        ep_match = re.search(r'[\-\s](\d{1,2})\s*[\-\s]', name)
        if ep_match:
            episode = int(ep_match.group(1))
        else:
            continue
    
    # Determine season (all seem to be Season 01 for Kaiji)
    season = 1
    
    desired_parent = "/raphael/Series/Kaiji/Season 01"
    desired_name = f"Kaiji - S{season:02d}E{episode:02d}{os.path.splitext(name)[1]}"
    
    if apply_correction(doc['_id'], desired_parent, desired_name):
        applied += 1
        print(f'  MOVED: {doc["parent"]}/{name} -> {desired_name}')

print(f'\nApplied: {applied}')

# Regenerate STRM
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)