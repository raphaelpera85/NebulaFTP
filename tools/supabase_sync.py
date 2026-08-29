"""
Supabase Sync Tool for NebulaFTP / MulletaFlix
Permite realizar backup do MongoDB no Supabase (PostgreSQL) e restauração do Supabase para o MongoDB.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
import urllib.error
from os import environ
from os.path import exists

if exists(".env"):
    from dotenv import load_dotenv
    load_dotenv()

def get_supabase_headers(api_key: str) -> dict:
    return {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
        "User-Agent": "MulletaFlix-Backend/1.0"
    }

def test_supabase_connection(url: str, key: str) -> bool:
    if not url or not key:
        print("[ERRO] Supabase URL e API Key são obrigatórios.")
        return False
    
    clean_url = url.rstrip("/") + "/rest/v1/"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "MulletaFlix-Backend/1.0"
    }
    
    try:
        req = urllib.request.Request(clean_url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[OK] Conexão com Supabase bem-sucedida! HTTP {resp.status}")
            return True
    except urllib.error.HTTPError as e:
        if e.code in (200, 404):  # 404 no root do rest/v1 pode acontecer e confirma que o endpoint está ativo e autenticado
            print(f"[OK] Supabase acessível e respondendo (HTTP {e.code}).")
            return True
        print(f"[ERRO] Falha HTTP ao conectar no Supabase: {e.code} - {e.reason}")
        return False
    except Exception as ex:
        print(f"[ERRO] Erro ao conectar no Supabase: {ex}")
        return False

def post_batch_to_supabase(url: str, key: str, table: str, records: list[dict]) -> int:
    if not records:
        return 0
    
    endpoint = f"{url.rstrip('/')}/rest/v1/{table}"
    headers = get_supabase_headers(key)
    
    # Serializa BSON types (ObjectId, datetime, etc.) para JSON puro
    data_json = json.dumps(records, default=str).encode("utf-8")
    
    req = urllib.request.Request(endpoint, data=data_json, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status in (200, 201, 204):
                return len(records)
            return len(records)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8', errors='ignore')
        print(f"[ERRO] Supabase PostgREST error ({e.code}) na tabela '{table}': {err_body}")
        raise RuntimeError(f"PostgREST Error {e.code}: {err_body}") from e
    except Exception as ex:
        print(f"[ERRO] Falha de rede ao enviar para Supabase ({table}): {ex}")
        raise

def fetch_from_supabase(url: str, key: str, table: str, limit: int = 1000, offset: int = 0) -> list[dict]:
    endpoint = f"{url.rstrip('/')}/rest/v1/{table}?select=*&limit={limit}&offset={offset}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "MulletaFlix-Backend/1.0"
    }

    
    req = urllib.request.Request(endpoint, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        content = resp.read().decode("utf-8")
        return json.loads(content)

async def backup_mongo_to_supabase(mongo_uri: str, db_name: str, supabase_url: str, supabase_key: str):
    from motor.motor_asyncio import AsyncIOMotorClient
    
    start_time = time.time()
    print("=" * 60)
    print("INICIANDO BACKUP: MongoDB -> Supabase")
    print("=" * 60)
    print(f"MongoDB: {mongo_uri} | DB: {db_name}")
    print(f"Supabase: {supabase_url}")
    
    client = AsyncIOMotorClient(mongo_uri)
    db = client[db_name]
    
    # 1. Backup de Arquivos (files)
    total_files_cursor = await db.files.count_documents({})
    print(f"\n[1/3] Encontrados {total_files_cursor} arquivos na coleção 'files'...")
    
    files_batch = []
    files_synced = 0
    BATCH_SIZE = 100
    
    async for doc in db.files.find({}):
        doc_id = str(doc.get("_id"))
        parent_id = str(doc.get("parent")) if doc.get("parent") else None
        name = doc.get("name", "")
        size = doc.get("size", 0)
        status = doc.get("status", "unknown")
        parts = doc.get("parts", [])
        uploaded_at = doc.get("uploaded_at")
        
        # Converte doc para JSON serializável
        clean_doc = {}
        for k, v in doc.items():
            if k == "_id":
                clean_doc["_id"] = str(v)
            elif k == "parent" and v:
                clean_doc["parent"] = str(v)
            else:
                clean_doc[k] = v
        
        record = {
            "id": doc_id,
            "name": name,
            "parent": parent_id,
            "size": int(size) if isinstance(size, (int, float)) else 0,
            "status": status,
            "parts": parts,
            "uploaded_at": float(uploaded_at) if isinstance(uploaded_at, (int, float)) else None,
            "doc_data": clean_doc
        }
        files_batch.append(record)
        
        if len(files_batch) >= BATCH_SIZE:
            try:
                post_batch_to_supabase(supabase_url, supabase_key, "nebula_files", files_batch)
                files_synced += len(files_batch)
                pct = (files_synced / total_files_cursor * 100) if total_files_cursor else 100
                print(f"[FILES] Sincronizados {files_synced}/{total_files_cursor} ({pct:.1f}%)")
            except Exception as ex:
                print(f"[ERRO-LOTE] Falha no envio do lote de arquivos: {ex}")
            files_batch = []
    
    if files_batch:
        try:
            post_batch_to_supabase(supabase_url, supabase_key, "nebula_files", files_batch)
            files_synced += len(files_batch)
            print(f"[FILES] Sincronizados {files_synced}/{total_files_cursor} (100.0%)")
        except Exception as ex:
            print(f"[ERRO-LOTE-FINAL] Falha no envio do lote final de arquivos: {ex}")
    
    # 2. Backup de Usuários (users)
    total_users_cursor = await db.users.count_documents({})
    print(f"\n[2/3] Encontrados {total_users_cursor} usuários na coleção 'users'...")
    
    users_batch = []
    users_synced = 0
    async for doc in db.users.find({}):
        clean_doc = {}
        for k, v in doc.items():
            if k == "_id":
                clean_doc["_id"] = str(v)
            else:
                clean_doc[k] = v
        
        login = doc.get("login", "")
        if login:
            record = {
                "login": login,
                "password_hash": doc.get("password", ""),
                "permissions": doc.get("permissions", []),
                "doc_data": clean_doc
            }
            users_batch.append(record)
    
    if users_batch:
        try:
            post_batch_to_supabase(supabase_url, supabase_key, "nebula_users", users_batch)
            users_synced = len(users_batch)
            print(f"[USERS] Sincronizados {users_synced}/{total_users_cursor} usuários.")
        except Exception as ex:
            print(f"[ERRO-USERS] Falha ao sincronizar usuários: {ex}")
    
    # 3. Registrar Histórico em nebula_backups
    elapsed = time.time() - start_time
    backup_record = [{
        "backup_type": "manual",
        "status": "success",
        "total_files": files_synced,
        "total_users": users_synced,
        "details": f"Backup concluído em {elapsed:.2f}s com {files_synced} arquivos e {users_synced} usuários."
    }]
    try:
        post_batch_to_supabase(supabase_url, supabase_key, "nebula_backups", backup_record)
    except Exception:
        pass
    
    print("\n" + "=" * 60)
    print(f"BACKUP FINALIZADO COM SUCESSO EM {elapsed:.2f}s!")
    print(f"Arquivos salvos no Supabase: {files_synced}")
    print(f"Usuários salvos no Supabase: {users_synced}")
    print("=" * 60)
    return {"files": files_synced, "users": users_synced, "elapsed": elapsed}

async def restore_supabase_to_mongo(mongo_uri: str, db_name: str, supabase_url: str, supabase_key: str):
    from motor.motor_asyncio import AsyncIOMotorClient
    from bson import ObjectId
    
    start_time = time.time()
    print("=" * 60)
    print("INICIANDO RESTAURAÇÃO: Supabase -> MongoDB")
    print("=" * 60)
    print(f"Supabase: {supabase_url}")
    print(f"MongoDB Destino: {mongo_uri} | DB: {db_name}")
    
    client = AsyncIOMotorClient(mongo_uri)
    db = client[db_name]
    
    # 1. Restaurar Usuários
    print("\n[1/2] Restaurando usuários do Supabase...")
    try:
        users = fetch_from_supabase(supabase_url, supabase_key, "nebula_users", limit=500)
        restored_users = 0
        for u in users:
            doc = u.get("doc_data", {})
            login = u.get("login") or doc.get("login")
            if login:
                if "_id" in doc and isinstance(doc["_id"], str) and len(doc["_id"]) == 24:
                    try:
                        doc["_id"] = ObjectId(doc["_id"])
                    except Exception:
                        pass
                await db.users.update_one({"login": login}, {"$set": doc}, upsert=True)
                restored_users += 1
        print(f"[USERS] Restaurados {restored_users} usuários com sucesso.")
    except Exception as ex:
        print(f"[ERRO-RESTORE-USERS] {ex}")
        restored_users = 0
    
    # 2. Restaurar Arquivos (com paginação)
    print("\n[2/2] Restaurando biblioteca de arquivos do Supabase...")
    restored_files = 0
    offset = 0
    limit = 500
    
    while True:
        try:
            files_chunk = fetch_from_supabase(supabase_url, supabase_key, "nebula_files", limit=limit, offset=offset)
            if not files_chunk:
                break
            
            for f in files_chunk:
                doc = f.get("doc_data", {})
                file_id_str = f.get("id") or doc.get("_id")
                if file_id_str:
                    obj_id = ObjectId(file_id_str) if len(file_id_str) == 24 else file_id_str
                    doc["_id"] = obj_id
                    
                    if "parent" in doc and doc["parent"] and isinstance(doc["parent"], str) and len(doc["parent"]) == 24:
                        try:
                            doc["parent"] = ObjectId(doc["parent"])
                        except Exception:
                            pass
                    
                    await db.files.update_one({"_id": obj_id}, {"$set": doc}, upsert=True)
                    restored_files += 1
            
            print(f"[FILES] Restaurados {restored_files} arquivos...")
            if len(files_chunk) < limit:
                break
            offset += limit
        except Exception as ex:
            print(f"[ERRO-RESTORE-FILES] Falha ao restaurar lote no offset {offset}: {ex}")
            break
    
    # 3. Recriar índices essenciais
    try:
        await db.files.create_index([("parent", 1), ("name", 1)], unique=True)
        await db.files.create_index("parent")
        await db.files.create_index("status")
        await db.users.create_index("login", unique=True)
        print("[INDEXES] Índices do MongoDB reconstruídos com sucesso.")
    except Exception as ex:
        print(f"[INDEXES-WARN] {ex}")
    
    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print(f"RESTAURAÇÃO CONCLUÍDA EM {elapsed:.2f}s!")
    print(f"Arquivos no MongoDB: {restored_files}")
    print(f"Usuários no MongoDB: {restored_users}")
    print("=" * 60)
    return {"files": restored_files, "users": restored_users, "elapsed": elapsed}

def print_sql_script():
    sql = """-- =========================================================================
-- SCRIPT DE CRIAÇÃO DE TABELAS PARA BACKUP DO NEBULA NO SUPABASE
-- Execute este script no SQL Editor do seu Dashboard do Supabase
-- =========================================================================

CREATE TABLE IF NOT EXISTS nebula_files (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    parent TEXT,
    size BIGINT DEFAULT 0,
    status TEXT NOT NULL,
    parts JSONB,
    uploaded_at NUMERIC,
    doc_data JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_nebula_files_status ON nebula_files(status);
CREATE INDEX IF NOT EXISTS idx_nebula_files_parent ON nebula_files(parent);
CREATE INDEX IF NOT EXISTS idx_nebula_files_name ON nebula_files(name);

CREATE TABLE IF NOT EXISTS nebula_users (
    login TEXT PRIMARY KEY,
    password_hash TEXT,
    permissions JSONB,
    doc_data JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS nebula_backups (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    backup_type TEXT DEFAULT 'manual',
    status TEXT DEFAULT 'success',
    total_files INTEGER DEFAULT 0,
    total_users INTEGER DEFAULT 0,
    details TEXT
);

-- Desabilita RLS para permitir escrita via API com Service Role Key / Anon Key
ALTER TABLE nebula_files DISABLE ROW LEVEL SECURITY;
ALTER TABLE nebula_users DISABLE ROW LEVEL SECURITY;
ALTER TABLE nebula_backups DISABLE ROW LEVEL SECURITY;
"""
    print(sql)

def main():
    parser = argparse.ArgumentParser(description="Nebula Supabase Backup & Restore Tool")
    parser.add_argument("--test", action="store_true", help="Testa conexão com o Supabase")
    parser.add_argument("--backup", action="store_true", help="Faz backup do MongoDB para o Supabase")
    parser.add_argument("--restore", action="store_true", help="Restaura dados do Supabase para o MongoDB")
    parser.add_argument("--sql-schema", action="store_true", help="Exibe o script SQL de criação das tabelas no Supabase")
    parser.add_argument("--url", help="URL do Supabase (ex: https://xxx.supabase.co)")
    parser.add_argument("--key", help="API Key do Supabase")
    parser.add_argument("--mongo-uri", help="MongoDB URI")
    parser.add_argument("--db-name", default="ftp", help="Nome do banco MongoDB (padrão: ftp)")
    
    args = parser.parse_args()
    
    if args.sql_schema:
        print_sql_script()
        return
    
    supabase_url = args.url or environ.get("SUPABASE_URL", "")
    supabase_key = args.key or environ.get("SUPABASE_KEY", "")
    mongo_uri = args.mongo_uri or environ.get("MONGODB", "mongodb://localhost:27017")
    db_name = args.db_name or environ.get("MONGO_DATABASE", "ftp")
    
    if args.test:
        ok = test_supabase_connection(supabase_url, supabase_key)
        sys.exit(0 if ok else 1)
    
    if args.backup:
        if not test_supabase_connection(supabase_url, supabase_key):
            sys.exit(1)
        asyncio.run(backup_mongo_to_supabase(mongo_uri, db_name, supabase_url, supabase_key))
        return
    
    if args.restore:
        if not test_supabase_connection(supabase_url, supabase_key):
            sys.exit(1)
        asyncio.run(restore_supabase_to_mongo(mongo_uri, db_name, supabase_url, supabase_key))
        return
    
    parser.print_help()

if __name__ == "__main__":
    main()
