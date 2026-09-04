#!/usr/bin/env python3
"""Fix remaining garbage directories."""

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

garbage_dirs_to_clean = [
    '/raphael/Filmes/BAIXAR - BLUDV COM',
    '/raphael/Filmes/BAIXE - BLUDV COM',
    '/raphael/Filmes/BAIXE - BLUDV TV',
    '/raphael/Filmes/BLUDV',
    '/raphael/Filmes/COMANDO LA-O Coletivo (2023)',
    '/raphael/Filmes/Deu a Louca na História - O Filme - COMANDO TO',
    '/raphael/Filmes/Drácula - A Última Viagem do Deméter 2023 - BLUDV TO (2023)',
    '/raphael/Filmes/EP 05 - BAIXE OUTROS EPS NO COMANDOTORRENTS COM',
    '/raphael/Filmes/EP 07 - BAIXE OUTROS EPS NO COMANDOTORRENTS COM',
    '/raphael/Filmes/EP 09 - BAIXE OUTROS EPS NO COMANDOTORRENTS COM',
    '/raphael/Filmes/EP 11 - ACESSE COMANDOTORRENTS COM',
    '/raphael/Filmes/EP 13 - ACESSE COMANDOTORRENTS COM',
    '/raphael/Filmes/EP 14 - ACESSE COMANDOTORRENTS COM',
    '/raphael/Filmes/EP 15 - ACESSE COMANDOTORRENTS COM',
    '/raphael/Filmes/EP 17 - ACESSE COMANDOTORRENTS COM',
    '/raphael/Filmes/EP 20 - ACESSE COMANDOTORRENTS COM',
    '/raphael/Filmes/EP 23-24 - ACESSE COMANDOTORRENTS COM',
    '/raphael/Filmes/EP 25 - ACESSE COMANDOTORRENTS COM',
    '/raphael/Filmes/Elementos 2023 - BLUDV TO (2023)',
    '/raphael/Filmes/Entergalactic 2022 - BLUDV TO (2022)',
    '/raphael/Filmes/Esquadrão Secreto 2022 - BLUDV TO (2022)',
    '/raphael/Filmes/Gran Turismo - De Jogador a Corredor 2023 - BLUDV TO (2023)',
    '/raphael/Filmes/Indiana Jones e a Relíquia do Destino 2023 - BLUDV TO (2023)',
    '/raphael/Filmes/Infiltrados - Venezuela 2023 - BLUDV TO (2023)',
    '/raphael/Filmes/Insanidade 2019 - BLUDV TO (2019)',
    '/raphael/Filmes/Lar dos Esquecidos 2022 - BLUDV TO (2022)',
    '/raphael/Filmes/Lobisomem na Noite 2022 - BLUDV TO (2022)',
    '/raphael/Filmes/O Demônio dos Mares 2023 - BLUDV TO (2023)',
    '/raphael/Filmes/O Gigante de Ferro 1999 - BLUDV TO (1999)',
    '/raphael/Filmes/Porta dos Fundos - O Espírito do Natal 2022 - COMANDO LA (2022)',
    '/raphael/Filmes/Seja Você Mesma 2023 - BLUDV TO (2023)',
    '/raphael/Filmes/Tesla - O Homem Elétrico 2020 - BLUDV TO (2020)',
    '/raphael/Filmes/Till - A Busca por Justiça 2022 - BLUDV TO (2022)',
    '/raphael/Filmes/WWW.BLUDV.COM',
    '/raphael/Filmes/5.1 Surround',
    '/raphael/Filmes/Surround',
    '/raphael/Filmes/English',
    '/raphael/Filmes/SDH',
    '/raphael/Filmes/YIFY',
    '/raphael/Filmes/TDF',
    '/raphael/Filmes/scOrp scOrp scOrp',
]

applied = 0

for garbage_dir in garbage_dirs_to_clean:
    dir_files = list(files.find({'parent': garbage_dir, 'type': 'file', 'status': 'completed'}, {'_id': 1, 'name': 1}))
    for doc in dir_files:
        mid = str(doc['_id'])
        name = doc['name']
        base_name, ext = os.path.splitext(name)
        clean = clean_title(base_name)
        
        # Extract year
        year = extract_year(clean)
        if year:
            clean = re.sub(r'\s*\(?\b(?:19|20)\d{2}\b\)?\s*$', '', clean).strip()
        
        # Check series
        is_series = False
        season = episode = None
        ep_match = re.search(r'(?i)S(\d{1,2})[._\- ]*E(\d{1,3})', name)
        if not ep_match:
            ep_match = re.search(r'(?i)(\d{1,2})x(\d{1,3})', name)
        if ep_match:
            is_series = True
            season = int(ep_match.group(1))
            episode = int(ep_match.group(2))
            clean = re.sub(r'(?i)S\d{1,2}[._\- ]*E\d{1,3}.*$', '', clean).strip(' -_.')
            clean = re.sub(r'(?i)\d{1,2}x\d{1,3}.*$', '', clean).strip(' -_.')
        
        if is_series and clean and season and episode:
            desired_parent = f'/raphael/Series/{safe_name(clean)}/Season {season:02d}'
            desired_name = f'{safe_name(clean)} - S{season:02d}E{episode:02d}{ext}'
        elif clean:
            year_suffix = f' ({year})' if year else ''
            desired_parent = f'/raphael/Filmes/{safe_name(clean)}{year_suffix}'
            desired_name = f'{safe_name(clean)}{year_suffix}{ext}'
        else:
            continue
        
        # Check current
        cur = files.find_one({'_id': ObjectId(mid)}, {'parent': 1, 'name': 1})
        if not cur:
            continue
        current = f'{cur["parent"]}/{cur["name"]}'
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
            if applied % 10 == 0:
                print(f'  [{applied}] {current} -> {desired}')

print(f'Total applied: {applied}')

# Regenerate strm
result = subprocess.run(['.venv/Scripts/python.exe', 'generate_strm.py'], cwd='.', capture_output=True, text=True)
print(result.stdout)