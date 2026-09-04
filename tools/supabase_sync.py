"""
Supabase integration for MongoDB backup and configuration sync.
Handles automated backup of MongoDB collections and configuration to Supabase.
"""

import os
import json
import asyncio
import logging
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


if __name__ == "__main__":
    # CLI for manual backup
    import sys

    async def main():
        if len(sys.argv) < 2:
            print("Usage: python supabase_sync.py <command> [args]")
            print("Commands: backup, restore, sync-config, load-config")
            return

        command = sys.argv[1]
        sync = await create_supabase_sync_from_env()

        if not sync._sync_enabled:
            print("Failed to initialize SupabaseSync. Check environment variables.")
            return

        try:
            if command == "backup":
                collections = sys.argv[2:] if len(sys.argv) > 2 else None
                result = await sync.backup_all_collections(collections)
                print(json.dumps(result, indent=2, default=str))

            elif command == "restore":
                if len(sys.argv) < 3:
                    print("Usage: python supabase_sync.py restore <collection_name> [backup_id]")
                    return
                collection = sys.argv[2]
                backup_id = sys.argv[3] if len(sys.argv) > 3 else None
                result = await sync.restore_collection(collection, backup_id)
                print(json.dumps(result, indent=2, default=str))

            elif command == "sync-config":
                # Load config from .env.mulletaflix
                from dotenv import dotenv_values
                config = dotenv_values(".env.mulletaflix")
                result = await sync.sync_config_to_supabase(config)
                print(json.dumps(result, indent=2, default=str))

            elif command == "load-config":
                config = await sync.load_config_from_supabase()
                print(json.dumps(config, indent=2, default=str))

            else:
                print(f"Unknown command: {command}")

        finally:
            await sync.close()

    asyncio.run(main())