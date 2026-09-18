import asyncio
import pytest
from ftp.common import DualLaneUploadQueue


@pytest.mark.asyncio
async def test_dual_lane_upload_queue_routing():
    q = DualLaneUploadQueue(small_file_max_bytes=50 * 1024 * 1024)

    small_item = {"path": "/fake/small.jpg", "size": 100 * 1024}
    large_item = {"path": "/fake/movie.mkv", "size": 500 * 1024 * 1024}

    assert q.is_small(small_item) is True
    assert q.is_small(large_item) is False

    await q.put(small_item)
    await q.put(large_item)

    assert q.small_qsize() == 1
    assert q.large_qsize() == 1
    assert q.qsize() == 2

    # Small worker gets small item
    got_small = await q.get_small()
    assert got_small["path"] == "/fake/small.jpg"
    q.task_done()

    # Large worker gets large item
    got_large = await q.get_large()
    assert got_large["path"] == "/fake/movie.mkv"
    q.task_done()

    assert q.empty() is True
    await q.join()


@pytest.mark.asyncio
async def test_dual_lane_upload_queue_large_fallback():
    q = DualLaneUploadQueue(small_file_max_bytes=10 * 1024 * 1024)

    # Only put small items
    await q.put({"path": "/fake/thumb.png", "size": 50 * 1024})
    assert q.large_qsize() == 0
    assert q.small_qsize() == 1

    # Large worker with fallback enabled should pick up the small item
    got = await q.get_large(fallback_to_small=True)
    assert got["path"] == "/fake/thumb.png"
    q.task_done()
    assert q.empty() is True


@pytest.mark.asyncio
async def test_dual_lane_upload_queue_priority_in_default_get():
    q = DualLaneUploadQueue(small_file_max_bytes=10 * 1024 * 1024)

    # Put a large item first, then a small item
    await q.put({"path": "/fake/big.mkv", "size": 100 * 1024 * 1024})
    await q.put({"path": "/fake/small.nfo", "size": 2 * 1024})

    # Default get() should prioritize small files to clear the queue rapidly
    first = await q.get()
    assert first["path"] == "/fake/small.nfo"
    q.task_done()

    second = await q.get()
    assert second["path"] == "/fake/big.mkv"
    q.task_done()

    assert q.empty() is True
    await q.join()


@pytest.mark.asyncio
async def test_dual_lane_upload_queue_get_nowait():
    q = DualLaneUploadQueue(small_file_max_bytes=10 * 1024 * 1024)

    q.put_nowait({"path": "/fake/a.jpg", "size": 1024})
    q.put_nowait({"path": "/fake/b.mp4", "size": 20 * 1024 * 1024})

    assert q.qsize() == 2
    item1 = q.get_nowait()
    assert item1["path"] == "/fake/a.jpg"
    q.task_done()

    item2 = q.get_nowait()
    assert item2["path"] == "/fake/b.mp4"
    q.task_done()

    assert q.empty() is True
