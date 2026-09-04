#!/usr/bin/env python3
"""
Build comprehensive consolidated fix plan from ALL sources with proper cleaning.
Priority order:
1. title_fix_plan (Telegram mapping - highest priority)
2. true_content_repair (visual verification - high confidence)
3. content_metadata_fix (embedded metadata)
4. full_audit with PROPER cleaning (lowest priority, but broadest coverage)
"""

import json
import re
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv
import os

load_dotenv('.env')
client = MongoClient(os.getenv('MONGODB', 'mongodb://localhost:27017'))
db = client[os.getenv('MONGO_DATABASE', 'ftp')]
files = db.files

# RELEASE_PREFIXES - comprehensive
RELEASE_PREFIXES = [
    r'(?i)^(?:galaxyrg(?:265)?|galaxytv|rarbg|psa|yts(?:\.[a-z]+)?|comando(?:\.la|to)?|bludv(?:\.to|tv)?|thepiratefilmes|dual|dublado|legendado|portuguese|720p|1080p|4k|brrip|webrip|videotrack|audiotrack|ingles|espanhol)\s*[-_\.]\s*',
    r'(?i)^(?:www\.[^_\-\s]+|by\s+.+|acesse\s+.+)\s*[-_\.]\s*',
    r'(?i)^encoded by [^-]+-\s*',
]

# Additional patterns to remove from END of title
SUFFIX_PATTERNS = [
    r'(?i)\s+(?:720p|1080p|4k|2160p|bdrip|brrip|webrip|web-dl|webdl|hdtv|x264|x265|h264|h265|hevc|aac|ac3|ddp?5\.?1?|dts|truehd|atmos|dd\+?)\s*$',
    r'(?i)\s+(?:bluray|web|web\.|remux|repack|proper|internal|limited|extended|uncut|directors?\.?cut|theatrical)\s*$',
    r'(?i)\s+(?:dual|dublado|legendado|portuguese|portugues|ptbr|pt-br|eng|english|spa|spanish)\s*$',
    r'(?i)\s+[-_\.]+$',
]

def clean_title(raw: str) -> str:
    if not raw:
        return ''
    title = raw.strip()
    
    # Remove prefixes
    for pattern in RELEASE_PREFIXES:
        title = re.sub(pattern, '', title)
    
    # Clean separators
    title = re.sub(r'[._]+', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip(' -_.')
    
    # Remove suffixes (quality, codec, language, etc.)
    for pattern in SUFFIX_PATTERNS:
        title = re.sub(pattern, '', title)
    
    # Final cleanup
    title = re.sub(r'\s+', ' ', title).strip(' -_.')
    return title

def extract_year(title: str):
    match = re.search(r'\b(19|20)\d{2}\b', title)
    return int(match.group(0)) if match else None

def safe_name(name: str) -> str:
    name = re.sub(r'[<>:\\"/\\|?*]+', ' - ', name)
    name = re.sub(r'\s+', ' ', name).strip(' .-')
    return name[:180]

# 1. title_fix_plan.json (HIGHEST PRIORITY)
print('Loading title_fix_plan...')
with open('title_fix_plan.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

all_corrections = {}
for item in data:
    mid = item['doc_id']
    all_corrections[mid] = {
        'mongo_id': mid,
        'desired_parent': item['correct_parent'],
        'desired_name': item['correct_name'],
        'sources': ['title_fix'],
        'priority': 1
    }

# 2. true_content_repair_plan.json
print('Loading true_content_repair...')
with open('../media_audit/true_content_repair_plan.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
for item in data:
    mid = item['mongo_id']
    if mid not in all_corrections:
        all_corrections[mid] = {
            'mongo_id': mid,
            'desired_parent': item['desired_parent'],
            'desired_name': item['desired_name'],
            'sources': ['true_repair'],
            'priority': 2
        }

# 3. content_metadata_fix_plan.json
print('Loading content_metadata_fix...')
with open('../media_audit/content_metadata_fix_plan.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
for item in data['candidates']:
    if not (item.get('selected_copy') and item['action'] == 'move_to_content_path'):
        continue
    mid = item['mongo_id']
    if mid not in all_corrections:
        all_corrections[mid] = {
            'mongo_id': mid,
            'desired_parent': item['desired_parent'],
            'desired_name': item['desired_name'],
            'sources': ['meta_fix'],
            'priority': 3
        }

# 4. full_audit with PROPER cleaning
print('Loading full_audit with proper cleaning...')
with open('../media_audit/full_audit_fix_plan.json', 'r', encoding='utf-8') as f:
    plan = json.load(f)

for item in plan['candidates']:
    if not (item.get('selected_copy') and item['action'] == 'move_to_content_path'):
        continue
    mid = item['mongo_id']
    
    # Only add if not already in higher priority plans
    if mid in all_corrections:
        continue
    
    kind = item.get('kind', '')
    title = item.get('title', '')
    year = item.get('year')
    season = item.get('season')
    episode = item.get('episode')
    
    # Clean properly
    clean = clean_title(title)
    clean_year = extract_year(clean)
    if clean_year:
        clean = re.sub(r'\s*\(?\b(?:19|20)\d{2}\b\)?\s*$', '', clean).strip()
        if year is None:
            year = clean_year
    
    # Skip if clean title is garbage
    GARBAGE_TITLES = {'COM', 'TV', 'LA', 'ENGLISH', 'SDH', 'YIFY', 'TDF', 'BLUDV', 'COMANDO', 'BAIXE', 'WOLVERDON', 'WWW', 'EN', 'BAIXE - BLUDV COM', 'COMANDO.LA', 'COMANDO.TO', 'BLUDV.TO', 'BLUDV.COM', 'BAIXE | BLUDV.TV', 'BAIXE | BLUDV.COM'}
    if not clean or len(clean) < 3 or clean.upper() in GARBAGE_TITLES:
        continue
    
    if kind == 'series_episode' and clean and season and episode:
        correct_parent = f'/raphael/Series/{safe_name(clean)}/Season {season:02d}'
        correct_name = f'{safe_name(clean)} - S{season:02d}E{episode:02d}.mp4'
    elif kind == 'movie' and clean:
        year_suffix = f' ({year})' if year else ''
        correct_parent = f'/raphael/Filmes/{safe_name(clean)}{year_suffix}'
        correct_name = f'{safe_name(clean)}{year_suffix}.mkv'
    else:
        continue
    
    all_corrections[mid] = {
        'mongo_id': mid,
        'desired_parent': correct_parent,
        'desired_name': correct_name,
        'sources': ['full_audit_proper'],
        'priority': 4
    }

print(f'Total unique corrections: {len(all_corrections)}')

# Check current state and filter only needed
corrections_needed = []
already_correct = 0
not_found = 0

for mid, c in all_corrections.items():
    doc = files.find_one({'_id': ObjectId(mid)}, {'parent': 1, 'name': 1})
    if not doc:
        not_found += 1
        continue
    
    current = f'{doc["parent"]}/{doc["name"]}'
    desired = f'{c["desired_parent"]}/{c["desired_name"]}'
    
    if current.lower() == desired.lower():
        already_correct += 1
    else:
        c['current_parent'] = doc['parent']
        c['current_name'] = doc['name']
        corrections_needed.append(c)

print(f'Already correct: {already_correct}')
print(f'Not found: {not_found}')
print(f'Need correction: {len(corrections_needed)}')

# Save final plan
corrections_needed.sort(key=lambda x: x['priority'])
with open('../media_audit/final_consolidated_fix_plan.json', 'w', encoding='utf-8') as f:
    json.dump(corrections_needed, f, ensure_ascii=False, indent=2)

print('Saved to ../media_audit/final_consolidated_fix_plan.json')

# Show stats by source
from collections import Counter
src_counts = Counter()
for c in corrections_needed:
    for s in c['sources']:
        src_counts[s] += 1
for s, cnt in src_counts.most_common():
    print(f'  {s}: {cnt}')

# Show first 10
for c in corrections_needed[:10]:
    print(f'  {c["mongo_id"]}: {c["current_parent"]}/{c["current_name"]} -> {c["desired_parent"]}/{c["desired_name"]} [{c["sources"]}]')