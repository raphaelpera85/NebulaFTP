"""
Supabase integration for MongoDB backup and configuration sync.
Handles automated backup of MongoDB collections and configuration to Supabase.
"""
from __future__ import annotations

import os
import sys
import time
import json
import asyncio
import logging
import argparse
import urllib.request
import urllib.error
from os import environ
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Union
from pathlib import Path

try:
    from supabase import create_client, Client as SupabaseClient
    SUPABASE_AVAILABLE = True
except ImportError:  # pragma: no cover
    SUPABASE_AVAILABLE = False
    SupabaseClient = None  # type: ignore
    create_client = None  # type: ignore

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("SupabaseSync")


class SupabaseSync:
    """Manages synchronization between MongoDB and Supabase for backup and config."""

    def __init__(
        self,
        mongo_uri: Optional[str] = None,
        mongo_db: Optional[str] = None,
        supabase_url: Optional[str] = None,
        supabase_key: Optional[str] = None,
        service_role_key: Optional[str] = None,
    ):
        self.mongo_uri = mongo_uri or os.getenv("MONGODB", "mongodb://localhost:27017")
        self.mongo_db = mongo_db or os.getenv("MONGO_DATABASE", "ftp")
        self.supabase_url = supabase_url or os.getenv("SUPABASE_URL")
        self.supabase_key = supabase_key or os.getenv("SUPABASE_ANON_KEY")
        self.service_role_key = service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY")

        self._mongo_client: Optional[AsyncIOMotorClient] = None
        self._supabase: Optional["SupabaseClient"] = None  # type: ignore
        self._sync_enabled = False

    async def initialize(self) -> bool:
        """Initialize connections and verify Supabase availability."""
        if not SUPABASE_AVAILABLE:
            logger.warning("supabase-py not installed. Run: pip install supabase")
            return False

        if not self.supabase_url or not self.supabase_key:
            logger.warning("SUPABASE_URL or SUPABASE_ANON_KEY not configured")
            return False

        try:
            self._mongo_client = AsyncIOMotorClient(self.mongo_uri)
            await self._mongo_client.admin.command("ping")

            self._supabase = create_client(self.supabase_url, self.supabase_key)  # type: ignore[attr-defined]
            # Test connection
            self._supabase.table("mongo_backups").select("id").limit(1).execute()  # type: ignore[attr-defined]

            self._sync_enabled = True
            logger.info("SupabaseSync initialized successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to initialize SupabaseSync: {e}")
            self._sync_enabled = False
            return False

    async def close(self):
        """Close connections."""
        if self._mongo_client:
            self._mongo_client.close()

    # ==================== BACKUP OPERATIONS ====================

    async def backup_collection(
        self,
        collection_name: str,
        query: Optional[Dict] = None,
        limit: int = 10000,
    ) -> Dict[str, Any]:
        """Backup a MongoDB collection to Supabase."""
        if not self._sync_enabled:
            return {"success": False, "error": "SupabaseSync not initialized"}

        try:
            db = self._mongo_client[self.mongo_db]
            collection = db[collection_name]

            cursor = collection.find(query or {}).limit(limit)
            documents = []
            async for doc in cursor:
                doc["_id"] = str(doc["_id"])
                # Convert datetime objects to ISO strings
                for key, value in doc.items():
                    if isinstance(value, datetime):
                        doc[key] = value.isoformat()
                    elif isinstance(value, ObjectId):
                        doc[key] = str(value)
                documents.append(doc)

            # Insert into Supabase
            backup_record = {
                "collection": collection_name,
                "database": self.mongo_db,
                "document_count": len(documents),
                "data": documents,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "query": query or {},
            }

            result = self._supabase.table("mongo_backups").insert(backup_record).execute()  # type: ignore[attr-defined]

            logger.info(f"Backed up {len(documents)} documents from {collection_name}")
            return {"success": True, "count": len(documents), "backup_id": result.data[0]["id"] if result.data else None}

        except Exception as e:
            logger.error(f"Backup failed for {collection_name}: {e}")
            return {"success": False, "error": str(e)}

    async def backup_all_collections(
        self,
        collections: Optional[List[str]] = None,
        limit_per_collection: int = 10000,
    ) -> Dict[str, Any]:
        """Backup multiple collections."""
        if collections is None:
            db = self._mongo_client[self.mongo_db]
            collections = await db.list_collection_names()

        results = {}
        for coll in collections:
            results[coll] = await self.backup_collection(coll, limit=limit_per_collection)

        return results

    async def restore_collection(
        self,
        collection_name: str,
        backup_id: Optional[str] = None,
        query: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Restore a collection from Supabase backup."""
        if not self._sync_enabled:
            return {"success": False, "error": "SupabaseSync not initialized"}

        try:
            # Get latest backup or specific backup
            query_builder = self._supabase.table("mongo_backups").select("*").eq("collection", collection_name).eq("database", self.mongo_db).order("created_at", desc=True)  # type: ignore[attr-defined]
            if backup_id:
                query_builder = query_builder.eq("id", backup_id)
            else:
                query_builder = query_builder.limit(1)

            result = query_builder.execute()  # type: ignore[attr-defined]
            if not result.data:
                return {"success": False, "error": "No backup found"}

            backup = result.data[0]
            documents = backup.get("data", [])

            # Convert string IDs back to ObjectId
            for doc in documents:
                if "_id" in doc:
                    doc["_id"] = ObjectId(doc["_id"])
                # Convert ISO strings back to datetime
                for key, value in doc.items():
                    if isinstance(value, str):
                        try:
                            doc[key] = datetime.fromisoformat(value.replace("Z", "+00:00"))
                        except ValueError:
                            pass

            # Restore to MongoDB
            db = self._mongo_client[self.mongo_db]
            collection = db[collection_name]

            # Clear existing if restoring full backup
            if not backup_id:
                await collection.delete_many({})

            if documents:
                await collection.insert_many(documents)

            logger.info(f"Restored {len(documents)} documents to {collection_name}")
            return {"success": True, "count": len(documents)}

        except Exception as e:
            logger.error(f"Restore failed for {collection_name}: {e}")
            return {"success": False, "error": str(e)}

    # ==================== CONFIGURATION SYNC ====================

    async def sync_config_to_supabase(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Sync application configuration to Supabase."""
        if not self._sync_enabled:
            return {"success": False, "error": "SupabaseSync not initialized"}

        try:
            config_record = {
                "app_name": "mulletaflix",
                "config": config,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "version": config.get("VERSION", "1.0.0"),
            }

            # Upsert config
            result = self._supabase.table("app_config").upsert(config_record, on_conflict="app_name").execute()  # type: ignore[attr-defined]

            logger.info("Configuration synced to Supabase")
            return {"success": True, "config_id": result.data[0]["id"] if result.data else None}

        except Exception as e:
            logger.error(f"Config sync failed: {e}")
            return {"success": False, "error": str(e)}

    async def load_config_from_supabase(self) -> Optional[Dict[str, Any]]:
        """Load application configuration from Supabase."""
        if not self._sync_enabled:
            return None

        try:
            result = self._supabase.table("app_config").select("*").eq("app_name", "mulletaflix").execute()  # type: ignore[attr-defined]
            if result.data:
                return result.data[0].get("config")
            return None
        except Exception as e:
            logger.error(f"Config load failed: {e}")
            return None

    # ==================== SCHEDULED BACKUP ====================

    async def run_scheduled_backup(
        self,
        collections: List[str] = None,
        interval_hours: int = 24,
    ):
        """Run periodic backup (call from scheduler)."""
        logger.info(f"Starting scheduled backup for collections: {collections}")
        results = await self.backup_all_collections(collections)
        logger.info(f"Scheduled backup completed: {results}")
        return results


# ==================== CONVENIENCE FUNCTIONS ====================

async def create_supabase_sync_from_env() -> SupabaseSync:
    """Create SupabaseSync instance from environment variables."""
    sync = SupabaseSync()
    await sync.initialize()
    return sync


async def backup_mongodb_to_supabase(
    mongo_uri: str = None,
    mongo_db: str = None,
    supabase_url: str = None,
    supabase_key: str = None,
    collections: List[str] = None,
) -> Dict[str, Any]:
    """One-shot backup function."""
    sync = SupabaseSync(mongo_uri, mongo_db, supabase_url, supabase_key)
    if await sync.initialize():
        try:
            return await sync.backup_all_collections(collections)
        finally:
            await sync.close()
    return {"success": False, "error": "Failed to initialize"}


async def sync_config_to_supabase(
    config: Dict[str, Any],
    supabase_url: str = None,
    supabase_key: str = None,
) -> Dict[str, Any]:
    """One-shot config sync."""
    sync = SupabaseSync(supabase_url=supabase_url, supabase_key=supabase_key)
    if await sync.initialize():
        try:
            return await sync.sync_config_to_supabase(config)
        finally:
            await sync.close()
    return {"success": False, "error": "Failed to initialize"}


# ==================== SUPABASE TABLE SCHEMA (SQL) ====================

SUPABASE_SCHEMA_SQL = """
-- Run this in Supabase SQL Editor to create required tables

-- Table for MongoDB backups
CREATE TABLE IF NOT EXISTS mongo_backups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collection TEXT NOT NULL,
    database TEXT NOT NULL,
    document_count INTEGER NOT NULL,
    data JSONB NOT NULL,
    query JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(collection, database, created_at)
);

CREATE INDEX IF NOT EXISTS idx_mongo_backups_collection_db 
ON mongo_backups(collection, database, created_at DESC);

-- Table for application configuration
CREATE TABLE IF NOT EXISTS app_config (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    app_name TEXT UNIQUE NOT NULL,
    config JSONB NOT NULL,
    version TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Enable Row Level Security
ALTER TABLE mongo_backups ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_config ENABLE ROW LEVEL SECURITY;

-- Policies (adjust as needed for your security model)
CREATE POLICY "Allow service role full access" ON mongo_backups
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Allow service role full access" ON app_config
    FOR ALL USING (auth.role() = 'service_role');
"""


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

