import asyncio
import os
from asyncio import IncompleteReadError, Queue, QueueEmpty
from contextlib import contextmanager
from locale import LC_ALL
from locale import setlocale as _setlocale
from threading import Lock

DEFAULT_SMALL_FILE_MAX_BYTES = int(os.environ.get("SMALL_FILE_MAX_MB", "50")) * 1024 * 1024


class DualLaneUploadQueue:
    """Fila de upload com faixas dedicadas para arquivos pequenos (fast lane) e grandes.

    Totalmente compatível com a interface de asyncio.Queue.
    """

    def __init__(self, small_file_max_bytes: int | None = None):
        self._small_queue: Queue = Queue()
        self._large_queue: Queue = Queue()
        self._unfinished_tasks = 0
        self._finished = asyncio.Event()
        self._finished.set()
        self._has_item = asyncio.Event()
        self._small_file_max_bytes = (
            small_file_max_bytes
            if small_file_max_bytes is not None
            else DEFAULT_SMALL_FILE_MAX_BYTES
        )

    @property
    def small_file_max_bytes(self) -> int:
        return self._small_file_max_bytes

    @small_file_max_bytes.setter
    def small_file_max_bytes(self, val: int) -> None:
        self._small_file_max_bytes = val

    def is_small(self, item: dict) -> bool:
        if not isinstance(item, dict):
            return False
        size = item.get("size")
        if size is None and "path" in item:
            try:
                if os.path.exists(item["path"]):
                    size = os.path.getsize(item["path"])
            except OSError:
                size = 0
        return (size or 0) <= self._small_file_max_bytes

    async def put(self, item: dict) -> None:
        self._unfinished_tasks += 1
        self._finished.clear()
        if self.is_small(item):
            await self._small_queue.put(item)
        else:
            await self._large_queue.put(item)
        self._has_item.set()

    def put_nowait(self, item: dict) -> None:
        self._unfinished_tasks += 1
        self._finished.clear()
        if self.is_small(item):
            self._small_queue.put_nowait(item)
        else:
            self._large_queue.put_nowait(item)
        self._has_item.set()

    async def get_small(self) -> dict:
        """Retira item da faixa rápida (arquivos pequenos)."""
        item = await self._small_queue.get()
        if self.empty():
            self._has_item.clear()
        return item

    async def get_large(self, fallback_to_small: bool = True) -> dict:
        """Retira item da faixa de arquivos grandes (ou recorre aos pequenos se vazio)."""
        while True:
            if not self._large_queue.empty():
                try:
                    item = self._large_queue.get_nowait()
                    if self.empty():
                        self._has_item.clear()
                    return item
                except QueueEmpty:
                    pass
            if fallback_to_small and not self._small_queue.empty():
                try:
                    item = self._small_queue.get_nowait()
                    if self.empty():
                        self._has_item.clear()
                    return item
                except QueueEmpty:
                    pass
            self._has_item.clear()
            await self._has_item.wait()

    async def get(self) -> dict:
        """Retira item priorizando arquivos pequenos para esvaziar a fila rapidamente."""
        while True:
            if not self._small_queue.empty():
                try:
                    item = self._small_queue.get_nowait()
                    if self.empty():
                        self._has_item.clear()
                    return item
                except QueueEmpty:
                    pass
            if not self._large_queue.empty():
                try:
                    item = self._large_queue.get_nowait()
                    if self.empty():
                        self._has_item.clear()
                    return item
                except QueueEmpty:
                    pass
            self._has_item.clear()
            await self._has_item.wait()

    def get_nowait(self) -> dict:
        if not self._small_queue.empty():
            item = self._small_queue.get_nowait()
            if self.empty():
                self._has_item.clear()
            return item
        if not self._large_queue.empty():
            item = self._large_queue.get_nowait()
            if self.empty():
                self._has_item.clear()
            return item
        raise QueueEmpty()

    def task_done(self) -> None:
        if self._unfinished_tasks <= 0:
            raise ValueError("task_done() called too many times")
        self._unfinished_tasks -= 1
        if self._unfinished_tasks == 0:
            self._finished.set()

    def empty(self) -> bool:
        return self._small_queue.empty() and self._large_queue.empty()

    def qsize(self) -> int:
        return self._small_queue.qsize() + self._large_queue.qsize()

    def small_qsize(self) -> int:
        return self._small_queue.qsize()

    def large_qsize(self) -> int:
        return self._large_queue.qsize()

    async def join(self) -> None:
        if self._unfinished_tasks > 0:
            await self._finished.wait()


# Fila Global de Upload com faixas dedicadas
UPLOAD_QUEUE = DualLaneUploadQueue()

__all__ = (
    "StreamIO",
    "wrap_with_container",
    "AbstractAsyncLister",
    "setlocale",
    "DualLaneUploadQueue",
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
        else:
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
        pass

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
