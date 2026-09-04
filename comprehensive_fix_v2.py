#!/usr/bin/env python3
"""
Comprehensive fix for NebulaFTP media library:
1. Fix garbage keywords in paths/names (63 items)
2. Fix movies without year in directory (88 items)
3. Fix series without SXXEXX pattern
4. Clean up subtitle files with garbage names
"""

import os
import re
import time
import json
import subprocess
from pathlib import Path
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

GARBAGE_PATTERNS = [
    r'(?i)(ACESSE|COMANDO|BLUDV|WOLVERDON|WWW\.|BAIXE|SDH|YIFY|TDF|LAPUMIA|GALAXYRG|GALAXYTV|RARBG|PSA|LAPUMIAFILMES|THEPIRATEFILMES|TORRENTDOSFILMES|COMANDOTORRENTS|COMANDOTORRENTS\.COM|BLUDV\.TO|BLUDV\.COM|WOLVERDONFILMES|COMANDO\.LA|COMANDO\.TO|WWW\.BLUDV|ENGLISH\s+SDH|SCORP|FORCED|BAIXAR|EP\s+\d+|ACESSE\s+COMANDO|BAIXE\s+OUTROS|COMANDO\.TO|COMANDO\.LA|BLUDV\.TO|BLUDV\.COM)'
]

SUFFIX_PATTERNS = [
    r'(?i)\s+(?:720p|1080p|4k|2160p|bdrip|brrip|webrip|web-dl|webdl|hdtv|x264|x265|h264|h265|hevc|aac|ac3|ddp?5\.?1?|dts|truehd|atmos|dd\+?)\s*$',
    r'(?i)\s+(?:bluray|web|web\.|remux|repack|proper|internal|limited|extended|uncut|directors?\.?cut|theatrical)\s*$',
    r'(?i)\s+(?:dual|dublado|legendado|portuguese|portugues|ptbr|pt-br|eng|english|spa|spanish)\s*$',
    r'(?i)\s+[-_\.]+$',
]

SERIES_EP_PATTERN = re.compile(r'(?i)(?:S(\d{1,2})[._\-\s]*E(\d{1,3})|(\d{1,2})x(\d{1,3}))')
YEAR_PATTERN = re.compile(r'\b(19|20)\d{2}\b')

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

def clean_title(raw):
    if not raw:
        return ''
    title = raw.strip()
    for pattern in GARBAGE_PATTERNS:
        title = re.sub(pattern, '', title)
    title = re.sub(r'[._]+', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip(' -_.')
    for pattern in SUFFIX_PATTERNS:
        title = re.sub(pattern, '', title)
    title = re.sub(r'\s+', ' ', title).strip(' -_.')
    return title

def safe_name(name):
    name = re.sub(r'[<>:\\"/\\|?*]+', ' - ', name)
    name = re.sub(r'\s+', ' ', name).strip(' .-')
    return name[:180]

def extract_year(title):
    match = YEAR_PATTERN.search(title)
    return int(match.group(0)) if match else None

def extract_season_episode(title):
    m = SERIES_EP_PATTERN.search(title)
    if m:
        if m.group(1) and m.group(2):
            return int(m.group(1)), int(m.group(2))
        elif m.group(3) and m.group(4):
            return int(m.group(3)), int(m.group(4))
    return None, None

# Track results
results = {'applied': 0, 'failed': 0, 'skipped': 0, 'details': []}

def apply_correction(mongo_id, desired_parent, desired_name, reason):
    global results
    doc = files.find_one({'_id': ObjectId(mongo_id)}, {'parent': 1, 'name': 1})
    if not doc:
        results['failed'] += 1
        results['details'].append({'id': mongo_id, 'reason': reason, 'status': 'not_found'})
        return False
    
    current = f'{doc["parent"]}/{doc["name"]}'
    desired = f'{desired_parent}/{desired_name}'
    
    if current.lower() == desired.lower():
        results['skipped'] += 1
        results['details'].append({'id': mongo_id, 'reason': reason, 'status': 'already_correct'})
        return True
    
    quarantine_existing(desired_parent, desired_name)
    ensure_path(desired_parent)
    
    result = files.update_one(
        {'_id': ObjectId(mongo_id)},
        {'$set': {'parent': desired_parent, 'name': desired_name, 'mtime': now}}
    )
    
    if result.matched_count == 1:
        results['applied'] += 1
        results['details'].append({'id': mongo_id, 'reason': reason, 'status': 'applied', 'from': current, 'to': desired})
        return True
    else:
        results['failed'] += 1
        results['details'].append({'id': mongo_id, 'reason': reason, 'status': 'failed'})
        return False

# ─── MAIN FIXES ────────────────────────────────────────────────────────────

print("=" * 60)
print("NEBULAFTP MEDIA LIBRARY - COMPREHENSIVE FIX")
print("=" * 60)

# 1. Find all suspect items
suspects = []
for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/(Filmes|Series)'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    path = f"{doc['parent']}/{doc['name']}"
    mid = str(doc['_id'])
    
    if re.search(r'(?i)(ACESSE|COMANDO|BLUDV|WOLVERDON|WWW\.|BAIXE|SDH|YIFY|TDF|LAPUMIA|GALAXYRG|GALAXYTV|RARBG|PSA|LAPUMIAFILMES|THEPIRATEFILMES|TORRENTDOSFILMES|COMANDOTORRENTS|COMANDOTORRENTS\.COM|BLUDV\.TO|BLUDV\.COM|WOLVERDONFILMES|COMANDO\.LA|COMANDO\.TO|WWW\.BLUDV|ENGLISH\s+SDH|SCORP|FORCED|BAIXAR|EP\s+\d+|ACESSE\s+COMANDO|BAIXE\s+OUTROS|COMANDO\.TO|COMANDO\.LA|BLUDV\.TO|BLUDV\.COM)', path):
        suspects.append({'id': mid, 'path': path, 'name': doc['name'], 'parent': doc['parent'], 'reason': 'garbage_keyword'})
    elif doc['parent'].startswith('/raphael/Filmes') and not re.search(r'\(\d{4}\)', doc['parent']):
        suspects.append({'id': mid, 'path': path, 'name': doc['name'], 'parent': doc['parent'], 'reason': 'movie_no_year_dir'})
    elif doc['parent'].startswith('/raphael/Series') and not SERIES_EP_PATTERN.search(doc['name']):
        suspects.append({'id': mid, 'path': path, 'name': doc['name'], 'parent': doc['parent'], 'reason': 'series_no_sxxexx'})

print(f"Total suspects: {len(suspects)}")
by_reason = {}
for s in suspects:
    by_reason[s['reason']] = by_reason.get(s['reason'], 0) + 1
for r, c in by_reason.items():
    print(f"  {r}: {c}")

# ─── FIX 1: Garbage keywords in path/name ──────────────────────────────────
print("\n" + "="*60)
print("FIX 1: Garbage keywords (63 items)")
print("="*60)

for s in [s for s in suspects if s['reason'] == 'garbage_keyword']:
    name = s['name']
    parent = s['parent']
    base_name, ext = os.path.splitext(name)
    
    # Clean the filename
    clean = clean_title(base_name)
    if clean != base_name:
        desired_name = safe_name(clean) + ext
        desired_parent = parent
    else:
        # Check if parent directory has garbage
        parent_parts = parent.strip('/').split('/')
        clean_parts = []
        for part in parent_parts:
            if part and part != 'raphael':
                clean_part = clean_title(part)
                if clean_part != part:
                    clean_parts.append(safe_name(clean_part))
                else:
                    clean_parts.append(part)
        
        if clean_parts != parent_parts[1:]:  # skip 'raphael'
            desired_parent = '/' + '/'.join(['raphael'] + clean_parts)
            desired_name = name
        else:
            # Check if file needs to move to correct location based on content
            # Look at full_audit_fix_plan for this file
            desired_parent = parent
            desired_name = name
    
    if desired_parent != parent or desired_name != name:
        apply_correction(s['id'], desired_parent, desired_name, 'garbage_keyword')

# ─── FIX 2: Movies without year in directory ───────────────────────────────
print("\n" + "="*60)
print("FIX 2: Movies without year in directory (88 items)")
print("="*60)

for s in [s for s in suspects if s['reason'] == 'movie_no_year_dir']:
    name = s['name']
    parent = s['parent']
    
    # Try to extract year from filename
    year = extract_year(name)
    if year:
        # Add year to parent directory
        new_parent = f"{parent} ({year})"
        apply_correction(s['id'], new_parent, s['name'], 'movie_no_year_dir')
    else:
        # Try to extract from parent path
        parent_year = extract_year(parent)
        if not parent_year:
            # Try to find year in any part of path
            full_path = f"{parent}/{s['name']}"
            year = extract_year(full_path)
            if year:
                new_parent = f"{parent} ({year})"
                apply_correction(s['id'], new_parent, s['name'], 'movie_no_year_dir')
            else:
                print(f"  NO YEAR FOUND: {s['path']}")

# ─── FIX 3: Series without SXXEXX ──────────────────────────────────────────
print("\n" + "="*60)
print("FIX 3: Series without SXXEXX pattern")
print("="*60)

for s in [s for s in suspects if s['reason'] == 'series_no_sxxexx']:
    name = s['name']
    parent = s['parent']
    
    # Try to extract season/episode from filename or parent
    season, episode = extract_season_episode(name)
    if not season:
        season, episode = extract_season_episode(parent)
    
    if season and episode:
        # Extract series name from parent
        series_name = parent.split('/')[-1]
        # Clean series name
        clean_series = clean_title(series_name)
        if clean_series != series_name:
            new_parent = f"/raphael/Series/{safe_name(clean_series)}/Season {season:02d}"
            new_name = f"{safe_name(clean_series)} - S{season:02d}E{episode:02d}{os.path.splitext(name)[1]}"
            apply_correction(s['id'], new_parent, new_name, 'series_no_sxxexx')
        else:
            new_parent = f"{parent}/Season {season:02d}"
            new_name = f"{safe_name(series_name)} - S{season:02d}E{episode:02d}{os.path.splitext(name)[1]}"
            apply_correction(s['id'], new_parent, new_name, 'series_no_sxxexx')
    else:
        print(f"  NO SEASON/EP: {s['path']}")

# ─── FIX 4: Subtitle files with garbage names ──────────────────────────────
print("\n" + "="*60)
print("FIX 4: Subtitle files with garbage names")
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
            apply_correction(str(doc['_id']), parent, desired_name, 'subtitle_garbage')

# ─── FIX 5: Clean up garbage directories (EP directories, etc.) ────────────
print("\n" + "="*60)
print("FIX 5: Move episodes from garbage directories")
print("="*60)

# EP directories - these are One Piece episodes
ep_files = list(files.find({
    'type': 'file', 'status': 'completed',
    'parent': {'$regex': '^/raphael/Filmes/EP\s+\d+'}
}, {'_id': 1, 'name': 1, 'parent': 1}))

for doc in ep_files:
    name = doc['name']
    base_name, ext = os.path.splitext(name)
    # Extract episode number from parent
    ep_match = re.search(r'EP\s+(\d+)', doc['parent'])
    if ep_match:
        ep_num = int(ep_match.group(1))
        desired_parent = "/raphael/Series/One Piece/Season 01"
        desired_name = f"One Piece - S01E{ep_num:02d}{ext}"
        apply_correction(str(doc['_id']), desired_parent, desired_name, 'ep_directory')

# COMANDO TO - Cobra Kai S03 episodes in Filmes
ck_s3 = list(files.find({
    'type': 'file', 'status': 'completed',
    'parent': {'$regex': '^/raphael/Filmes/COMANDO TO - Cobra Kai'}
}, {'_id': 1, 'name': 1, 'parent': 1}))

for doc in ck_s3:
    name = doc['name']
    base_name, ext = os.path.splitext(name)
    season, episode = extract_season_episode(name)
    if not season:
        # Try to get from filename
        ep_match = re.search(r'S(\d{1,2})[._\-\s]*E(\d{1,3})', name, re.I)
        if ep_match:
            season, episode = int(ep_match.group(1)), int(ep_match.group(2))
    if season and episode:
        desired_parent = f"/raphael/Series/Cobra Kai/Season {season:02d}"
        desired_name = f"Cobra Kai - S{season:02d}E{episode:02d}{ext}"
        apply_correction(str(doc['_id']), desired_parent, desired_name, 'ck_gardir')

# The Last of Us
tlo = list(files.find({
    'type': 'file', 'status': 'completed',
    'parent': {'$regex': '^/raphael/Filmes/The Last of Us'}
}, {'_id': 1, 'name': 1, 'parent': 1}))

for doc in tlo:
    desired_parent = "/raphael/Series/The Last of Us/Season 01"
    desired_name = "The Last of Us - S01E01.mkv"
    apply_correction(str(doc['_id']), desired_parent, desired_name, 'tlo_dir')

# ─── FIX 6: Clean up movie directory names (remove garbage) ───────────────
print("\n" + "="*60)
print("FIX 6: Clean movie directory names")
print("="*60)

# Find movie directories with garbage
movie_dirs = files.find({'type': 'dir', 'parent': {'$regex': '^/raphael/Filmes/'}}, {'_id': 1, 'name': 1, 'parent': 1})
for doc in movie_dirs:
    if re.search(r'(?i)(ACESSE|COMANDO|BLUDV|WOLVERDON|WWW\.|BAIXE|SDH|YIFY|TDF|EP\s+\d+)', doc['name']):
        clean = clean_title(doc['name'])
        if clean != doc['name']:
            new_name = safe_name(clean)
            # Move all files in this directory
            dir_files = list(files.find({'type': 'file', 'parent': f"{doc['parent']}/{doc['name']}"}, {'_id': 1, 'name': 1}))
            new_parent = f"{doc['parent']}/{safe_name(clean)}"
            ensure_path(new_parent)
            for f in dir_files:
                apply_correction(str(f['_id']), new_parent, f['name'], 'clean_movie_dir')

# ─── SUMMARY ───────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("SUMMARY")
print("="*60)
print(f"Applied:   {results['applied']}")
print(f"Skipped:   {results['skipped']}")
print(f"Failed:    {results['failed']}")

# Regenerate STRM
print("\nRegenerating STRM files...")
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)
if result.stderr:
    print(result.stderr)

# Save results
with open('fix_results.json', 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("\nResults saved to fix_results.json")