import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ftp import pathio


def test_cache_identity_survives_telegram_file_reference_refresh():
    node = pathio.Node(
        type="file",
        name="episode.mkv",
        size=1234,
        _id="stable-media-id",
        mtime=1700000000,
        parts=[{"part_id": 0, "tg_file": "old-reference"}],
    )
    original_cache, _ = pathio._media_cache_paths(node)

    node.parts[0]["tg_file"] = "refreshed-reference"
    refreshed_cache, _ = pathio._media_cache_paths(node)

    assert refreshed_cache == original_cache


@pytest.mark.asyncio
async def test_prefetch_stops_when_last_player_reader_closes(monkeypatch, tmp_path):
    payload = b"MKV|AUDIO_PT|AUDIO_EN|SUBTITLE_SRT"
    monkeypatch.setattr(pathio, "MEDIA_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(pathio, "MEDIA_CACHE_MAX_BYTES", 1024**3)
    monkeypatch.setattr(
        pathio.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100 * 1024**3, free=90 * 1024**3),
    )
    calls = 0

    class Bot:
        def __init__(self, name):
            self.name = name
            self._nebula_streams = 0

    class FakeFile:
        reference_refreshed = False
        file_id = "telegram-file"

        def __init__(self, _file_id, _client, **_kwargs):
            pass

        async def stream(self, offset=0):
            nonlocal calls
            calls += 1
            yield payload[offset:]

    monkeypatch.setattr(pathio, "File", FakeFile)
    db = SimpleNamespace(files=SimpleNamespace(update_one=AsyncMock()))
    node = pathio.Node(
        type="file",
        name="movie.mkv",
        size=len(payload),
        _id="cache-test-media",
        parts=[{
            "part_id": 0,
            "file_size": len(payload),
            "tg_file": "telegram-file",
            "tg_message": 1,
            "bot_index": 0,
            "bot_name": "bot1",
        }],
    )
    bots = [Bot("bot1"), Bot("bot2")]

    first_reader = pathio.MongoDBMemoryIO(node, "rb", bots, db)
    async with pathio._MEDIA_CACHE_PREFETCH_LIMIT:
        assert b"".join([chunk async for chunk in first_reader.iter_by_block(8)]) == payload
        cache_tasks = list(pathio._MEDIA_CACHE_DOWNLOADS.values())
        assert cache_tasks
        assert all(task.cancelled() for task in cache_tasks)

    cache_path, _ = pathio._media_cache_paths(node)
    assert not tmp_path.joinpath(os.path.basename(cache_path)).exists()
    assert not tmp_path.joinpath(os.path.basename(cache_path).replace(".cache", ".part")).exists()

    tmp_path.joinpath(os.path.basename(cache_path)).write_bytes(payload)

    cached_reader = pathio.MongoDBMemoryIO(node, "rb", bots, db)
    await cached_reader.seek(4)
    assert b"".join([chunk async for chunk in cached_reader.iter_by_block(5)]) == payload[4:]
    assert calls == 1  # cached playback does not make another Telegram request


@pytest.mark.asyncio
async def test_cache_quota_falls_back_to_live_telegram_stream(monkeypatch, tmp_path):
    payload = b"original-media"
    monkeypatch.setattr(pathio, "MEDIA_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(pathio, "MEDIA_CACHE_MAX_BYTES", 1)

    class Bot:
        name = "bot1"
        _nebula_streams = 0

    class FakeFile:
        reference_refreshed = False
        file_id = "telegram-file"

        def __init__(self, _file_id, _client, **_kwargs):
            pass

        async def stream(self, offset=0):
            yield payload[offset:]

    monkeypatch.setattr(pathio, "File", FakeFile)
    node = pathio.Node(
        type="file",
        name="movie.mkv",
        size=len(payload),
        _id="over-quota-media",
        parts=[{
            "part_id": 0,
            "file_size": len(payload),
            "tg_file": "telegram-file",
            "bot_index": 0,
            "bot_name": "bot1",
        }],
    )
    reader = pathio.MongoDBMemoryIO(node, "rb", [Bot()], SimpleNamespace(files=None))

    assert b"".join([chunk async for chunk in reader.iter_by_block(16)]) == payload
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_idle_prefetch_is_cancelled_after_media_ttl(monkeypatch, tmp_path):
    monkeypatch.setattr(pathio, "MEDIA_CACHE_TTL_SECONDS", 900)
    cache_path = str(tmp_path / ("d" * 64 + ".cache"))
    started = asyncio.Event()

    async def prefetch():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(prefetch())
    pathio._MEDIA_CACHE_DOWNLOADS[cache_path] = task
    pathio._MEDIA_CACHE_LAST_ACCESS[cache_path] = 1000
    await started.wait()

    try:
        cancelled = await pathio.cancel_inactive_media_cache_downloads(now=1901)

        assert cancelled == 1
        assert task.cancelled()
    finally:
        pathio._MEDIA_CACHE_DOWNLOADS.pop(cache_path, None)
        pathio._MEDIA_CACHE_LAST_ACCESS.pop(cache_path, None)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_prefetch_waits_until_all_player_readers_close(tmp_path):
    node = pathio.Node(
        type="file",
        name="movie.mkv",
        size=1,
        _id="shared-reader-media",
        parts=[{"part_id": 0, "file_size": 1, "tg_file": "telegram-file"}],
    )
    first = pathio.MongoDBMemoryIO(node, "rb", [], None)
    second = pathio.MongoDBMemoryIO(node, "rb", [], None)
    cache_path, _ = pathio._media_cache_paths(node)
    started = asyncio.Event()

    async def prefetch():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(prefetch())
    pathio._MEDIA_CACHE_DOWNLOADS[cache_path] = task
    await started.wait()

    try:
        await first._register_media_cache_reader(cache_path)
        await second._register_media_cache_reader(cache_path)
        await first._release_media_cache_reader()

        assert not task.done()
        assert pathio._MEDIA_CACHE_READERS[cache_path] == 1

        await second._release_media_cache_reader()

        assert task.cancelled()
        assert cache_path not in pathio._MEDIA_CACHE_READERS
    finally:
        pathio._MEDIA_CACHE_DOWNLOADS.pop(cache_path, None)
        pathio._MEDIA_CACHE_LAST_ACCESS.pop(cache_path, None)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_recently_accessed_prefetch_remains_active(monkeypatch, tmp_path):
    monkeypatch.setattr(pathio, "MEDIA_CACHE_TTL_SECONDS", 900)
    cache_path = str(tmp_path / ("e" * 64 + ".cache"))
    started = asyncio.Event()

    async def prefetch():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(prefetch())
    pathio._MEDIA_CACHE_DOWNLOADS[cache_path] = task
    pathio._MEDIA_CACHE_LAST_ACCESS[cache_path] = 1500
    await started.wait()

    try:
        cancelled = await pathio.cancel_inactive_media_cache_downloads(now=2000)

        assert cancelled == 0
        assert not task.done()
    finally:
        pathio._MEDIA_CACHE_DOWNLOADS.pop(cache_path, None)
        pathio._MEDIA_CACHE_LAST_ACCESS.pop(cache_path, None)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_expiration_removes_only_owned_idle_cache_files(monkeypatch, tmp_path):
    monkeypatch.setattr(pathio, "MEDIA_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(pathio, "MEDIA_CACHE_TTL_SECONDS", 900)
    expired = tmp_path / ("a" * 64 + ".cache")
    active = tmp_path / ("b" * 64 + ".cache")
    unrelated = tmp_path / "keep-me.mkv"
    expired.write_bytes(b"old")
    active.write_bytes(b"active")
    unrelated.write_bytes(b"unrelated")
    old_time = time.time() - 901
    os.utime(expired, (old_time, old_time))
    os.utime(active, (old_time, old_time))
    pathio._MEDIA_CACHE_ACTIVE_PATHS.add(str(active))
    try:
        removed = pathio._cleanup_expired_media_cache_sync()
    finally:
        pathio._MEDIA_CACHE_ACTIVE_PATHS.discard(str(active))

    assert removed == 1
    assert not expired.exists()
    assert active.exists()
    assert unrelated.exists()


def test_expiration_never_scans_inside_upload_staging(monkeypatch, tmp_path):
    stage = tmp_path / "stage"
    cache = stage / "media-cache"
    cache.mkdir(parents=True)
    monkeypatch.setattr(pathio, "CACHE_DIRS", [str(stage)])
    monkeypatch.setattr(pathio, "MEDIA_CACHE_DIR", str(cache))
    old_cache_file = cache / ("c" * 64 + ".cache")
    old_cache_file.write_bytes(b"do not delete staging data")
    old_time = time.time() - 3600
    os.utime(old_cache_file, (old_time, old_time))

    assert pathio._cleanup_expired_media_cache_sync() == 0
    assert old_cache_file.exists()
