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


@pytest.mark.asyncio
async def test_bot_api_upload_reuses_and_closes_http_session(monkeypatch):
    class FakeResponse:
        status = 200

        async def json(self, content_type=None):
            return {
                "ok": True,
                "result": {
                    "message_id": 7,
                    "document": {"file_id": "telegram-file"},
                },
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    class FakeSession:
        closed = False

        def __init__(self):
            self.posts = 0

        def post(self, *_args, **_kwargs):
            self.posts += 1
            return FakeResponse()

        async def close(self):
            self.closed = True

    sessions = []

    def make_session(**_kwargs):
        session = FakeSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(tg.aiohttp, "ClientSession", make_session)
    bot = MagicMock()
    fake_credential = "test-token"
    setattr(bot, "_nebula_bot_token", fake_credential)  # noqa: B010 - MagicMock attribute

    first = await tg.send_document_bot_api(bot, "chat", b"part", "first.bin")
    second = await tg.send_document_bot_api(bot, "chat", b"part", "second.bin")

    assert first.document.file_id == second.document.file_id == "telegram-file"
    assert len(sessions) == 1
    assert sessions[0].posts == 2
    await tg.close_upload_http_sessions([bot])
    assert sessions[0].closed


@pytest.mark.asyncio
async def test_bot_api_upload_preserves_429_retry_after(monkeypatch):
    class FakeResponse:
        status = 429

        async def json(self, content_type=None):
            return {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 17},
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    class FakeSession:
        closed = False

        def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(tg.aiohttp, "ClientSession", lambda **_kwargs: FakeSession())
    bot = MagicMock()
    setattr(bot, "_nebula_bot_token", "test-token")  # noqa: B010

    with pytest.raises(tg.TelegramBotAPIError) as raised:
        await tg.send_document_bot_api(bot, "chat", b"part", "part.bin")

    assert raised.value.code == 429
    assert raised.value.retry_after == 17


def test_reliable_upload_replaces_effective_pyrogram_method():
    tg.install_reliable_upload()

    assert tg.Methods.save_file is tg.sequential_save_file
