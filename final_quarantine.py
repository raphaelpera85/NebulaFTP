#!/usr/bin/env python3
"""Final quarantine of remaining movie_no_year_dir items that can't be auto-categorized"""

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
print("FINAL QUARANTINE OF UNCATEGORIZABLE ITEMS")
print("=" * 60)

quarantined = 0

# Items that are actual movies with known years - add year to directory
movies_with_known_years = {
    'Alexander [Revisited The Final Cut]': 2004,
    '20': 2009,  # 20 (Twilight Zone movie?)
    'Justice League Origens Secretas Parte I': 2001,
    'Justice League Origens Secretas Parte II': 2001,
    'Justice League Origens Secretas Parte III': 2001,
    'Justice League Na Noite Mais Escura Part I': 2003,
}

# Items to quarantine (uncategorizable garbage/unknown)
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    parent = doc.get('parent', '')
    name = doc.get('name', '')
    
    # Skip if already has year in parent
    if re.search(r'\(\d{4}\)', parent):
        continue
    
    parent_name = parent.split('/')[-1] if parent != '/raphael/Filmes' else ''
    
    # Check if it's a known movie we can fix
    year = None
    for movie_title, movie_year in movies_with_known_years.items():
        if movie_title.lower() in parent_name.lower() or movie_title.lower() in name.lower():
            year = movie_year
            break
    
    if year:
        new_parent = f"{parent} ({year})"
        if apply_correction(str(doc['_id']), new_parent, doc['name']):
            print(f'  ADDED YEAR: {parent}/{name} -> {new_parent}/{doc["name"]}')
            continue
    
    # Check for Justice League episodes - move to Series
    if 'Justice League' in parent_name or 'Justice League' in name:
        season, episode = extract_season_episode(name)
        if not season:
            # These are Justice League animated series episodes
            # Try to extract part number
            part_match = re.search(r'(?i)Part[e]?\s*([IVX]+|\d+)', name)
            if part_match:
                part = part_match.group(1)
                # Convert roman numerals
                roman_map = {'I': 1, 'II': 2, 'III': 3, 'IV': 4}
                if part in roman_map:
                    episode = roman_map[part]
                else:
                    try:
                        episode = int(part)
                    except:
                        episode = 1
            else:
                episode = 1
            season = 1
        
        if season and episode:
            desired_parent = "/raphael/Series/Justice League/Season 01"
            desired_name = f"Justice League - S{season:02d}E{episode:02d}{os.path.splitext(name)[1]}"
            
            if apply_correction(doc['_id'], desired_parent, desired_name):
                print(f'  MOVED JL SERIES: {parent}/{name} -> {desired_parent}/{desired_name}')
                continue
    
    # Check for Episode 2, 3, 4 - likely series
    if 'Episode' in parent_name:
        ep_match = re.search(r'(?i)Episode\s*(\d+)', parent_name)
        if ep_match:
            episode = int(ep_match.group(1))
            # Unknown series, quarantine
            pass
    
    # Quarantine everything else
    quar_parent = f'{AUDIT_ROOT}/Duplicatas/Filmes/{parent_name if parent_name else "root"}'
    ensure_path(quar_parent)
    stem, ext = os.path.splitext(name)
    quar_name = f'{stem}__UNCATEGORIZED_{str(doc["_id"])[-8:]}{ext}'
    
    files.update_one(
        {'_id': doc['_id']},
        {'$set': {'parent': quar_parent, 'name': quar_name, 'mtime': now}}
    )
    print(f'  QUARANTINED: {parent}/{name} -> {quar_parent}/{quar_name}')
    quarantined += 1

print(f"\nQuarantined uncategorizable: {quarantined}")

# Regenerate STRM
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)