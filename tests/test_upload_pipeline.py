"""Tests for upload pipeline: bot rotation, read-ahead, BytesIO wrapping."""
from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ---------------------------------------------------------------------------
# Helpers: stub pyrogram types before importing main.py
# ---------------------------------------------------------------------------

class FakeFloodWait(Exception):
    """Stub for pyrogram.errors.FloodWait with a .value attribute."""
    def __init__(self, seconds: int):
        super().__init__(seconds)
        self.value = seconds


class FakeRPCError(Exception):
    """Stub for pyrogram.errors.RPCError."""
    pass


def _make_bot(name: str = "bot") -> AsyncMock:
    bot = AsyncMock()
    bot.name = name
    return bot


def _make_sent_msg(file_id: str = "file123", msg_id: int = 1) -> MagicMock:
    sent = MagicMock()
    sent.document.file_id = file_id
    sent.id = msg_id
    return sent


# Patch pyrogram errors in main.py's namespace BEFORE loading it
_fake_errors = MagicMock()
_fake_errors.FloodWait = FakeFloodWait
_fake_errors.RPCError = FakeRPCError

_fake_pyrogram = MagicMock()
sys.modules["pyrogram"] = _fake_pyrogram
sys.modules["pyrogram.errors"] = _fake_errors
sys.modules["pyrogram.session"] = MagicMock()
sys.modules["pyrogram.session.internals"] = MagicMock()
sys.modules["pyrogram.session.internals.msg_id"] = MagicMock()
_fake_pyrogram_utils = MagicMock()
_fake_pyrogram_utils.MIN_CHANNEL_ID = -1001000000000
_fake_pyrogram.utils = _fake_pyrogram_utils
sys.modules["pyrogram.utils"] = _fake_pyrogram_utils
sys.modules["motor"] = MagicMock()
sys.modules["motor.motor_asyncio"] = MagicMock()
sys.modules["pymongo"] = MagicMock()
_fake_pymongo_errors = MagicMock()
_fake_pymongo_errors.DuplicateKeyError = type("DuplicateKeyError", (Exception,), {})
sys.modules["pymongo.errors"] = _fake_pymongo_errors

# Stub tools.check_deps so ensure_runtime_dependencies is a no-op
_fake_check_deps = MagicMock()
_fake_check_deps.ensure_runtime_dependencies = MagicMock()
sys.modules["tools"] = MagicMock()
sys.modules["tools.check_deps"] = _fake_check_deps

# Stub remaining imports main.py needs (aiofiles is real and installed)
sys.modules["bson"] = MagicMock()
sys.modules["bson.objectid"] = MagicMock()

# Stub ftp package so main.py can `from ftp import ...`
_fake_ftp = MagicMock()
sys.modules["ftp"] = _fake_ftp
sys.modules["ftp.common"] = _fake_ftp
sys.modules["ftp.server"] = _fake_ftp
sys.modules["ftp.auth"] = _fake_ftp
sys.modules["ftp.pathio"] = _fake_ftp
sys.modules["ftp.tg"] = _fake_ftp
sys.modules["ftp.errors"] = _fake_ftp
sys.modules["ftp.range"] = _fake_ftp

# Now load main
spec = importlib.util.spec_from_file_location("main", os.path.join(ROOT, "main.py"))
main_mod = importlib.util.module_from_spec(spec)
sys.modules["main"] = main_mod
spec.loader.exec_module(main_mod)

# Rebind the patched errors into the loaded module so isinstance works
main_mod.FloodWait = FakeFloodWait
main_mod.RPCError = FakeRPCError

upload_part_with_retries = main_mod.upload_part_with_retries
_readahead_producer = main_mod._readahead_producer


@pytest.fixture(autouse=True)
def reset_upload_bot_cursor():
    main_mod.UPLOAD_BOT_CURSOR = 0
    yield


# ===========================================================================
# 1. Bot rotation tests
# ===========================================================================

@pytest.mark.asyncio
async def test_global_upload_concurrency_limit(monkeypatch):
    monkeypatch.setattr(main_mod, "UPLOAD_CONCURRENCY", 2)
    main_mod._UPLOAD_SEMAPHORE = None
    main_mod._UPLOAD_SEMAPHORE_LOOP = None
    active = 0
    peak = 0

    async def guarded_upload():
        nonlocal active, peak
        async with main_mod.get_upload_semaphore():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(guarded_upload() for _ in range(5)))
    assert peak == 2


def test_worker_count_is_bounded_by_real_transmission_slots(monkeypatch):
    monkeypatch.setattr(main_mod, "MAX_WORKERS", 24)
    monkeypatch.setattr(main_mod, "UPLOAD_CONCURRENCY", 8)
    monkeypatch.setattr(main_mod, "PART_WORKERS_PER_FILE", 2)

    assert main_mod.get_upload_worker_count(23) == 4


def test_staging_path_detection(monkeypatch, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(main_mod, "STAGING_DIRS", [str(staging.resolve())])

    assert main_mod.is_staging_path(staging / "movie.mkv") is True
    assert main_mod.is_staging_path(tmp_path / "source" / "movie.mkv") is False


def test_source_cleanup_ignores_legacy_global_delete(monkeypatch, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    monkeypatch.setattr(main_mod, "STAGING_DIRS", [str(staging.resolve())])
    monkeypatch.setenv("DELETE_SOURCE_AFTER_UPLOAD", "true")

    main_mod.safe_remove_staging_file(source)

    assert source.exists()


def test_resume_uses_only_contiguous_completed_parts():
    parts = [
        {"part_id": 0, "tg_file": "f0", "file_size": 64},
        {"part_id": 1, "tg_file": "f1", "file_size": 64},
        {"part_id": 3, "tg_file": "f3", "file_size": 64},
    ]

    assert [part["part_id"] for part in main_mod.get_contiguous_uploaded_parts(parts, 4)] == [0, 1]


@pytest.mark.asyncio
async def test_bot_api_upload_continues_when_streaming_starts(monkeypatch):
    bot = MagicMock()
    bot._nebula_bot_token = "token"
    bot._nebula_streams = 0
    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_upload(*_args):
        started.set()
        await finish.wait()
        return _make_sent_msg("f0", 1)

    monkeypatch.setattr(main_mod, "send_document_bot_api", slow_upload)
    task = asyncio.create_task(
        main_mod._send_part_document(bot, "chat", b"data", "part.bin")
    )
    await started.wait()
    bot._nebula_streams = 1

    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    assert (await task).document.file_id == "f0"


@pytest.mark.asyncio
async def test_upload_marks_bot_busy_and_releases_it(tmp_path):
    main_mod.CHUNK_SIZE = 4
    fpath = tmp_path / "file.bin"
    fpath.write_bytes(b"data")
    bot = _make_bot("bot0")

    async def send_document(**_kwargs):
        assert bot._nebula_uploads == 1
        return _make_sent_msg("f0", 1)

    bot.send_document.side_effect = send_document
    await upload_part_with_retries(1, [bot], "chat", str(fpath), "uuid", 0)
    assert bot._nebula_uploads == 0


@pytest.mark.asyncio
async def test_upload_releases_bot_before_retry_backoff(monkeypatch):
    bot = _make_bot("bot0")
    bot.send_document.side_effect = ConnectionError("network down")

    with pytest.raises(ConnectionError, match="network down"):
        await main_mod._send_part_on_idle_bot(
            bot, "chat", b"data", "part.bin"
        )

    assert bot._nebula_uploads == 0


class TestBotRotation:
    """upload_part_with_retries should rotate bots on FloodWait."""

    @pytest.mark.asyncio
    async def test_success_uses_initial_bot(self, tmp_path):
        """Normal path: first bot succeeds, no rotation."""
        CHUNK_SIZE = 1024  # override for testing
        main_mod.CHUNK_SIZE = CHUNK_SIZE

        data = b"x" * 2048  # 2 chunks
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)

        bot0 = _make_bot("bot0")
        bot1 = _make_bot("bot1")
        bot0.send_document.return_value = _make_sent_msg("f0", 1)
        bots = [bot0, bot1]

        # part_num=0 → initial_bot_idx = 0 % 2 = 0 → bot0
        result = await upload_part_with_retries(
            worker_id=1,
            bots=bots,
            target_chat_id="chat1",
            local_path=str(fpath),
            file_uuid="uuid-test",
            part_num=0,
        )

        assert result["bot_index"] == 0
        assert result["file_size"] == CHUNK_SIZE
        bot0.send_document.assert_awaited_once()
        bot1.send_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successive_uploads_rotate_starting_bot(self, tmp_path):
        main_mod.CHUNK_SIZE = 1024
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(b"x" * 1024)
        bots = [_make_bot(f"bot{index}") for index in range(3)]
        bots[0].send_document.return_value = _make_sent_msg("f0", 1)
        bots[1].send_document.return_value = _make_sent_msg("f1", 2)

        first = await upload_part_with_retries(
            worker_id=1,
            bots=bots,
            target_chat_id="chat1",
            local_path=str(fpath),
            file_uuid="uuid-test",
            part_num=0,
        )
        second = await upload_part_with_retries(
            worker_id=2,
            bots=bots,
            target_chat_id="chat1",
            local_path=str(fpath),
            file_uuid="uuid-test-2",
            part_num=0,
        )

        assert first["bot_index"] == 0
        assert second["bot_index"] == 1
        bots[0].send_document.assert_awaited_once()
        bots[1].send_document.assert_awaited_once()
        bots[2].send_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_floodwait_rotates_to_next_bot(self, tmp_path):
        """First bot FloodWait → rotate to second bot which succeeds."""
        CHUNK_SIZE = 1024
        main_mod.CHUNK_SIZE = CHUNK_SIZE

        data = b"y" * 1024
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)

        bot0 = _make_bot("bot0")
        bot1 = _make_bot("bot1")
        bot0.send_document.side_effect = FakeFloodWait(0)  # instant FloodWait
        bot1.send_document.return_value = _make_sent_msg("f1", 2)
        bots = [bot0, bot1]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await upload_part_with_retries(
                worker_id=1,
                bots=bots,
                target_chat_id="chat1",
                local_path=str(fpath),
                file_uuid="uuid-test",
                part_num=0,
            )

        # After rotation: bot_index should be 1 (rotated from 0 → 1)
        assert result["bot_index"] == 1
        bot0.send_document.assert_awaited_once()
        bot1.send_document.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_timeout_rotates_to_next_bot(self, tmp_path):
        main_mod.CHUNK_SIZE = 1024
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(b"x" * 1024)
        bot0 = _make_bot("bot0")
        bot1 = _make_bot("bot1")
        bot0.send_document.side_effect = TimeoutError("slow upload")
        bot1.send_document.return_value = _make_sent_msg("f1", 2)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await upload_part_with_retries(
                worker_id=1,
                bots=[bot0, bot1],
                target_chat_id="chat1",
                local_path=str(fpath),
                file_uuid="uuid-test",
                part_num=0,
            )

        assert result["bot_index"] == 1
        bot0.send_document.assert_awaited_once()
        bot1.send_document.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_floodwait_rotates_multiple_times(self, tmp_path):
        """Two bots FloodWait → rotates past both, succeeds on third attempt (same bot)."""
        CHUNK_SIZE = 1024
        main_mod.CHUNK_SIZE = CHUNK_SIZE

        data = b"z" * 1024
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)

        bot0 = _make_bot("bot0")
        bot1 = _make_bot("bot1")
        # Both FloodWait once, then bot0 succeeds on retry
        bot0.send_document.side_effect = [
            FakeFloodWait(0),
            _make_sent_msg("f0", 3),
        ]
        bot1.send_document.side_effect = FakeFloodWait(0)
        bots = [bot0, bot1]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await upload_part_with_retries(
                worker_id=1,
                bots=bots,
                target_chat_id="chat1",
                local_path=str(fpath),
                file_uuid="uuid-test",
                part_num=0,
            )

        # Rotated: 0→1 (FloodWait) → 2%2=0 (FloodWait) → back to 0 (success)
        assert result["bot_index"] == 0
        assert bot0.send_document.await_count == 2
        assert bot1.send_document.await_count == 1

    @pytest.mark.asyncio
    async def test_successive_parts_rotate_across_bots(self, tmp_path, monkeypatch):
        """Successive parts advance the shared round-robin cursor."""
        CHUNK_SIZE = 1024
        main_mod.CHUNK_SIZE = CHUNK_SIZE
        monkeypatch.setattr(main_mod, "PART_WORKERS_PER_FILE", 2)

        data = b"w" * 2048
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)

        bot0 = _make_bot("bot0")
        bot1 = _make_bot("bot1")
        bot0.send_document.return_value = _make_sent_msg("f0", 1)
        bot1.send_document.return_value = _make_sent_msg("f1", 2)
        bots = [bot0, bot1]

        first = await upload_part_with_retries(
            worker_id=1,
            bots=bots,
            target_chat_id="chat1",
            local_path=str(fpath),
            file_uuid="uuid-test",
            part_num=0,
        )
        second = await upload_part_with_retries(
            worker_id=1,
            bots=bots,
            target_chat_id="chat1",
            local_path=str(fpath),
            file_uuid="uuid-test",
            part_num=1,
        )

        assert first["bot_index"] == 0
        assert second["bot_index"] == 1
        bot0.send_document.assert_awaited_once()
        bot1.send_document.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rpcerror_retries_same_bot(self, tmp_path):
        """RPCError should retry same bot (exponential backoff), not rotate."""
        CHUNK_SIZE = 1024
        main_mod.CHUNK_SIZE = CHUNK_SIZE

        data = b"r" * 1024
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)

        bot0 = _make_bot("bot0")
        bot1 = _make_bot("bot1")
        bot0.send_document.side_effect = [
            FakeRPCError("server error"),
            _make_sent_msg("f0", 1),
        ]
        bots = [bot0, bot1]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await upload_part_with_retries(
                worker_id=1,
                bots=bots,
                target_chat_id="chat1",
                local_path=str(fpath),
                file_uuid="uuid-test",
                part_num=0,
            )

        # RPCError doesn't rotate: stays on bot0
        assert result["bot_index"] == 0
        assert bot0.send_document.await_count == 2
        bot1.send_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_document_uses_bytesio(self, tmp_path):
        """Verify send_document receives io.BytesIO (pyrogram needs file-like with .read())."""
        CHUNK_SIZE = 1024
        main_mod.CHUNK_SIZE = CHUNK_SIZE

        data = b"b" * 1024
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)

        bot0 = _make_bot("bot0")
        bot0.send_document.return_value = _make_sent_msg("f0", 1)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await upload_part_with_retries(
                worker_id=1,
                bots=[bot0],
                target_chat_id="chat1",
                local_path=str(fpath),
                file_uuid="uuid-test",
                part_num=0,
            )

        call_kwargs = bot0.send_document.call_args
        document_arg = call_kwargs.kwargs.get("document") or call_kwargs[1].get("document")
        # Must be io.BytesIO — pyrogram requires a file-like object with .read()
        assert isinstance(document_arg, io.BytesIO)
        document_arg.seek(0)
        assert document_arg.read() == data

    @pytest.mark.asyncio
    async def test_chunk_data_shortcuts_disk_read(self, tmp_path):
        """Providing chunk_data should skip reopening the file."""
        CHUNK_SIZE = 1024
        main_mod.CHUNK_SIZE = CHUNK_SIZE

        data = b"c" * 1024
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)

        bot0 = _make_bot("bot0")
        bot0.send_document.return_value = _make_sent_msg("f0", 1)

        with patch.object(main_mod.aiofiles, "open") as mock_open:
            await upload_part_with_retries(
                worker_id=1,
                bots=[bot0],
                target_chat_id="chat1",
                local_path=str(fpath),
                file_uuid="uuid-test",
                part_num=0,
                chunk_data=data,
            )

        mock_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_chunk_raises(self, tmp_path):
        """Reading past EOF should raise."""
        CHUNK_SIZE = 1024
        main_mod.CHUNK_SIZE = CHUNK_SIZE

        fpath = tmp_path / "file.bin"
        fpath.write_bytes(b"x" * 100)

        bot0 = _make_bot("bot0")
        # part_num=5 is past EOF → chunk_data will be empty bytes
        with pytest.raises(Exception, match="Parte vazia"):
            await upload_part_with_retries(
                worker_id=1,
                bots=[bot0],
                target_chat_id="chat1",
                local_path=str(fpath),
                file_uuid="uuid-test",
                part_num=5,
            )

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_raises(self, tmp_path):
        """If all retries FloodWait, should raise."""
        main_mod.CHUNK_SIZE = 1024
        main_mod.MAX_RETRIES = 3

        data = b"e" * 1024
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)

        bot0 = _make_bot("bot0")
        bot0.send_document.side_effect = FakeFloodWait(0)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(Exception, match="Falha upload"):
                await upload_part_with_retries(
                    worker_id=1,
                    bots=[bot0],
                    target_chat_id="chat1",
                    local_path=str(fpath),
                    file_uuid="uuid-test",
                    part_num=0,
                )

    @pytest.mark.asyncio
    async def test_single_bot_no_rotation(self, tmp_path):
        """With only one bot, FloodWait retries same bot."""
        CHUNK_SIZE = 1024
        main_mod.CHUNK_SIZE = CHUNK_SIZE

        data = b"s" * 2048
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)

        bot0 = _make_bot("bot0")
        bot0.send_document.side_effect = [
            FakeFloodWait(0),
            _make_sent_msg("f0", 1),
        ]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await upload_part_with_retries(
                worker_id=1,
                bots=[bot0],
                target_chat_id="chat1",
                local_path=str(fpath),
                file_uuid="uuid-test",
                part_num=0,
            )

        # With one bot, rotation wraps to same bot
        assert result["bot_index"] == 0
        assert bot0.send_document.await_count == 2


# ===========================================================================
# 2. Read-ahead producer tests
# ===========================================================================

class TestReadaheadProducer:
    """_readahead_producer should queue all chunks + None sentinel."""

    @pytest.mark.asyncio
    async def test_all_chunks_queued(self, tmp_path):
        """Every chunk appears in the queue in order."""
        CHUNK_SIZE = 256
        main_mod.CHUNK_SIZE = CHUNK_SIZE

        data = b"a" * 700  # 3 chunks: 256 + 256 + 188
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)

        queue: asyncio.Queue = asyncio.Queue()
        await _readahead_producer(str(fpath), 3, queue, worker_id=1)

        chunks = []
        while True:
            item = await queue.get()
            if item is None:
                break
            part_num, chunk_data = item
            chunks.append((part_num, chunk_data))

        assert len(chunks) == 3
        assert [c[0] for c in chunks] == [0, 1, 2]
        # Verify data integrity
        assert chunks[0][1] == data[0:256]
        assert chunks[1][1] == data[256:512]
        assert chunks[2][1] == data[512:700]

    @pytest.mark.asyncio
    async def test_resume_starts_at_requested_part(self, tmp_path):
        main_mod.CHUNK_SIZE = 4
        data = b"aaaabbbbcccc"
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)
        queue: asyncio.Queue = asyncio.Queue()

        await _readahead_producer(str(fpath), 3, queue, worker_id=1, start_part=2)

        assert await queue.get() == (2, b"cccc")
        assert await queue.get() is None

    @pytest.mark.asyncio
    async def test_resume_can_start_after_larger_existing_parts(self, tmp_path):
        main_mod.CHUNK_SIZE = 4
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(b"aaaabbbbcccc")
        queue: asyncio.Queue = asyncio.Queue()

        await _readahead_producer(
            str(fpath),
            3,
            queue,
            worker_id=1,
            start_part=2,
            start_offset=4,
        )

        assert await queue.get() == (2, b"bbbb")

    @pytest.mark.asyncio
    async def test_none_sentinel_at_end(self, tmp_path):
        """Queue ends with None after all chunks."""
        CHUNK_SIZE = 512
        main_mod.CHUNK_SIZE = CHUNK_SIZE

        data = b"b" * 1024
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)

        queue: asyncio.Queue = asyncio.Queue()
        await _readahead_producer(str(fpath), 2, queue, worker_id=1)

        items = []
        while not queue.empty():
            items.append(await queue.get())

        assert items[-1] is None
        assert len(items) == 3  # chunk0, chunk1, None

    @pytest.mark.asyncio
    async def test_single_chunk_file(self, tmp_path):
        """File smaller than CHUNK_SIZE produces one chunk + None."""
        CHUNK_SIZE = 4096
        main_mod.CHUNK_SIZE = CHUNK_SIZE

        data = b"c" * 100
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)

        queue: asyncio.Queue = asyncio.Queue()
        await _readahead_producer(str(fpath), 1, queue, worker_id=1)

        item0 = await queue.get()
        sentinel = await queue.get()

        assert item0[0] == 0
        assert item0[1] == data
        assert sentinel is None

    @pytest.mark.asyncio
    async def test_bounded_queue_respected(self, tmp_path):
        """Producer blocks when queue is full (maxsize respected)."""
        CHUNK_SIZE = 256
        main_mod.CHUNK_SIZE = CHUNK_SIZE

        data = b"d" * 1024  # 4 chunks
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)

        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        producer = asyncio.create_task(
            _readahead_producer(str(fpath), 4, queue, worker_id=1)
        )

        # Give producer time to fill queue up to maxsize
        await asyncio.sleep(0.1)

        # Queue should be full (2 items), producer should be suspended
        assert queue.qsize() == 2

        # Drain one to unblock producer
        await queue.get()
        await asyncio.sleep(0.05)
        # Producer should have added one more
        assert queue.qsize() == 2

        # Cancel producer to clean up
        producer.cancel()
        try:
            await producer
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_empty_total_parts(self, tmp_path):
        """Zero total_parts should just enqueue None."""
        CHUNK_SIZE = 1024
        main_mod.CHUNK_SIZE = CHUNK_SIZE

        fpath = tmp_path / "file.bin"
        fpath.write_bytes(b"e" * 100)

        queue: asyncio.Queue = asyncio.Queue()
        await _readahead_producer(str(fpath), 0, queue, worker_id=1)

        sentinel = await queue.get()
        assert sentinel is None
        assert queue.empty()


# ===========================================================================
# 3. Integration: read-ahead + bot rotation together
# ===========================================================================

class TestUploadIntegration:
    """End-to-end: read-ahead feeds chunks, bot rotation handles failures."""

    @pytest.mark.asyncio
    async def test_readahead_feeds_upload(self, tmp_path):
        """Read-ahead produces chunks that upload_part_with_retries consumes."""
        CHUNK_SIZE = 256
        main_mod.CHUNK_SIZE = CHUNK_SIZE

        data = b"z" * 512  # 2 parts
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)

        bot0 = _make_bot("bot0")
        bot0.send_document.return_value = _make_sent_msg("f0", 1)

        queue: asyncio.Queue = asyncio.Queue()
        await _readahead_producer(str(fpath), 2, queue, worker_id=1)

        # Consume from queue and upload each part
        results = []
        while True:
            item = await queue.get()
            if item is None:
                break
            part_num, chunk_data = item
            result = await upload_part_with_retries(
                worker_id=1,
                bots=[bot0],
                target_chat_id="chat1",
                local_path=str(fpath),
                file_uuid="uuid-integ",
                part_num=part_num,
                chunk_data=chunk_data,
            )
            results.append(result)

        assert len(results) == 2
        assert results[0]["part_id"] == 0
        assert results[1]["part_id"] == 1
        assert bot0.send_document.await_count == 2


def test_safe_remove_staging_file_cleans_empty_and_metadata_folders(monkeypatch, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    source_dir = tmp_path / "Nova pasta" / "Filmes" / "Movie (2026)"
    source_dir.mkdir(parents=True)
    video = source_dir / "Movie (2026).mkv"
    video.write_bytes(b"video data")
    poster = source_dir / "poster.jpg"
    poster.write_bytes(b"poster data")

    monkeypatch.setattr(main_mod, "STAGING_DIRS", [str(staging.resolve())])
    main_mod.safe_remove_staging_file(video, force_delete=True)

    assert not video.exists()
    assert not source_dir.exists()


def test_safe_remove_staging_file_preserves_folder_with_strm_files(monkeypatch, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    source_dir = tmp_path / "Nova pasta" / "Filmes" / "Movie (2026)"
    source_dir.mkdir(parents=True)
    video = source_dir / "Movie (2026).mkv"
    video.write_bytes(b"video data")
    strm = source_dir / "Movie (2026) Extra.strm"
    strm.write_text("https://example.com/extra.mkv", encoding="utf-8")

    monkeypatch.setattr(main_mod, "STAGING_DIRS", [str(staging.resolve())])
    main_mod.safe_remove_staging_file(video, force_delete=True)

    assert not video.exists()
    assert source_dir.exists()
    assert strm.exists()


@pytest.mark.asyncio
async def test_restore_pending_uploads_prioritizes_oldest_downloaded_file(tmp_path, monkeypatch):
    file_old = tmp_path / "old_download.mkv"
    file_med = tmp_path / "med_download.mkv"
    file_new = tmp_path / "new_download.mkv"
    file_old.write_bytes(b"old")
    file_med.write_bytes(b"med")
    file_new.write_bytes(b"new")

    # Mock MongoDB
    docs = [
        {"_id": "3", "name": "new.mkv", "parent": "/Filmes", "status": "queued", "local_path": str(file_new), "mtime": 3000},
        {"_id": "1", "name": "old.mkv", "parent": "/Filmes", "status": "queued", "local_path": str(file_old), "mtime": 1000},
        {"_id": "2", "name": "med.mkv", "parent": "/Filmes", "status": "queued", "local_path": str(file_med), "mtime": 2000},
    ]

    class FakeCursor:
        def __init__(self, items):
            self.items = items
        def __aiter__(self):
            return self
        async def __anext__(self):
            if not self.items:
                raise StopAsyncIteration
            return self.items.pop(0)

    class FakeFiles:
        def find(self, query):
            return FakeCursor(list(docs))
        async def find_one(self, *a, **k):
            return None
        async def update_one(self, *a, **k):
            pass

    class FakeMongo:
        files = FakeFiles()

    queue = asyncio.Queue()
    monkeypatch.setattr(main_mod, "UPLOAD_QUEUE", queue)
    async def fake_resolve(*a, **k):
        return a[1]

    monkeypatch.setattr(main_mod, "resolve_media_parent", fake_resolve)
    monkeypatch.setattr(main_mod, "log_queue_state", AsyncMock())

    await main_mod.restore_pending_uploads(FakeMongo())

    enqueued = []
    while not queue.empty():
        item = queue.get_nowait()
        enqueued.append(item["filename"])

    assert enqueued == ["old.mkv", "med.mkv", "new.mkv"]


@pytest.mark.asyncio
async def test_queued_mongo_scanner_requests_oldest_first(tmp_path, monkeypatch):
    file_old = tmp_path / "old.mkv"
    file_old.write_bytes(b"old")

    captured_calls = []

    class FakeFiles:
        async def find_one_and_update(self, q, update, sort=None, **kwargs):
            captured_calls.append({"query": q, "update": update, "sort": sort})
            if len(captured_calls) == 1:
                return {"_id": "1", "name": "old.mkv", "parent": "/Filmes", "status": "queued", "local_path": str(file_old), "mtime": 1000}
            return None
        async def update_one(self, *a, **k):
            pass

    class FakeMongo:
        files = FakeFiles()

    queue = asyncio.Queue()
    monkeypatch.setattr(main_mod, "UPLOAD_QUEUE", queue)
    async def fake_resolve(*a, **k):
        return a[1]
    monkeypatch.setattr(main_mod, "resolve_media_parent", fake_resolve)

    # Run single iteration of scanner by cancelling sleep
    orig_sleep = asyncio.sleep
    async def fake_sleep(sec):
        raise asyncio.CancelledError()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    try:
        await main_mod.queued_mongo_scanner(FakeMongo(), max_workers=1)
    except asyncio.CancelledError:
        pass

    assert len(captured_calls) >= 1
    assert captured_calls[0]["sort"] == [("mtime", 1), ("ctime", 1), ("_id", 1)]
    assert not queue.empty()
    item = queue.get_nowait()
    assert item["filename"] == "old.mkv"
