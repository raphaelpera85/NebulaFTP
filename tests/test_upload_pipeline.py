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
_fake_pyrogram_utils = MagicMock()
_fake_pyrogram_utils.MIN_CHANNEL_ID = -1001000000000
_fake_pyrogram.utils = _fake_pyrogram_utils
sys.modules["pyrogram.utils"] = _fake_pyrogram_utils
sys.modules["motor"] = MagicMock()
sys.modules["motor.motor_asyncio"] = MagicMock()
sys.modules["pymongo"] = MagicMock()

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


# ===========================================================================
# 1. Bot rotation tests
# ===========================================================================

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
    async def test_initial_bot_selection_uses_modulo(self, tmp_path):
        """Initial bot is chosen by part_num % len(bots)."""
        CHUNK_SIZE = 1024
        main_mod.CHUNK_SIZE = CHUNK_SIZE

        data = b"w" * 2048
        fpath = tmp_path / "file.bin"
        fpath.write_bytes(data)

        bot0 = _make_bot("bot0")
        bot1 = _make_bot("bot1")
        bot0.send_document.return_value = _make_sent_msg("f0", 1)
        bot1.send_document.return_value = _make_sent_msg("f1", 2)
        bots = [bot0, bot1]

        # part_num=1 → 1 % 2 = 1 → starts with bot1
        result = await upload_part_with_retries(
            worker_id=1,
            bots=bots,
            target_chat_id="chat1",
            local_path=str(fpath),
            file_uuid="uuid-test",
            part_num=1,
        )

        assert result["bot_index"] == 1
        bot1.send_document.assert_awaited_once()
        bot0.send_document.assert_not_awaited()

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
