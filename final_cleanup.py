#!/usr/bin/env python3
"""Final cleanup of movie_no_year_dir - handle Bleach episodes and garbage entries"""

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

def extract_year(title):
    match = re.search(r'\b(19|20)\d{2}\b', title)
    return int(match.group(0)) if match else None

def extract_season_episode(title):
    m = re.search(r'(?i)S(\d{1,2})[._\-\s]*E(\d{1,3})', title)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r'(?i)(\d{1,2})x(\d{1,3})', title)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None

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
print("FINAL CLEANUP - MOVIE_NO_YEAR_DIR")
print("=" * 60)

moved_to_series = 0
added_year = 0
quarantined = 0

# Garbage parent directory names that should be quarantined
garbage_parents = [
    '/', '/()', '/en', '/TV', '/LA', '/TO', '/COM', '/Org', '/Video',
    '/MiNiX', '/Portuguese BR - 3LT0N', '/Stereo', '/5 1 Surround',
    '/BLUREI COM - By-LuanHarper', '/Amor Doentio', '/A Pele Que Habito',
    '/Deu a Louca na História - O Filme', '/Minha Mãe é Uma Peça 3',
    '/Justice League', '/Sons of Anarchy S06 - By-LuanHarper - The Pirate Filmes',
    '/The Big Bang Theory S08 [720p] - By-LuanHarper',
    '/The Big Bang Theory Season 9 - By-LuanHarper - -Orion - THE PIRATE FILMES',
    '/Inglês', '/Japonês', '/Cara Alto e Gostoso', '/Cavalos no Céu',
    '/Veja-os Partir', '/Cigarros, uísque, um prado e você',
]

# Bleach episode patterns
bleach_patterns = [
    'Hisatsu no ichigeki', 'Nerawareta Orihime', 'Toppaseyo', 'Tachihadagaru Renji',
    'Aizen ansatsu', 'Fushijin no Otoko', 'Saikai Ichigo', 'Shitou kecchaku',
    'Shokei no asa', 'Senbonzakura funsai', 'Rukia no ketsui', 'Shunjin Yoruichi',
    'Zetsubou no Shinjitsu', 'Genkai wo koero', 'Adautsu monotachi',
    'Ishida kyugen', 'Rukia no akumu', 'Shinobi yoru kyoufu', 'BAUNTO',
    'Toppa seyo', 'Rukia no kikan', 'Hitsugaya ugoku', 'Shinigami to Kuinshii',
    'Bounto Kyoushuu', 'Ishida Genkai', 'Saigo no Kuinshii', 'Totsunyuu Shinigami',
]

for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    parent = doc.get('parent', '')
    name = doc.get('name', '')
    
    # Skip if already has year in parent
    if re.search(r'\(\d{4}\)', parent):
        continue
    
    # Check if parent is a garbage directory
    parent_name = parent.split('/')[-1] if parent != '/raphael/Filmes' else ''
    is_garbage_parent = parent_name in [g.strip('/') for g in garbage_parents if g.strip('/')]
    
    if is_garbage_parent or parent in ['/raphael/Filmes/', '/raphael/Filmes']:
        # Quarantine this file
        quar_parent = f'{AUDIT_ROOT}/Duplicatas/Filmes/{parent_name if parent_name else "root"}'
        ensure_path(quar_parent)
        stem, ext = os.path.splitext(name)
        quar_name = f'{stem}__GARBAGE_{str(doc["_id"])[-8:]}{ext}'
        
        files.update_one(
            {'_id': doc['_id']},
            {'$set': {'parent': quar_parent, 'name': quar_name, 'mtime': now}}
        )
        print(f'  QUARANTINED GARBAGE: {parent}/{name} -> {quar_parent}/{quar_name}')
        quarantined += 1
        continue
    
    # Check if it's a Bleach episode
    is_bleach = False
    for pattern in bleach_patterns:
        if pattern.lower() in name.lower() or pattern.lower() in parent.lower():
            is_bleach = True
            break
    
    if is_bleach:
        # Try to extract episode number
        season, episode = extract_season_episode(name)
        if not season:
            # Try Japanese episode patterns
            ep_match = re.search(r'[\-\s](\d{1,2})\s*[\-\s]', name)
            if ep_match:
                episode = int(ep_match.group(1))
                season = 1
        
        if season and episode:
            desired_parent = "/raphael/Series/Bleach/Season 01"
            desired_name = f"Bleach - S{season:02d}E{episode:02d}{os.path.splitext(name)[1]}"
            
            if apply_correction(doc['_id'], desired_parent, desired_name):
                print(f'  MOVED BLEACH: {parent}/{name} -> {desired_parent}/{desired_name}')
                moved_to_series += 1
                continue
        else:
            print(f'  NO EP NUM: {parent}/{name}')
            continue
    
    # Try to extract year from filename
    year = extract_year(name)
    if not year:
        year = extract_year(parent)
    
    if year:
        new_parent = f"{parent} ({year})"
        if apply_correction(str(doc['_id']), new_parent, doc['name']):
            print(f'  ADDED YEAR: {parent}/{name} -> {new_parent}/{doc["name"]}')
            added_year += 1
    else:
        print(f'  NO YEAR FOUND: {parent}/{name}')

print(f"\nMoved to Series (Bleach): {moved_to_series}")
print(f"Added year to directory: {added_year}")
print(f"Quarantined garbage: {quarantined}")

# Regenerate STRM
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)