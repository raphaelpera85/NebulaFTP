#!/usr/bin/env python3
"""
Final comprehensive fix for remaining issues:
1. Fix remaining garbage keywords with proper multi-word handling
2. Fix movie_no_year_dir items (many are actually series episodes)
3. Fix remaining series without SXXEXX
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

def safe_name(name):
    name = re.sub(r'[<>:\\"/\\|?*]+', ' - ', name)
    name = re.sub(r'\s+', ' ', name).strip(' .-')
    return name[:180]

def clean_title(raw):
    """Remove release groups - multi-word first, then single"""
    if not raw:
        return ''
    title = raw.strip()
    
    # Multi-word release groups (processed FIRST)
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
    
    # Remove multi-word groups FIRST
    for group in multi_groups:
        title = re.sub(rf'(?i)^{re.escape(group)}[\s_\-\.]+', '', title)
        title = re.sub(rf'(?i)\b{re.escape(group)}\b', '', title)
    
    # Single word groups (processed AFTER multi-word)
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
        'TO', 'TV', 'LA',  # Added missing release group suffixes
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

# ─── MAIN FIXES ────────────────────────────────────────────────────────────

print("=" * 60)
print("FINAL COMPREHENSIVE FIX")
print("=" * 60)

applied = 0

# ─── FIX 1: Remaining garbage keywords ────────────────────────────────────
print("\n" + "="*60)
print("FIX 1: Remaining garbage keywords")
print("="*60)

suspects = []
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/(Filmes|Series)'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    path = f"{doc['parent']}/{doc['name']}"
    mid = str(doc['_id'])
    
    if re.search(r'(?i)(ACESSE|COMANDO|BLUDV|WOLVERDON|WWW\.|BAIXE|SDH|YIFY|TDF|LAPUMIA|GALAXYRG|GALAXYTV|RARBG|PSA|LAPUMIAFILMES|THEPIRATEFILMES|TORRENTDOSFILMES|COMANDOTORRENTS|COMANDOTORRENTS\.COM|BLUDV\.TO|BLUDV\.COM|WOLVERDONFILMES|COMANDO\.LA|COMANDO\.TO|WWW\.BLUDV|ENGLISH\s+SDH|SCORP|FORCED|BAIXAR|EP\s+\d+|ACESSE\s+COMANDO|BAIXE\s+OUTROS|COMANDO\.TO|COMANDO\.LA|BLUDV\.TO|BLUDV\.COM)', path):
        suspects.append({'id': mid, 'path': path, 'name': doc['name'], 'parent': doc['parent']})

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
        
        # Also clean parent directory
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

# ─── FIX 2: Movies without year - many are actually series ────────────────
print("\n" + "="*60)
print("FIX 2: Movies without year (many are series episodes)")
print("="*60)

movie_suspects = []
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    parent = doc.get('parent', '')
    name = doc.get('name', '')
    if not re.search(r'\(\d{4}\)', parent):
        movie_suspects.append({'id': str(doc['_id']), 'name': name, 'parent': parent})

# Anime series patterns that are actually series episodes
anime_patterns = [
    'Kaiji', 'Gyakkyou Burai Kaiji', 'Bleach', 'One Piece', 'Naruto', 'Dragon Ball',
    'Fullmetal', 'My Hero Academia', 'Jujutsu', 'Demon Slayer', 'Chainsaw Man',
    'Spy', 'Spy × Family', 'Spy Family', 'Jujutsu Kaisen', 'Tokyo Ghoul',
    'Death Note', 'Code Geass', 'Steins Gate', 'Re:Zero', 'Overlord',
    'Shield Hero', 'Mushoku Tensei', 'Tensei Shitara', 'Kumo Desu ga',
    'Slime', 'That Time I Got Reincarnated', 'Fullmetal Alchemist',
    'Hunter x Hunter', 'Fairy Tail', 'Black Clover', 'Boruto',
    'Evangelion', 'Gundam', 'Macross', 'Sword Art Online', 'SAO',
    'Log Horizon', 'No Game No Life', 'Konosuba', 'Re:Zero',
    'Tensei', 'Isekai', 'Overlord', 'Konosuba', 'Shield Hero',
    'Mushoku Tensei', 'Tensei Shitara', 'Kumo Desu ga',
    'That Time I Got Reincarnated as a Slime',
]

print("\nMoving series episodes from Filmes to Series...")
series_moved = 0

for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    parent = doc.get('parent', '')
    name = doc.get('name', '')
    full = f"{parent}/{name}"
    
    # Check if it's an anime series
    is_anime = False
    for pattern in anime_patterns:
        if pattern.lower() in parent.lower() or pattern.lower() in name.lower():
            is_anime = True
            break
    
    if is_anime:
        # Try to extract season/episode
        season, episode = extract_season_episode(name)
        if not season:
            season, episode = extract_season_episode(parent)
        
        if not season:
            # Try other patterns for Japanese titles
            ep_match = re.search(r'[\-\s](\d{1,2})\s*[\-\s]', name)
            if ep_match:
                episode = int(ep_match.group(1))
                season = 1
            else:
                continue
        else:
            episode = int(episode)
        
        # Determine series name
        series_name = None
        for pattern in anime_patterns:
            if pattern.lower() in parent.lower():
                series_name = pattern
                break
            if pattern.lower() in name.lower():
                series_name = pattern
                break
        
        if not series_name:
            # Extract from parent
            series_name = parent.split('/')[-1]
        
        clean_series = clean_title(series_name)
        desired_parent = f"/raphael/Series/{safe_name(clean_series)}/Season {season:02d}"
        desired_name = f"{safe_name(clean_series)} - S{season:02d}E{episode:02d}{os.path.splitext(name)[1]}"
        
        if apply_correction(doc['_id'], desired_parent, f"{safe_name(clean_series)} - S{season:02d}E{episode:02d}{os.path.splitext(name)[1]}"):
            print(f'  MOVED SERIES: {full} -> {desired_parent}/{safe_name(clean_series)} - S{season:02d}E{episode:02d}')
            continue
        
        # Also check for Kaiji already handled
        # Try to extract from Bleach, One Piece, etc.

# ─── FIX 3: Movies without year that have year in filename ────────────────
print("\n" + "="*60)
print("FIX 3: Movies with year in filename but not in directory")
print("="*60)

for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    parent = doc.get('parent', '')
    name = doc.get('name', '')
    if not re.search(r'\(\d{4}\)', parent):
        year = extract_year(name)
        if year:
            new_parent = f"{parent} ({year})"
            if apply_correction(str(doc['_id']), new_parent, doc['name']):
                print(f'  ADDED YEAR: {parent}/{name} -> {new_parent}/{doc["name"]}')

# ─── FIX 4: Fix remaining series without SXXEXX ──────────────────────────
print("\n" + "="*60)
print("FIX 4: Series without SXXEXX")
print("="*60)

for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Series/'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    name = doc.get('name', '')
    parent = doc.get('parent', '')
    if not re.search(r'S\d{2}E\d{2}', name):
        season, episode = extract_season_episode(name)
        if not season:
            season, episode = extract_season_episode(parent)
        if season and episode:
            series_name = parent.split('/')[-1]
            clean_series = clean_title(series_name)
            desired_parent = f"/raphael/Series/{safe_name(clean_series)}/Season {season:02d}"
            desired_name = f"{safe_name(clean_series)} - S{season:02d}E{episode:02d}{os.path.splitext(name)[1]}"
            if apply_correction(str(doc['_id']), desired_parent, desired_name):
                print(f'  FIXED SERIES: {parent}/{name} -> {desired_name}')

# ─── FIX 5: Clean subtitle files ────────────────────────────────────────
print("\n" + "="*60)
print("FIX 5: Clean subtitle files")
print("="*60)

subs = list(files.find({
    'type': 'file', 'status': 'completed',
    'name': {'$regex': r'\.(srt|pob|ass)$', '$options': 'i'},
    'parent': {'$regex': '^/raphael/(Filmes|Series)'}
}, {'_id': 1, 'name': 1, 'parent': 1}))

for doc in subs:
    name = doc['name']
    parent = doc['parent']
    base_name, ext = os.path.splitext(name)
    
    if re.search(r'(?i)(YIFY|BLUDV|COMANDO|WOLVERDON|WWW\.|BAIXE|SDH|TDF|COMANDO\.LA|COMANDO\.TO|BLUDV\.TO|BLUDV\.COM|TIOKENNEDY|BOKUTOX|GALAXYRG|GALAXYTV|RARBG|PSA|THEPIRATEFILMES)', base_name):
        clean = clean_title(base_name)
        if clean != base_name:
            desired_name = safe_name(clean) + ext
            if apply_correction(str(doc['_id']), parent, desired_name):
                print(f'  FIXED SUB: {parent}/{name} -> {desired_name}')

# ─── FIX 6: Fix series directories with garbage names ────────────────────
print("\n" + "="*60)
print("FIX 6: Clean series directory names")
print("="*60)

series_dirs = list(files.find({'type': 'dir', 'parent': {'$regex': '^/raphael/Series/'}}, {'_id': 1, 'name': 1, 'parent': 1}))
for doc in series_dirs:
    clean = clean_title(doc['name'])
    if clean != doc['name']:
        new_name = safe_name(clean)
        new_parent = f"{doc['parent']}/{safe_name(clean)}"
        ensure_path(new_parent)
        dir_files = list(files.find({'type': 'file', 'parent': f"{doc['parent']}/{doc['name']}"}, {'_id': 1, 'name': 1}))
        for f in dir_files:
            apply_correction(str(f['_id']), new_parent, f['name'])
        print(f'  FIXED SERIES DIR: {doc["parent"]}/{doc["name"]} -> {new_parent}')

# ─── FIX 7: Clean movie directory names ──────────────────────────────────
print("\n" + "="*60)
print("FIX 7: Clean movie directory names")
print("="*60)

movie_dirs = list(files.find({'type': 'dir', 'parent': {'$regex': '^/raphael/Filmes/'}}, {'_id': 1, 'name': 1, 'parent': 1}))
for doc in movie_dirs:
    clean = clean_title(doc['name'])
    if clean != doc['name']:
        new_name = safe_name(clean)
        new_parent = f"{doc['parent']}/{safe_name(clean)}"
        ensure_path(new_parent)
        dir_files = list(files.find({'type': 'file', 'parent': f"{doc['parent']}/{doc['name']}"}, {'_id': 1, 'name': 1}))
        for f in dir_files:
            apply_correction(str(f['_id']), new_parent, f['name'])
        print(f'  FIXED MOVIE DIR: {doc["parent"]}/{doc["name"]} -> {new_parent}')

print(f"\n{'='*60}")
print(f"TOTAL APPLIED FIXES")
print(f"{'='*60}")

# Regenerate STRM
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)