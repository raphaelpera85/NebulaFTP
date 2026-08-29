#!/usr/bin/env python3
"""Final cleanup of remaining garbage items"""

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
    """Clean filename base (without extension)"""
    if not raw:
        return ''
    title = raw.strip()
    
    # Remove garbage suffixes
    garbage_suffixes = [
        r'(?i)\s+alt\s+df[0-9a-f]+\s*$',
        r'(?i)\s+alt\s*$',
        r'(?i)\s+BOKUTOX\s*$',
        r'(?i)\s+DUAL\s*[-_\.]\s*$',
        r'(?i)\s+DUAL\s*$',
        r'(?i)\s+-?\s*filmes\s+com\s*$',
        r'(?i)\s+-\s*filmes\s+com\s*$',
        r'(?i)\s+\[.*\]\s*$',
        r'(?i)\s+\(\s*\)\s*$',
        r'(?i)\s+-\s*$',
    ]
    for pattern in garbage_suffixes:
        title = re.sub(pattern, '', title)
    
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
print("FINAL GARBAGE CLEANUP")
print("=" * 60)

fixed = 0

# 1. Fix garbage in filme filenames
print("\n1. Fixing garbage in filme filenames...")
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc['name']
    base, ext = os.path.splitext(name)
    clean = clean_filename(base)
    # Ensure we keep the original extension
    if not clean.endswith(ext):
        new_name = clean + ext
    else:
        new_name = clean
    if new_name != name:
        if apply_correction(str(doc['_id']), doc['parent'], new_name):
            print(f'  FIXED: {doc["parent"]}/{name} -> {new_name}')
            fixed += 1

# 2. Fix garbage in series directory names
print("\n2. Fixing garbage series directory names...")
garbage_series_dirs = {
    '12X10 - OUTROS EPS NO COM': 'Dexter',
    '12x01 - [ OUTROS EPS NO COM]': 'Dexter',
    '12x01 - [BAIXE OUTROS EPS. NO COMANDOTORRENTS.COM]': 'Dexter',
    'COMANDO TO - Cobra Kai': 'Cobra Kai',
    'The Last of Us 1x01': 'The Last of Us',
    'The Last of Us 1x01 - COMANDO.LA': 'The Last of Us',
    'RARBG - Vikings': 'Vikings',
    'RARBG - Young.Justice': 'Young Justice',
    'Bad.Sisters': 'Bad Sisters',
    'Black.Mirror': 'Black Mirror',
    'The.Big.Bang.Theory': 'The Big Bang Theory',
    'The.Burning.Girls': 'The Burning Girls',
    'The.Capture': 'The Capture',
    'The.Gold': 'The Gold',
    'The.Herculoids.1981': 'The Herculoids (1981)',
    'The.Long.Shadow': 'The Long Shadow',
    'The.Other.Black.Girl': 'The Other Black Girl',
    'The.Rig': 'The Rig',
    'The.Tourist': 'The Tourist',
    'Time.2021': 'Time (2021)',
    'Twisted.Metal': 'Twisted Metal',
    'Yellowstone.2018': 'Yellowstone (2018)',
    'Young Justice': 'Young Justice',
    'Justice.League': 'Justice League',
    'Star Wars - Andor': 'Star Wars: Andor',
    'Star Wars Andor': 'Star Wars: Andor',
    'Invasão Secreta': 'Secret Invasion',
    'Minhas Aventuras com o Superman': 'My Adventures with Superman',
    'Monarch Legacy of Monsters': 'Monarch: Legacy of Monsters',
    'Monarch-Legado de Monstros': 'Monarch: Legacy of Monsters',
    'FullMetal Alchemist Brotherhood': 'Fullmetal Alchemist: Brotherhood',
    'Gavião Arqueiro': 'Hawkeye',
    'Obi-Wan Kenobi': 'Obi-Wan Kenobi',
    'Vikings': 'Vikings',
    'Sons of Anarchy': 'Sons of Anarchy',
    'Breaking Bad': 'Breaking Bad',
    'Better Call Saul': 'Better Call Saul',
}

for old_name, new_name in garbage_series_dirs.items():
    dir_doc = files.find_one({'type': 'dir', 'parent': '/raphael/Series', 'name': old_name})
    if dir_doc:
        new_dir_name = safe_name(new_name)
        old_path = f'/raphael/Series/{old_name}'
        new_path = f'/raphael/Series/{new_dir_name}'
        
        # Move all files in this directory (and subdirectories)
        dir_files = list(files.find({'parent': {'$regex': f'^{re.escape(old_path)}'}}, {'_id': 1, 'name': 1, 'parent': 1}))
        for f in dir_files:
            if f.get('type') == 'dir':
                continue
            rel_parent = f['parent'][len(old_path):].lstrip('/')
            new_parent = f'{new_path}/{rel_parent}' if rel_parent else new_path
            
            # Check if target already exists
            existing = files.find_one({'parent': new_parent, 'name': f['name']})
            if existing and str(existing['_id']) != str(f['_id']):
                # Quarantine duplicate
                quar_parent = f'{AUDIT_ROOT}/Duplicatas{new_parent.replace("/raphael", "")}'
                ensure_path(quar_parent)
                stem, ext = os.path.splitext(f['name'])
                quar_name = f'{stem}__DUP_{str(f["_id"])[-8:]}{os.path.splitext(f["name"])[1]}'
                files.update_one(
                    {'_id': f['_id']},
                    {'$set': {'parent': quar_parent, 'name': quar_name, 'mtime': now}}
                )
                print(f'  QUARANTINED DUP: {f["parent"]}/{f["name"]} -> {quar_parent}/{quar_name}')
            else:
                if apply_correction(str(f['_id']), new_parent, f['name']):
                    print(f'  MOVED: {f["parent"]}/{f["name"]} -> {new_parent}/{f["name"]}')
                    fixed += 1
        print(f'  FIXED DIR: {old_name} -> {new_dir_name}')

# 3. Fix Hai to Gensou no Grimgar filenames
print("\n3. Fixing Hai to Gensou no Grimgar files...")
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Series/Hai to Gensou no Grimgar'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc['name']
    base, ext = os.path.splitext(name)
    clean = clean_filename(base)
    if not clean.endswith(ext):
        new_name = clean + ext
    else:
        new_name = clean
    if new_name != name:
        if apply_correction(str(doc['_id']), doc['parent'], new_name):
            print(f'  FIXED: {doc["parent"]}/{name} -> {new_name}')
            fixed += 1

# 4. Fix remaining subtitle garbage
print("\n4. Fixing remaining subtitle garbage...")
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}, 'name': {'$regex': r'\.(srt|pob|ass)$', '$options': 'i'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc['name']
    base, ext = os.path.splitext(name)
    clean = clean_filename(base)
    if not clean.endswith(ext):
        new_name = clean + ext
    else:
        new_name = clean
    if new_name != name:
        if apply_correction(str(doc['_id']), doc['parent'], new_name):
            print(f'  FIXED SUB: {doc["parent"]}/{name} -> {new_name}')
            fixed += 1

print(f"\nTotal fixed: {fixed}")

# Regenerate STRM
print("\nRegenerating STRM...")
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)