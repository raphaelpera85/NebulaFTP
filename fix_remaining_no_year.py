#!/usr/bin/env python3
"""Fix the remaining movie_no_year_dir items - many are actually series episodes"""

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
    if not raw:
        return ''
    title = raw.strip()
    
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
    for group in multi_groups:
        title = re.sub(rf'(?i)^{re.escape(group)}[\s_\-\.]+', '', title)
        title = re.sub(rf'(?i)\b{re.escape(group)}\b', '', title)
    
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
        'TO', 'TV', 'LA',
    ]
    
    for group in single_groups:
        title = re.sub(rf'(?i)\b{re.escape(group)}\b', '', title)
    
    title = re.sub(r'[._]+', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip(' -_.')
    
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

# Anime series patterns that should be moved to Series
anime_patterns = [
    'Bleach', 'Naruto', 'One Piece', 'Dragon Ball', 'Fullmetal',
    'My Hero Academia', 'Jujutsu', 'Demon Slayer', 'Chainsaw Man',
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
    'Justice League', 'Batman', 'Superman', 'Wonder Woman',
    'The Big Bang Theory', 'Friends', 'Breaking Bad', 'Better Call Saul',
    'Game of Thrones', 'House of Cards', 'Stranger Things', 'The Witcher',
    'Sons of Anarchy', 'Cobra Kai', 'The Last of Us', 'The Mandalorian',
    'Loki', 'WandaVision', 'Falcon and Winter Soldier', 'Hawkeye',
    'Moon Knight', 'Ms Marvel', 'She Hulk', 'Secret Invasion',
    'Invincible', 'The Boys', 'Gen V', 'Peacemaker', 'Ted Lasso',
    'Severance', 'Foundation', 'For All Mankind', 'See',
    'Dickinson', 'Mythic Quest', 'Ted Lasso', 'Physical',
    'Schmigadoon', 'Loot', 'Surface', 'Bad Sisters', 'Slow Horses',
    'Silo', 'Shrinking', 'Platonic', 'The Afterparty', 'High Desert',
    'City on Fire', 'American Born Chinese', 'Class of 09', 'Tiny Beautiful Things',
    'Beef', 'Swarm', 'Tiny Beautiful Things', 'Fleishman Is in Trouble',
    'A Small Light', 'Saint X', 'The Bear', 'Only Murders in the Building',
    'What We Do in the Shadows', 'Atlanta', 'Reservation Dogs', 'Pistol',
    'The Old Man', 'Under the Banner of Heaven', 'Candy', 'The Dropout',
    'Inventing Anna', 'The Girl from Plainville', 'Pam and Tommy',
    'Dopesick', 'The Staircase', 'WeCrashed', 'Super Pumped', 'Black Bird',
    'Five Days at Memorial', 'The Patient', 'Echoes', 'Surface',
    'Bad Sisters', 'Slow Horses', 'Silo', 'Shrinking', 'Platonic',
    'The Afterparty', 'High Desert', 'City on Fire', 'American Born Chinese',
    'Class of 09', 'Tiny Beautiful Things', 'Fleishman Is in Trouble',
    'A Small Light', 'Saint X', 'The Bear', 'Only Murders in the Building',
    'What We Do in the Shadows', 'Atlanta', 'Reservation Dogs', 'Pistol',
    'The Old Man', 'Under the Banner of Heaven', 'Candy', 'The Dropout',
    'Inventing Anna', 'The Girl from Plainville', 'Pam and Tommy',
    'Dopesick', 'The Staircase', 'WeCrashed', 'Super Pumped', 'Black Bird',
    'Five Days at Memorial', 'The Patient', 'Echoes',
]

print("=" * 60)
print("FIXING REMAINING MOVIE_NO_YEAR_DIR ITEMS")
print("=" * 60)

moved = 0
added_year = 0

for doc in files.find({'type': 'file', 'status': 'completed', 'parent': {'$regex': '^/raphael/Filmes/'}}, {'_id': 1, 'name': 1, 'parent': 1}):
    parent = doc.get('parent', '')
    name = doc.get('name', '')
    
    # Skip if already has year in parent
    if re.search(r'\(\d{4}\)', parent):
        continue
    
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
        
        # Try Japanese episode patterns
        if not season:
            ep_match = re.search(r'[\-\s](\d{1,2})\s*[\-\s]', name)
            if ep_match:
                episode = int(ep_match.group(1))
                season = 1
            else:
                # Try to extract from parent (e.g., "Episode 2")
                ep_match = re.search(r'(?i)(?:episode|ep)\s*(\d{1,2})', parent)
                if ep_match:
                    episode = int(ep_match.group(1))
                    season = 1
        
        if season and episode:
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
                series_name = parent.split('/')[-1]
            
            clean_series = clean_title(series_name)
            desired_parent = f"/raphael/Series/{safe_name(clean_series)}/Season {season:02d}"
            desired_name = f"{safe_name(clean_series)} - S{season:02d}E{episode:02d}{os.path.splitext(name)[1]}"
            
            if apply_correction(doc['_id'], desired_parent, f"{safe_name(clean_series)} - S{season:02d}E{episode:02d}{os.path.splitext(name)[1]}"):
                print(f'  MOVED SERIES: {parent}/{name} -> {desired_parent}/{safe_name(clean_series)} - S{season:02d}E{episode:02d}')
                moved += 1
                continue
    
    # Try to extract year from filename for movies
    year = extract_year(name)
    if not year:
        year = extract_year(parent)
    
    if year:
        new_parent = f"{parent} ({year})"
        if apply_correction(str(doc['_id']), new_parent, doc['name']):
            print(f'  ADDED YEAR: {parent}/{name} -> {new_parent}/{doc["name"]}')
            added_year += 1
    else:
        print(f'  NO YEAR: {parent}/{name}')

print(f"\nMoved to Series: {moved}")
print(f"Added year to directory: {added_year}")

# Regenerate STRM
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)