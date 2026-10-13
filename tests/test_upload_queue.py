import importlib.util
from pathlib import Path

import pytest

_COMMON_PATH = Path(__file__).resolve().parents[1] / "ftp" / "common.py"
_SPEC = importlib.util.spec_from_file_location("nebula_upload_queue", _COMMON_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)
UploadQueue = _MODULE.UploadQueue


@pytest.mark.asyncio
async def test_upload_queue_is_single_fifo_for_every_file_size():
    queue = UploadQueue()
    large = {"path": "/fake/movie.mkv", "size": 500 * 1024 * 1024}
    small = {"path": "/fake/cover.jpg", "size": 100 * 1024}

    await queue.put(large)
    await queue.put(small)

    assert queue.qsize() == 2
    assert (await queue.get())["path"] == large["path"]
    queue.task_done()
    assert (await queue.get())["path"] == small["path"]
    queue.task_done()
    await queue.join()
    assert queue.empty()


@pytest.mark.asyncio
async def test_upload_queue_keeps_asyncio_task_accounting():
    queue = UploadQueue()
    queue.put_nowait({"path": "/fake/a.mkv"})
    assert (await queue.get())["path"] == "/fake/a.mkv"
    queue.task_done()
    await queue.join()
