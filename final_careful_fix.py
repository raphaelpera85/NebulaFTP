#!/usr/bin/env python3
"""
Comprehensive fix for NebulaFTP media library - CAREFUL VERSION
Only removes known release groups as WHOLE WORDS, not substrings.
"""

import os
import re
import time
import json
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

# Release groups to remove - ONLY as whole words/prefixes/suffixes
RELEASE_GROUPS = [
    'WWW', 'YIFY', 'BLUDV', 'COMANDO', 'WOLVERDON', 'LAPUMIA', 'GALAXYRG', 'GALAXYTV',
    'RARBG', 'PSA', 'THEPIRATEFILMES', 'TORRENTDOSFILMES', 'COMANDOTORRENTS',
    'COMANDOTORRENTS.COM', 'BLUDV.TO', 'BLUDV.COM', 'WOLVERDONFILMES',
    'COMANDO.LA', 'COMANDO.TO', 'WWW.BLUDV', 'ENGLISH SDH', 'SCORP', 'FORCED',
    'BAIXAR', 'BAIXE', 'ACESSE', 'COMANDO.TO', 'COMANDO.LA', 'BLUDV.TO',
    'BLUDV.COM', 'WWW.BLUDV', 'TIOKENNEDY', 'BOKUTOX', 'GALAXYRG', 'GALAXYTV',
    'RARBG', 'PSA', 'THEPIRATEFILMES', 'LAPUMIAFILMES', 'COMANDOTORRENTS',
    'COMANDOTORRENTS.COM', 'WOLVERDONFILMES', 'ENGLISH SDH', 'SDH',
    'TDF', 'LAPUMIAFILMES', 'COMANDOTORRENTS', 'COMANDOTORRENTS.COM',
    'WOLVERDONFILMES', 'COMANDO.LA', 'COMANDO.TO', 'WWW.BLUDV'
]

# Build regex that matches whole words only
GROUP_PATTERN = r'(?i)\b(' + '|'.join(re.escape(g) for g in RELEASE_GROUPS) + r')\b'

SUFFIX_PATTERNS = [
    r'(?i)\s+(?:720p|1080p|4k|2160p|bdrip|brrip|webrip|web-dl|webdl|hdtv|x264|x265|h264|h265|hevc|aac|ac3|ddp?5\.?1?|dts|truehd|atmos|dd\+?)\s*$',
    r'(?i)\s+(?:bluray|web|web\.|remux|repack|proper|internal|limited|extended|uncut|directors?\.?cut|theatrical)\s*$',
    r'(?i)\s+(?:dual|dublado|legendado|portuguese|portugues|ptbr|pt-br|eng|english|spa|spanish)\s*$',
    r'(?i)\s+[-_\.]+$',
]

SERIES_EP_PATTERN = re.compile(r'(?i)(?:S(\d{1,2})[._\-\s]*E(\d{1,3})|(\d{1,2})x(\d{1,3}))')
YEAR_PATTERN = re.compile(r'\b(19|20)\d{2}\b')

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

def clean_title(raw):
    """Clean title - only remove known release groups as whole words"""
    if not raw:
        return ''
    title = raw.strip()
    
    # Remove release group prefixes
    for group in RELEASE_GROUPS:
        # Match at start with separator
        title = re.sub(rf'(?i)^{re.escape(group)}[\s_\-\.]+', '', title)
    
    # Remove release groups as whole words anywhere
    title = re.sub(GROUP_PATTERN, '', title)
    
    # Clean separators
    title = re.sub(r'[._]+', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip(' -_.')
    
    # Remove quality/codec suffixes
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

now = int(time.time())
results = {'applied': 0, 'failed': 0, 'skipped': 0, 'details': []}

def apply_correction(mongo_id, desired_parent, desired_name, reason):
    global now
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

# ─── SCAN FOR ISSUES ──────────────────────────────────────────────────────

suspects = []
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/(Filmes|Series)'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    path = f"{doc['parent']}/{doc['name']}"
    mid = str(doc['_id'])
    
    if re.search(r'(?i)(ACESSE|COMANDO|BLUDV|WOLVERDON|WWW\.|BAIXE|SDH|YIFY|TDF|LAPUMIA|GALAXYRG|GALAXYTV|RARBG|PSA|LAPUMIAFILMES|THEPIRATEFILMES|TORRENTDOSFILMES|COMANDOTORRENTS|COMANDOTORRENTS\.COM|BLUDV\.TO|BLUDV\.COM|WOLVERDONFILMES|COMANDO\.LA|COMANDO\.TO|WWW\.BLUDV|ENGLISH\s+SDH|SCORP|FORCED|BAIXAR|EP\s+\d+|ACESSE\s+COMANDO|BAIXE\s+OUTROS|COMANDO\.TO|COMANDO\.LA|BLUDV\.TO|BLUDV\.COM)', path):
        suspects.append({'id': mid, 'path': path, 'name': doc['name'], 'parent': doc['parent'], 'reason': 'garbage_keyword'})
    elif doc['parent'].startswith('/raphael/Filmes') and not re.search(r'\(\d{4}\)', doc['parent']):
        suspects.append({'id': mid, 'path': path, 'name': doc['name'], 'parent': doc['parent'], 'reason': 'movie_no_year_dir'})
    elif doc['parent'].startswith('/raphael/Series') and not re.search(r'S\d{2}E\d{2}', doc['name']):
        suspects.append({'id': mid, 'path': path, 'name': doc['name'], 'parent': doc['parent'], 'reason': 'series_no_sxxexx'})

print(f"Total suspects: {len(suspects)}")

# ─── FIX 1: Garbage keywords ──────────────────────────────────────────────
applied = 0
for s in [s for s in suspects if s['reason'] == 'garbage_keyword']:
    name = s['name']
    parent = s['parent']
    base_name, ext = os.path.splitext(name)
    
    # Clean the filename
    clean = clean_title(base_name)
    if clean != base_name:
        desired_name = safe_name(clean) + ext
        desired_parent = parent
        
        # Also clean parent if it has garbage
        parent_parts = parent.strip('/').split('/')
        clean_parts = []
        for part in parent_parts:
            if part and part != 'raphael':
                clean_part = clean_title(part)
                clean_parts.append(safe_name(clean_part) if clean_part != part else part)
            else:
                clean_parts.append(part)
        
        if clean_parts != parent_parts:
            desired_parent = '/' + '/'.join(clean_parts)
        
        if apply_correction(s['id'], desired_parent, desired_name, 'garbage_keyword'):
            applied += 1

# ─── FIX 2: Movies without year ──────────────────────────────────────────
for s in [s for s in suspects if s['reason'] == 'movie_no_year_dir']:
    name = s['name']
    parent = s['parent']
    year = extract_year(name) or extract_year(parent)
    if year:
        new_parent = f"{parent} ({year})"
        if apply_correction(s['id'], new_parent, s['name'], 'movie_no_year_dir'):
            pass  # count at end

# ─── FIX 3: Series without SXXEXX ────────────────────────────────────────
for s in [s for s in suspects if s['reason'] == 'series_no_sxxexx']:
    name = s['name']
    parent = s['parent']
    season, episode = extract_season_episode(name)
    if not season:
        season, episode = extract_season_episode(parent)
    if season and episode:
        series_name = parent.split('/')[-1]
        clean_series = clean_title(series_name)
        desired_parent = f"/raphael/Series/{safe_name(clean_series)}/Season {season:02d}"
        desired_name = f"{safe_name(clean_series)} - S{season:02d}E{episode:02d}{os.path.splitext(name)[1]}"
        if apply_correction(s['id'], desired_parent, desired_name, 'series_no_sxxexx'):
            pass

# ─── SUBTITLE FILES ──────────────────────────────────────────────────────
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
            apply_correction(str(doc['_id']), parent, desired_name, 'subtitle_garbage')

# ─── EP DIRECTORIES (One Piece) ──────────────────────────────────────────
ep_files = list(files.find({
    'type': 'file', 'status': 'completed',
    'parent': {'$regex': '^/raphael/Filmes/EP\s+\d+'}
}, {'_id': 1, 'name': 1, 'parent': 1}))

for doc in ep_files:
    name = doc['name']
    base_name, ext = os.path.splitext(name)
    ep_match = re.search(r'EP\s+(\d+)', doc['parent'])
    if ep_match:
        ep_num = int(ep_match.group(1))
        desired_parent = "/raphael/Series/One Piece/Season 01"
        desired_name = f"One Piece - S01E{ep_num:02d}{ext}"
        apply_correction(str(doc['_id']), desired_parent, desired_name, 'ep_directory')

# ─── COMANDO TO - COBRA KAI S03 ──────────────────────────────────────────
ck_s3 = list(files.find({
    'type': 'file', 'status': 'completed',
    'parent': {'$regex': '^/raphael/Filmes/COMANDO TO - Cobra Kai'}
}, {'_id': 1, 'name': 1, 'parent': 1}))

for doc in ck_s3:
    name = doc['name']
    base_name, ext = os.path.splitext(name)
    season, episode = extract_season_episode(name)
    if not season:
        ep_match = re.search(r'S(\d{1,2})[._\-\s]*E(\d{1,3})', name, re.I)
        if ep_match:
            season, episode = int(ep_match.group(1)), int(ep_match.group(2))
    if season and episode:
        desired_parent = f"/raphael/Series/Cobra Kai/Season {season:02d}"
        desired_name = f"Cobra Kai - S{season:02d}E{episode:02d}{ext}"
        apply_correction(str(doc['_id']), desired_parent, desired_name, 'ck_gardir')

# ─── THE LAST OF US ──────────────────────────────────────────────────────
tlo = list(files.find({
    'type': 'file', 'status': 'completed',
    'parent': {'$regex': '^/raphael/Filmes/The Last of Us'}
}, {'_id': 1, 'name': 1, 'parent': 1}))

for doc in tlo:
    desired_parent = "/raphael/Series/The Last of Us/Season 01"
    desired_name = "The Last of Us - S01E01.mkv"
    apply_correction(str(doc['_id']), desired_parent, desired_name, 'tlo_dir')

# ─── CLEAN MOVIE DIRECTORY NAMES ────────────────────────────────────────
movie_dirs = list(files.find({'type': 'dir', 'parent': {'$regex': '^/raphael/Filmes/'}}, {'_id': 1, 'name': 1, 'parent': 1}))
for doc in movie_dirs:
    if re.search(r'(?i)(ACESSE|COMANDO|BLUDV|WOLVERDON|WWW\.|BAIXE|SDH|YIFY|TDF|EP\s+\d+)', doc['name']):
        clean = clean_title(doc['name'])
        if clean != doc['name']:
            new_name = safe_name(clean)
            dir_files = list(files.find({'type': 'file', 'parent': f"{doc['parent']}/{doc['name']}"}, {'_id': 1, 'name': 1}))
            new_parent = f"{doc['parent']}/{safe_name(clean)}"
            ensure_path(new_parent)
            for f in dir_files:
                apply_correction(str(f['_id']), new_parent, f['name'], 'clean_movie_dir')

# ─── SUMMARY ─────────────────────────────────────────────────────────────
# Re-scan to count
suspects_final = []
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/(Filmes|Series)'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    path = f"{doc['parent']}/{doc['name']}"
    mid = str(doc['_id'])
    
    if re.search(r'(?i)(ACESSE|COMANDO|BLUDV|WOLVERDON|WWW\.|BAIXE|SDH|YIFY|TDF|LAPUMIA|GALAXYRG|GALAXYTV|RARBG|PSA|LAPUMIAFILMES|THEPIRATEFILMES|TORRENTDOSFILMES|COMANDOTORRENTS|COMANDOTORRENTS\.COM|BLUDV\.TO|BLUDV\.COM|WOLVERDONFILMES|COMANDO\.LA|COMANDO\.TO|WWW\.BLUDV|ENGLISH\s+SDH|SCORP|FORCED|BAIXAR|EP\s+\d+|ACESSE\s+COMANDO|BAIXE\s+OUTROS|COMANDO\.TO|COMANDO\.LA|BLUDV\.TO|BLUDV\.COM)', path):
        suspects_final.append({'id': mid, 'path': path, 'reason': 'garbage_keyword'})
    elif doc['parent'].startswith('/raphael/Filmes') and not re.search(r'\(\d{4}\)', doc['parent']):
        suspects_final.append({'id': mid, 'path': path, 'reason': 'movie_no_year_dir'})
    elif doc['parent'].startswith('/raphael/Series') and not re.search(r'S\d{2}E\d{2}', doc['name']):
        suspects_final.append({'id': mid, 'path': path, 'reason': 'series_no_sxxexx'})

print(f"Remaining suspects: {len(suspects_final)}")
from collections import Counter
print('By reason:', Counter(s['reason'] for s in suspects_final))

# Regenerate STRM
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)