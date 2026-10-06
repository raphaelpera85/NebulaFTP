from __future__ import annotations

import asyncio
import re
from pathlib import PurePosixPath

from ftp.pathio import MongoDBPathIO


class _Cursor:
    def __init__(self, documents):
        self._documents = iter(documents)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._documents)
        except StopIteration:
            raise StopAsyncIteration

    def limit(self, _count):
        return self

    async def to_list(self, length=None):
        documents = list(self._documents)
        return documents if length is None else documents[:length]


class _Files:
    def __init__(self, documents):
        self.documents = documents

    async def find_one(self, query):
        return next(
            (
                doc for doc in self.documents
                if all(doc.get(key) == value for key, value in query.items())
            ),
            None,
        )

    def find(self, query, _projection=None):
        if "parent" in query and isinstance(query["parent"], str):
            matches = [doc for doc in self.documents if doc.get("parent") == query["parent"]]
        else:
            parent_filters = query["$or"]
            matches = []
            for doc in self.documents:
                parent = doc.get("parent", "")
                if doc.get("parts") and any(
                    parent == item["parent"] if isinstance(item["parent"], str)
                    else re.search(item["parent"]["$regex"], parent) is not None
                    for item in parent_filters
                ):
                    matches.append(doc)
        return _Cursor(matches)


class _Database:
    def __init__(self, documents):
        self.files = _Files(documents)


def test_listing_matches_published_library_layout(monkeypatch):
    monkeypatch.setenv("NEBULA_LIBRARY_USER", "raphael")
    documents = [
        {"name": "Filmes", "parent": "/raphael", "type": "dir"},
        {"name": "Series", "parent": "/raphael", "type": "dir"},
        {"name": "Novelas", "parent": "/raphael", "type": "dir"},
        {"name": "strm", "parent": "/raphael", "type": "dir"},
        {"name": "raphael", "parent": "/raphael", "type": "dir"},
        {"name": "series", "parent": "/raphael", "type": "file", "parts": []},
        {"name": "Movie.mkv", "parent": "/raphael/Filmes", "type": "file", "parts": [{"tg_file_id": "published"}]},
        {"name": "Show.mkv", "parent": "/raphael/Series", "type": "file", "parts": [{"tg_file_id": "published"}]},
        {"name": "nested.mkv", "parent": "/raphael/Series/Filmes", "type": "file", "parts": [{"tg_file_id": "published"}]},
        {"name": "empty.mkv", "parent": "/raphael/Novelas", "type": "file", "parts": []},
        {"name": "Filmes", "parent": "/raphael/Series", "type": "dir"},
        {"name": "stale.tmp", "parent": "/raphael", "type": "file", "parts": []},
    ]
    path_io = MongoDBPathIO()
    path_io.db = _Database(documents)

    async def list_paths():
        return [str(path) async for path in path_io.list(PurePosixPath("/raphael"))]

    assert asyncio.run(list_paths()) == ["/raphael/Filmes", "/raphael/Series"]


def test_listing_keeps_only_complete_multi_part_payloads():
    assert MongoDBPathIO._has_telegram_payload({
        "parts": [{"tg_file_id": "one"}, {"tg_file": "two"}],
    })
    assert not MongoDBPathIO._has_telegram_payload({
        "parts": [{"tg_file_id": "one"}, {}],
    })
    assert not MongoDBPathIO._has_telegram_payload({"parts": []})
