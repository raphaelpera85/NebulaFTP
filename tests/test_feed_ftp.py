from __future__ import annotations

import io
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import feed_ftp


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_split_source_paths_supports_multiple_values(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    values = feed_ftp.split_source_paths([f"{first}; {second}", str(first)])

    assert values == [first.resolve(), second.resolve()]


def test_materialize_strm_downloads_and_deletes_placeholder(tmp_path, monkeypatch):
    src = tmp_path / "movie.strm"
    src.write_text("https://example.com/media/movie.mp4\n", encoding="utf-8")

    def fake_urlopen(url, timeout=0):
        assert url == "https://example.com/media/movie.mp4"
        return _FakeResponse(b"media-bytes")

    monkeypatch.setattr(feed_ftp, "urlopen", fake_urlopen)

    target = feed_ftp.materialize_strm(src)

    assert target == tmp_path / "movie.mp4"
    assert target.read_bytes() == b"media-bytes"
    assert not src.exists()
