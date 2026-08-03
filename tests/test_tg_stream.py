import asyncio
import io
from unittest.mock import AsyncMock, MagicMock

import pytest

from ftp import tg
from ftp.tg import GETFILE_CHUNK_SIZE, File, get_file_limit


def test_stream_uses_telegram_maximum_download_chunk():
    assert GETFILE_CHUNK_SIZE == 1024 * 1024
    assert get_file_limit(0) == 1024 * 1024
    assert get_file_limit(4096) == 1024 * 1024 - 4096
    assert get_file_limit(1024 * 1024) == 1024 * 1024


@pytest.mark.asyncio
async def test_stream_continues_after_short_telegram_chunk():
    file = object.__new__(File)
    responses = iter([b"short", b"next", b""])

    async def get_chunk(offset=0):
        return next(responses)

    file.getChunkAt = get_chunk

    assert [chunk async for chunk in file.stream()] == [b"short", b"next"]


@pytest.mark.asyncio
async def test_stream_aligns_telegram_offset_and_discards_prefix():
    file = object.__new__(File)
    offsets = []

    async def get_chunk(offset=0):
        offsets.append(offset)
        return b"0123456789" if len(offsets) == 1 else b""

    file.getChunkAt = get_chunk

    assert [chunk async for chunk in file.stream(offset=3)] == [b"3456789"]
    assert offsets == [0, 10]


@pytest.mark.asyncio
async def test_sequential_upload_propagates_part_failure(monkeypatch):
    session = MagicMock()
    session.start = AsyncMock()
    session.stop = AsyncMock()
    session.invoke = AsyncMock(side_effect=TimeoutError("upload timeout"))
    monkeypatch.setattr(tg, "Session", MagicMock(return_value=session))

    client = MagicMock()
    client.save_file_semaphore = asyncio.Semaphore(1)
    client.storage.dc_id = AsyncMock(return_value=1)
    client.storage.auth_key = AsyncMock(return_value=b"key")
    client.storage.test_mode = AsyncMock(return_value=False)
    client.me.is_premium = False
    client.rnd_id.return_value = 123

    payload = io.BytesIO(b"x" * 1024)
    payload.name = "part.bin"

    with pytest.raises(TimeoutError, match="upload timeout"):
        await tg.sequential_save_file(client, payload)

    session.stop.assert_awaited_once()


def test_reliable_upload_replaces_effective_pyrogram_method():
    tg.install_reliable_upload()

    assert tg.Methods.save_file is tg.sequential_save_file
