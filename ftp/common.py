from asyncio import IncompleteReadError, Queue
from contextlib import contextmanager
from locale import LC_ALL
from locale import setlocale as _setlocale
from threading import Lock


class UploadQueue:
    """FIFO upload queue, compatible with ``asyncio.Queue``.

    MongoDB remains the persistent source of truth. This queue only
    coordinates items already atomically claimed for the current process.
    """

    def __init__(self):
        self._queue: Queue = Queue()

    async def put(self, item: dict) -> None:
        await self._queue.put(item)

    def put_nowait(self, item: dict) -> None:
        self._queue.put_nowait(item)

    async def get(self) -> dict:
        return await self._queue.get()

    def get_nowait(self) -> dict:
        return self._queue.get_nowait()

    def task_done(self) -> None:
        self._queue.task_done()

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()

    async def join(self) -> None:
        await self._queue.join()


UPLOAD_QUEUE = UploadQueue()

__all__ = (
    "StreamIO",
    "wrap_with_container",
    "AbstractAsyncLister",
    "setlocale",
    "UploadQueue",
    "UPLOAD_QUEUE",
)


class AsyncStreamIterator:
    def __init__(self, read_coro):
        self.read_coro = read_coro

    def __aiter__(self):
        return self

    async def __anext__(self):
        data = await self.read_coro()
        if data:
            return data
        raise StopAsyncIteration


class AbstractAsyncLister:
    async def _to_list(self):
        items = []
        async for item in self:
            items.append(item)
        return items

    def __aiter__(self):
        return self

    def __await__(self):
        return self._to_list().__await__()


def wrap_with_container(o):
    if isinstance(o, str):
        o = (o,)
    return o


class StreamIO:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    async def readline(self):
        return await self.reader.readline()

    async def read(self, count=-1):
        return await self.reader.read(count)

    async def readexactly(self, count):
        try:
            return await self.reader.readexactly(count)
        except IncompleteReadError as e:
            return e.partial

    async def write(self, data):
        self.writer.write(data)
        await self.writer.drain()

    def close(self):
        self.writer.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args, **kwargs):
        self.close()

    def iter_by_block(self, count=8192):
        return AsyncStreamIterator(lambda: self.readexactly(count))


LOCALE_LOCK = Lock()


@contextmanager
def setlocale(name):
    with LOCALE_LOCK:
        old_locale = _setlocale(LC_ALL)
        try:
            yield _setlocale(LC_ALL, name)
        finally:
            _setlocale(LC_ALL, old_locale)
