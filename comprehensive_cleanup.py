#!/usr/bin/env python3
"""
Final comprehensive cleanup of remaining issues:
1. Subtitle files with release group names (YIFY, BLUDV, COMANDO, etc.)
2. Episode files in garbage directories (EP 05-25, etc.)
3. Series directories with garbage names
3. Series file without SXXEXX (FullMetal Alchemist s01e61)
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

applied = 0

# ============================================================
# 1. Clean subtitle files with release group names
# ============================================================
subtitle_fixes = [
    ('6a76659654e0892ddf9d6b95', '/raphael/Filmes/12 Monkeys (1995)', '12 Monkeys 1995 BluRay x264 720p.pob.srt'),
    ('6a76659654e0892ddf9d6b97', '/raphael/Filmes/310 to Yuma (2007)', '310 to Yuma 2007 720p BrRip x264 BOKUTOX.pob.srt'),
    ('6a76659654e0892ddf9d6bb0', '/raphael/Filmes/A Bruxa (2016)', 'A Bruxa 2016 1080p BluRay 5 1 x264 DUAL-WWW BLUDV COM forced.srt'),
    ('6a76659654e0892ddf9d6bb2', '/raphael/Filmes/A Bruxa (2016)', 'A Bruxa 2016 1080p BluRay 5 1 x264 DUAL-WWW BLUDV COM.srt'),
    ('6a76659654e0892ddf9d6bb3', '/raphael/Filmes/A Bruxa 2016 1080p BluRay 5 1 x264 DUAL-WWW BLUDV COM (2016)', 'A Bruxa 2016 1080p BluRay 5 1 x264 DUAL-WWW BLUDV COM (2016).srt'),
    ('6a76659654e0892ddf9d6ba5', '/raphael/Filmes/A Christmas Carol (2009)', 'A Christmas Carol 2009 720p BrRip x264 YIFY pob.srt'),
    ('6a76659654e0892ddf9d6ba7', '/raphael/Filmes/Alexander [The Final Cut] (2004)', 'Alexander [Revisited The Final Cut](2004) BrRip 720p x264 YIFY pob.srt'),
    ('6a76659654e0892ddf9d6ba9', '/raphael/Filmes/Apollo 13 (1995)', 'Apollo 13 1995 720p BluRay x264 YIFY pob.srt'),
    ('6a76659854e0892ddf9d7295', '/raphael/Filmes/Aquaman (2019)', 'Aquaman 2019 720p BluRay IMAX x264 DUAL-WWW BLUDV TV-TioKennedy pob.srt'),
    ('6a76659854e0892ddf9d72a6', '/raphael/Filmes/As Aventuras do Dr. Dolittle (2020)', 'Dolittle 2020 720p BluRay x264-DUAL-COMANDO TO pob.srt'),
    ('6a76659854e0892ddf9d72a7', '/raphael/Filmes/As Aventuras do Dr. Dolittle (2020)', 'Dolittle 2020 720p BluRay x264-DUAL-COMANDO TO.srt'),
    ('6a76659854e0892ddf9d72a8', '/raphael/Filmes/As Aventuras do Dr. Dolittle (2020)', 'Dolittle 2020 720p BluRay x264-DUAL-COMANDO TO Forced.srt'),
    ('6a76659654e0892ddf9d6bb9', '/raphael/Filmes/As Good as It Gets (1997)', 'As Good as It Gets 1997 BluRay 720p x264 YIFY pob.srt'),
    ('6a76659654e0892ddf9d6bbb', '/raphael/Filmes/Assassins (1995)', 'Assassins 1995 720p BluRay x264 YIFY pob.srt'),
    ('6a76659654e0892ddf9d6bbd', '/raphael/Filmes/Bangkok Dangerous (2008)', 'Bangkok Dangerous 2008 720p BrRip x264 YIFY pob.srt'),
    ('6a76659654e0892ddf9d6bbf', '/raphael/Filmes/Be Cool (2005)', 'Be Cool 2005 720p BluRay x264 YIFY pob.srt'),
    ('6a76659d54e0892ddf9d7b2c', '/raphael/Filmes/O Protetor (2015)', 'O Protetor (2015) Dual-WOLVERDONFILMES COM pob.srt'),
    ('6a76659854e0892ddf9d72e1', '/raphael/Filmes/O Protetor 2 (2018)', 'O Protetor 2 2018 720p BluRay 6CH x264 DUAL-WWW BLUDV TV.srt'),
]

print('Cleaning subtitle files...')
for mid, parent, desired_name in subtitle_fixes:
    doc = files.find_one({'_id': ObjectId(mid)}, {'parent': 1, 'name': 1})
    if not doc:
        print(f'NOT FOUND: {mid}')
        continue
    current = f'{doc["parent"]}/{doc["name"]}'
    desired = f'{parent}/{desired_name}'
    if current.lower() == desired.lower():
        continue
    
    quarantine_existing(parent, desired_name)
    ensure_path(parent)
    result = files.update_one(
        {'_id': ObjectId(mid)},
        {'$set': {'parent': parent, 'name': desired_name, 'mtime': now}}
    )
    if result.matched_count == 1:
        applied += 1
        print(f'  FIXED: {current} -> {desired}')
    else:
        print(f'  FAILED: {mid}')

# ============================================================
# 2. Move episode files from garbage EP directories to Series
# ============================================================
# These are One Piece episodes
one_piece_eps = [
    ('6a76659d54e0892ddf9d7b37', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E05.mp4'),
    ('6a76659d54e0892ddf9d7b39', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E07.mp4'),
    ('6a76659d54e0892ddf9d7b3b', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E09.mp4'),
    ('6a76659d54e0892ddf9d7b3d', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E11.mp4'),
    ('6a76659d54e0892ddf9d7b3f', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E13.mp4'),
    ('6a76659d54e0892ddf9d7b41', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E14.mp4'),
    ('6a76659d54e0892ddf9d7b43', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E15.mp4'),
    ('6a76659d54e0892ddf9d7b45', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E17.mp4'),
    ('6a76659d54e0892ddf9d7b47', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E23.mp4'),
    ('6a76659d54e0892ddf9d7b49', '/raphael/Series/One Piece/Season 01', 'One Piece - S01E24.mp4'),
]

print('\\nMoving One Piece episodes...')
for mid, desired_parent, desired_name in one_piece_eps:
    doc = files.find_one({'_id': ObjectId(mid)}, {'parent': 1, 'name': 1})
    if not doc:
        print(f'NOT FOUND: {mid}')
        continue
    current = f'{doc["parent"]}/{doc["name"]}'
    desired = f'{desired_parent}/{desired_name}'
    if current.lower() == desired.lower():
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

# ============================================================
# 3. Fix series directories with garbage names
# ============================================================
# Move episodes from garbage series directories to proper locations
series_dir_fixes = [
    # 12X10 - BAIXE OUTROS EPS. NO COMANDOTORRENTS.COM -> One Piece Season 12
    ('6a76659d54e0892ddf9d7b4d', '/raphael/Series/One Piece/Season 12', 'One Piece - S12E10.mp4'),
    ('6a76659d54e0892ddf9d7b4f', '/raphael/Series/One Piece/Season 12', 'One Piece - S12E01.mp4'),
    
    # 12x01 - [BAIXE OUTROS EPS. NO COMANDOTORRENTS.COM] -> One Piece Season 12
    ('6a76659d54e0892ddf9d7b51', '/raphael/Series/One Piece/Season 12', 'One Piece - S12E01.mp4'),
    
    # COMANDO TO - Cobra Kai Season 3
    ('6a76659a54e0892ddf9d75c9', '/raphael/Series/Cobra Kai/Season 03', 'Cobra Kai - S03E02.mp4'),
    ('6a76659a54e0892ddf9d75cb', '/raphael/Series/Cobra Kai/Season 03', 'Cobra Kai - S03E03.mp4'),
    
    # Cobra Kai Season 1 with COMANDO TO
    ('6a76659a54e0892ddf9d7579', '/raphael/Series/Cobra Kai/Season 01', 'Cobra Kai - S01E02.mp4'),
    ('6a76659a54e0892ddf9d757b', '/raphael/Series/Cobra Kai/Season 01', 'Cobra Kai - S01E04.mp4'),
    
    # The Last of Us
    ('6a76659a54e0892ddf9d757d', '/raphael/Series/The Last of Us/Season 01', 'The Last of Us - S01E01.mp4'),
]

print('\\nFixing series directory episodes...')
for mid, desired_parent, desired_name in series_dir_fixes:
    doc = files.find_one({'_id': ObjectId(mid)}, {'parent': 1, 'name': 1})
    if not doc:
        print(f'NOT FOUND: {mid}')
        continue
    current = f'{doc["parent"]}/{doc["name"]}'
    desired = f'{desired_parent}/{desired_name}'
    if current.lower() == desired.lower():
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

# ============================================================
# 4. Fix FullMetal Alchemist s01e61 (episode 61 doesn't exist - FMA has 64 episodes)
# ============================================================
fma_fix = ('6a76659a54e0892ddf9d76ad', '/raphael/Series/FullMetal Alchemist Brotherhood/Season 01', 'FullMetal Alchemist Brotherhood - S01E61.mkv')
mid, desired_parent, desired_name = fma_fix
doc = files.find_one({'_id': ObjectId(mid)}, {'parent': 1, 'name': 1})
if doc:
    current = f'{doc["parent"]}/{doc["name"]}'
    desired = f'{desired_parent}/{desired_name}'
    if current.lower() != desired.lower():
        quarantine_existing(desired_parent, desired_name)
        ensure_path(desired_parent)
        result = files.update_one(
            {'_id': ObjectId(mid)},
            {'$set': {'parent': desired_parent, 'name': desired_name, 'mtime': now}}
        )
        if result.matched_count == 1:
            applied += 1
            print(f'  FIXED FMA: {current} -> {desired}')

# Regenerate strm
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)

print(f'\\nTotal applied: {applied}')