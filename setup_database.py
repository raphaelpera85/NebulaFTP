from __future__ import annotations

import argparse
import asyncio
from os import environ
from os.path import exists

if exists(".env"):
    from dotenv import load_dotenv

    load_dotenv()


def _parse_args():
    parser = argparse.ArgumentParser(description="Create NebulaFTP MongoDB indexes")
    parser.add_argument("--uri", help="MongoDB connection string (defaults to MONGODB env var)")
    return parser.parse_args()


async def main():
    args = _parse_args()
    mongo_uri = args.uri or environ.get("MONGODB")
    if not mongo_uri:
        raise SystemExit("FATAL: MONGODB is required")

    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except Exception as exc:
        raise SystemExit(f"FATAL: motor unavailable: {exc}") from exc

    client = AsyncIOMotorClient(mongo_uri)
    db = client.ftp

    await db.files.create_index([("parent", 1), ("name", 1)], unique=True)
    await db.files.create_index("parent")
    await db.files.create_index("uploadId", sparse=True)
    await db.files.create_index("uploaded_at")
    await db.files.create_index("status")
    await db.users.create_index("login", unique=True)

    print("Database indexes created.")


if __name__ == "__main__":
    asyncio.run(main())
