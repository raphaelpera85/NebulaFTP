#!/usr/bin/env python3
"""
Final comprehensive cleanup:
1. Clean subtitle files with release group names
2. Move episode files from garbage directories to proper Series/Season locations
3. Clean up garbage directories
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
    SUFFIX_PATTERNS = [
        r'(?i)\s+(?:720p|1080p|4k|2160p|bdrip|brrip|webrip|web-dl|webdl|hdtv|x264|x265|h264|h265|hevc|aac|ac3|ddp?5\.?1?|dts|truehd|atmos|dd\+?)\s*$',
        r'(?i)\s+(?:bluray|web|web\.|remux|repack|proper|internal|limited|extended|uncut|directors?\.?cut|theatrical)\s*$',
        r'(?i)\s+(?:dual|dublado|legendado|portuguese|portugues|ptbr|pt-br|eng|english|spa|spanish)\s*$',
        r'(?i)\s+[-_\.]+$',
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

# ============================================================
# STEP 1: Clean subtitle files with release group names
# ============================================================
garbage_main = list(files.find({'parent': {'$regex': '^/raphael/(Filmes|Series)/'}}, {'_id': 1, 'name': 1, 'parent': 1}))
subtitle_corrections = []

for g in garbage_main:
    name = g['name']
    parent = g['parent']
    base_name, ext = os.path.splitext(name)
    
    # Focus on subtitle files
    if ext.lower() in ['.srt', '.pob', '.ass']:
        if re.search(r'(?i)(?:YIFY|BLUDV|COMANDO|WOLVERDON|WWW\.|BAIXE|SDH|TDF|COMANDO\.LA|COMANDO\.TO|BLUDV\.TO|BLUDV\.COM|TIOKENNEDY|BOKUTOX|GALAXYRG|GALAXYTV|RARBG|PSA|THEPIRATEFILMES)', base_name):
            clean = clean_title(base_name)
            if clean != base_name:
                desired_name = safe_name(clean) + ext
                if desired_name != name:
                    subtitle_corrections.append({
                        'mongo_id': str(g['_id']),
                        'desired_parent': parent,
                        'desired_name': desired_name,
                        'current_parent': parent,
                        'current_name': name,
                        'sources': ['subtitle_cleanup'],
                    })

print(f'Subtitle corrections needed: {len(subtitle_corrections)}')

# Apply subtitle corrections
applied = 0
for c in subtitle_corrections:
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
        if applied % 20 == 0 or applied <= 10:
            print(f'  [{applied}] {current} -> {desired}')

print(f'Subtitle cleanup applied: {applied}')

# ============================================================
# STEP 2: Move episode files from garbage directories
# ============================================================
# These are episode files in garbage directories that should be in Series/Season XX/
episode_moves = [
    # EP directories -> Series
    ('6a76659d54e0892ddf9d7b37', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E05.mp4'),  # EP 05
    ('6a76659d54e0892ddf9d7b39', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E07.mp4'),  # EP 07
    ('6a76659d54e0892ddf9d7b3b', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E09.mp4'),  # EP 09
    ('6a76659d54e0892ddf9d7b3d', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E11.mp4'),  # EP 11
    ('6a76659d54e0892ddf9d7b3f', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E13.mp4'),  # EP 13
    ('6a76659d54e0892ddf9d7b41', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E14.mp4'),  # EP 14
    ('6a76659d54e0892ddf9d7b43', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E15.mp4'),  # EP 15
    ('6a76659d54e0892ddf9d7b45', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E17.mp4'),  # EP 17
    ('6a76659d54e0892ddf9d7b47', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E20.mp4'),  # EP 20
    ('6a76659d54e0892ddf9d7b49', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E23.mp4'),  # EP 23-24 part 1
    ('6a76659d54e0892ddf9d7b4b', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E24.mp4'),  # EP 25
]

# COMANDO TO - Cobra Kai episodes
cobra_kai_moves = [
    ('6a76659a54e0892ddf9d75c9', '/raphael/Series/Cobra Kai/Season 03', 'Cobra Kai - S03E02.mp4'),
    ('6a76659a54e0892ddf9d75cb', '/raphael/Series/Cobra Kai/Season 03', 'Cobra Kai - S03E03.mp4'),
]

# Cobra Kai Season 1 episodes with COMANDO TO in name
cobra_kai_s1 = [
    ('6a76659a54e0892ddf9d7579', '/raphael/Series/Cobra Kai/Season 01', 'Cobra Kai - S01E02.mp4'),
    ('6a76659a54e0892ddf9d757b', '/raphael/Series/Cobra Kai/Season 01', 'Cobra Kai - S01E04.mp4'),
]

# The Last of Us
last_of_us = [
    ('6a76659a54e0892ddf9d757d', '/raphael/Series/The Last of Us/Season 01', 'The Last of Us - S01E01.mp4'),
]

all_episode_moves = episode_moves + cobra_kai_moves + cobra_kai_s1 + last_of_us

print(f'Episode moves: {len(all_episode_moves)}')

for mid, desired_parent, desired_name in all_episode_moves:
    doc = files.find_one({'_id': ObjectId(mid)}, {'parent': 1, 'name': 1})
    if not doc:
        print(f'NOT FOUND: {mid}')
        continue
    current = f'{doc["parent"]}/{doc["name"]}'
    desired = f'{desired_parent}/{desired_name}'
    if current.lower() == desired.lower():
        print(f'ALREADY CORRECT: {mid}')
        continue
    
    quarantine_existing(desired_parent, desired_name)
    ensure_path(desired_parent)
    result = files.update_one(
        {'_id': ObjectId(mid)},
        {'$set': {'parent': desired_parent, 'name': desired_name, 'mtime': now}}
    )
    if result.matched_count == 1:
        applied += 1
        print(f'  MOVED: {current} -> {desired}')
    else:
        print(f'  FAILED: {mid}')

print(f'Episode moves applied: {applied}')

# ============================================================
# STEP 3: Move movies from garbage directories to proper locations
# ============================================================
movie_moves = [
    ('6a76659854e0892ddf9d7327', '/raphael/Series/Cobra Kai/Season 03', 'Cobra Kai - S03E01.mp4'),  # COMANDO LA-O Coletivo
    ('6a76659a54e0892ddf9d75e5', '/raphael/Series/Cobra Kai/Season 03', 'Cobra Kai - S03E10.mp4'),  # Surround
    ('6a76659d54e0892ddf9d7a6f', '/raphael/Series/Bleach/Season 10', 'Bleach - S10E16.mkv'),  # English
    ('6a76659954e0892ddf9d7477', '/raphael/Series/Bleach/Season 04', 'Bleach - S04E07.pob.srt'),  # SDH
    ('6a76659a54e0892ddf9d75d8', '/raphael/Series/Cobra Kai/Season 02', 'Cobra Kai - S02E08.mp4'),  # YIFY
    ('6a76659a54e0892ddf9d76ad', '/raphael/Series/FullMetal Alchemist Brotherhood/Season 01', 'FullMetal_Alchemist_Brotherhood_-_s01e61.mkv'),  # TDF
    ('6a76659a54e0892ddf9d75dc', '/raphael/Series/Cobra Kai/Season 03', 'Cobra Kai - S03E01.mp4'),  # scOrp scOrp scOrp
]

print(f'Movie moves: {len(movie_moves)}')

for mid, desired_parent, desired_name in movie_moves:
    doc = files.find_one({'_id': ObjectId(mid)}, {'parent': 1, 'name': 1})
    if not doc:
        print(f'NOT FOUND: {mid}')
        continue
    current = f'{doc["parent"]}/{doc["name"]}'
    desired = f'{desired_parent}/{desired_name}'
    if current.lower() == desired.lower():
        print(f'ALREADY CORRECT: {mid}')
        continue
    
    quarantine_existing(desired_parent, desired_name)
    ensure_path(desired_parent)
    result = files.update_one(
        {'_id': ObjectId(mid)},
        {'$set': {'parent': desired_parent, 'name': desired_name, 'mtime': now}}
    )
    if result.matched_count == 1:
        applied += 1
        print(f'  MOVED: {current} -> {desired}')
    else:
        print(f'  FAILED: {mid}')

print(f'\\nTotal additional moves applied: {applied}')

# Regenerate strm
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)