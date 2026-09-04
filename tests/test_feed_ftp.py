from __future__ import annotations

import io
import os
import queue
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import feed_ftp


class _FakeResponse(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self._headers = {"Content-Length": str(len(value))}

    @property
    def headers(self):
        return self._headers

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


def test_register_one_clears_stale_delete_source(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"media")
    updates = []

    class Files:
        def find_one(self, *_args, **_kwargs):
            return {"_id": "file-1", "status": "staging"}

        def update_one(self, *args, **kwargs):
            updates.append((args, kwargs))

    class Client:
        ftp = SimpleNamespace(files=Files())

        def close(self):
            pass

    monkeypatch.setattr(feed_ftp, "MongoClient", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(feed_ftp, "ensure_nebula_metadata", lambda *_args: None)

    assert feed_ftp.register_one(
        source,
        tmp_path / "dest" / source.name,
        tmp_path / "dest",
        "mongodb://test",
        overwrite=False,
        delete_source=False,
    ) == 0
    assert updates[0][0][1]["$set"]["delete_source"] is False


def test_cleanup_removes_only_stale_incomplete_groups(tmp_path):
    stale = tmp_path / ".old.download"
    stale_part = tmp_path / ".old.download.part0"
    active = tmp_path / ".active.download"
    active_part = tmp_path / ".active.download.part0"
    for path in (stale, stale_part, active, active_part):
        path.write_bytes(b"x")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    os.utime(stale_part, (old, old))

    removed, released = feed_ftp.cleanup_stale_downloads([tmp_path], 1800)

    assert removed == 2
    assert released == 2
    assert not stale.exists()
    assert not stale_part.exists()
    assert active.exists()
    assert active_part.exists()


def test_materialize_strm_downloads_and_preserves_placeholder(tmp_path, monkeypatch):
    src = tmp_path / "movie.strm"
    src.write_text("https://example.com/media/movie.mp4\n", encoding="utf-8")

    def fake_urlopen(request, timeout=0):
        assert request.full_url == "https://example.com/media/movie.mp4"
        assert request.get_header("Range") == "bytes=0-0"
        return _FakeResponse(b"media-bytes")

    monkeypatch.setattr(feed_ftp, "urlopen", fake_urlopen)

    target = feed_ftp.materialize_strm(src)

    assert target == tmp_path / "movie.mp4"
    assert target.read_bytes() == b"media-bytes"
    assert src.exists()


def test_materialize_strm_creates_temporary_file_in_target_stage(tmp_path, monkeypatch):
    source = tmp_path / "source"
    stage = tmp_path / "other-stage"
    source.mkdir()
    src = source / "movie.strm"
    target = stage / "movie.mp4"
    src.write_text("https://example.com/movie.mp4\n", encoding="utf-8")
    created_in_stage = []

    real_open = Path.open

    def track_open(self, *args, **kwargs):
        if str(stage) in str(self):
            created_in_stage.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", track_open)
    monkeypatch.setattr(feed_ftp, "urlopen", lambda *args, **kwargs: _FakeResponse(b"media"))

    feed_ftp.materialize_strm(src, target=target)

    assert any(".movie.download" in str(p) for p in created_in_stage)
    assert target.read_bytes() == b"media"


def test_materialize_strm_resumes_completed_parts(tmp_path, monkeypatch):
    src = tmp_path / "movie.strm"
    src.write_text("https://example.com/movie.mp4\n", encoding="utf-8")
    media = bytes(range(100))  # 100 bytes, 4 parts -> 25 bytes each

    # Pre-cria as partes 0, 1 e 2 já completas no disco
    target = tmp_path / "movie.mp4"
    tmp_base = tmp_path / ".movie.download"
    part0 = Path(f"{tmp_base}.part0")
    part1 = Path(f"{tmp_base}.part1")
    part2 = Path(f"{tmp_base}.part2")
    part0.write_bytes(media[0:25])
    part1.write_bytes(media[25:50])
    part2.write_bytes(media[50:75])

    downloaded_ranges = []

    def fake_urlopen(request, timeout=0):
        value = request.get_header("Range")
        if value == "bytes=0-0":
            response = _FakeResponse(media[:1])
            response.headers["Content-Range"] = f"bytes 0-0/{len(media)}"
            return response
        start, end = map(int, value.removeprefix("bytes=").split("-"))
        downloaded_ranges.append((start, end))
        response = _FakeResponse(media[start:end + 1])
        response.headers["Content-Range"] = f"bytes {start}-{end}/{len(media)}"
        return response

    monkeypatch.setenv("STRM_DOWNLOAD_PARTS", "4")
    monkeypatch.setattr(feed_ftp, "urlopen", fake_urlopen)

    res = feed_ftp.materialize_strm(src, target=target)

    # Apenas a parte 3 (bytes 75-99) deve ter sido baixada via HTTP!
    assert downloaded_ranges == [(75, 99)]
    assert res.read_bytes() == media


def test_materialize_strm_downloads_parallel_ranges(tmp_path, monkeypatch):
    src = tmp_path / "movie.strm"
    src.write_text("https://example.com/movie.mp4\n", encoding="utf-8")
    media = bytes(range(100))

    def fake_urlopen(request, timeout=0):
        value = request.get_header("Range")
        start, end = map(int, value.removeprefix("bytes=").split("-"))
        response = _FakeResponse(media[start:end + 1])
        response.headers["Content-Range"] = f"bytes {start}-{end}/{len(media)}"
        return response

    monkeypatch.setenv("STRM_DOWNLOAD_PARTS", "4")
    monkeypatch.setattr(feed_ftp, "urlopen", fake_urlopen)

    target = feed_ftp.materialize_strm(src)

    assert target.read_bytes() == media


def test_materialize_strm_retries_failed_part(tmp_path, monkeypatch):
    src = tmp_path / "movie.strm"
    src.write_text("https://example.com/movie.mp4\n", encoding="utf-8")
    media = b"retry-me"
    part_attempts = 0

    def fake_urlopen(request, timeout=0):
        nonlocal part_attempts
        value = request.get_header("Range")
        if value == "bytes=0-0":
            response = _FakeResponse(media[:1])
            response.headers["Content-Range"] = f"bytes 0-0/{len(media)}"
            return response
        part_attempts += 1
        if part_attempts == 1:
            raise TimeoutError("stalled")
        response = _FakeResponse(media)
        response.headers["Content-Range"] = f"bytes 0-{len(media) - 1}/{len(media)}"
        return response

    monkeypatch.setenv("STRM_DOWNLOAD_PARTS", "1")
    monkeypatch.setattr(feed_ftp.time, "sleep", lambda _: None)
    monkeypatch.setattr(feed_ftp, "urlopen", fake_urlopen)

    target = feed_ftp.materialize_strm(src)

    assert target.read_bytes() == media
    assert part_attempts == 2


def test_strm_worker_materializes_one_and_queues_upload(tmp_path, monkeypatch):
    first = tmp_path / "first.strm"
    first.write_text("https://example.com/first.mp4\n", encoding="utf-8")

    uploaded: list[tuple[Path, Path, Path]] = []

    def fake_materialize(src, overwrite=False):
        assert src == first
        return tmp_path / "first.mp4"

    def fake_enqueue_upload_job(jobs, pending, lock, source_root, dest, src, src_key=None):
        uploaded.append((source_root, dest, src))
        return True

    monkeypatch.setattr(feed_ftp, "materialize_strm", fake_materialize)
    monkeypatch.setattr(feed_ftp, "enqueue_upload_job", fake_enqueue_upload_job)

    strm_jobs: queue.Queue = queue.Queue()
    upload_jobs: queue.Queue = queue.Queue()
    pending: set[str] = set()
    seen: set[str] = set()

    strm_jobs.put((tmp_path, first))
    strm_jobs.put(None)

    feed_ftp.strm_worker(1, strm_jobs, upload_jobs, feed_ftp.Stats(), threading.Lock(), tmp_path, False, pending, {}, tmp_path / "links.json", set(), seen, None)

    assert uploaded == [(tmp_path, tmp_path, tmp_path / "first.mp4")]
    assert str(first) in seen
    assert str(first) not in pending


def test_strm_worker_reuses_cached_link_without_redownload(tmp_path, monkeypatch):
    cached = tmp_path / "cached.mp4"
    cached.write_bytes(b"cached-bytes")
    src = tmp_path / "duplicate.strm"
    src.write_text("https://example.com/shared.mp4\n", encoding="utf-8")

    uploaded: list[tuple[Path, Path, Path]] = []

    def fail_materialize(*args, **kwargs):
        raise AssertionError("network materialization should not run for cached link")

    def fake_enqueue_upload_job(jobs, pending, lock, source_root, dest, src_path, src_key=None):
        uploaded.append((source_root, dest, src_path))
        return True

    monkeypatch.setattr(feed_ftp, "materialize_strm", fail_materialize)
    monkeypatch.setattr(feed_ftp, "enqueue_upload_job", fake_enqueue_upload_job)

    strm_jobs: queue.Queue = queue.Queue()
    upload_jobs: queue.Queue = queue.Queue()
    pending: set[str] = set()
    seen: set[str] = set()
    links = {"https://example.com/shared.mp4": str(cached)}
    links_file = tmp_path / "links.json"

    strm_jobs.put((tmp_path, src))
    strm_jobs.put(None)

    feed_ftp.strm_worker(1, strm_jobs, upload_jobs, feed_ftp.Stats(), threading.Lock(), tmp_path, False, pending, links, links_file, set(), seen, None)

    assert uploaded == [(tmp_path, tmp_path, tmp_path / "duplicate.mp4")]
    assert (tmp_path / "duplicate.mp4").read_bytes() == b"cached-bytes"
    assert str(src) in seen


def test_strm_worker_skips_failed_file_for_current_run(tmp_path, monkeypatch):
    src = tmp_path / "blocked.strm"
    src.write_text("https://example.com/blocked.mp4\n", encoding="utf-8")
    failed: set[str] = set()

    monkeypatch.setattr(
        feed_ftp,
        "materialize_strm",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("403")),
    )

    strm_jobs: queue.Queue = queue.Queue()
    strm_jobs.put((tmp_path, src))
    strm_jobs.put(None)

    feed_ftp.strm_worker(
        1, strm_jobs, queue.Queue(), feed_ftp.Stats(), threading.Lock(),
        tmp_path, False, set(), {}, tmp_path / "links.json", failed,
    )

    assert str(src) in failed


def test_strm_worker_direct_mongo_uses_staging_and_virtual_destination(tmp_path, monkeypatch):
    source = tmp_path / "source"
    src = source / "Filmes" / "Movie (2026)" / "Movie (2026).strm"
    src.parent.mkdir(parents=True)
    src.write_text("https://example.com/movie.mp4\n", encoding="utf-8")
    captured = {}

    def fake_materialize(src_path, overwrite=False, known_links=None, target=None):
        captured["target"] = target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"media")
        src_path.unlink()
        return target, "https://example.com/movie.mp4", False

    def fake_enqueue(jobs, pending, lock, source_root, dest, src_path, destination=None, src_key=None):
        captured["source"] = src_path
        captured["destination"] = destination
        src_path.unlink()
        return True

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STAGING_DIRS", raising=False)
    monkeypatch.delenv("STAGING_DIR", raising=False)
    monkeypatch.setattr(feed_ftp, "remote_content_size", lambda url: 5)
    monkeypatch.setattr(feed_ftp, "wait_for_disk_capacity", lambda *args: None)
    monkeypatch.setattr(feed_ftp, "materialize_or_reuse_strm", fake_materialize)
    monkeypatch.setattr(feed_ftp, "enqueue_upload_job", fake_enqueue)

    jobs = queue.Queue()
    jobs.put((source, src))
    jobs.put(None)
    feed_ftp.strm_worker(
        1, jobs, queue.Queue(), feed_ftp.Stats(), threading.Lock(),
        tmp_path / "virtual", False, set(), {}, tmp_path / "links.json",
        set(), direct_mongo=True,
    )

    assert captured["target"].parent == (tmp_path / "staging" / "strm").resolve()
    assert captured["source"] == captured["target"]
    assert captured["destination"] == tmp_path / "virtual" / "Filmes" / "Movie (2026)" / "Movie (2026).mp4"


def test_strm_worker_skips_strm_when_destination_was_already_sent_without_cached_url(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    src = source / "Filmes" / "Movie (2026)" / "Movie (2026).strm"
    src.parent.mkdir(parents=True)
    url = "https://example.com/movie.mp4"
    src.write_text(url, encoding="utf-8")
    seen = set()

    monkeypatch.setattr(feed_ftp, "is_completed_destination", lambda *args: True)
    monkeypatch.setattr(
        feed_ftp,
        "materialize_or_reuse_strm",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("completed URL must not be downloaded")
        ),
    )

    jobs = queue.Queue()
    uploads = queue.Queue()
    jobs.put((source, src))
    jobs.put(None)
    feed_ftp.strm_worker(
        1, jobs, uploads, feed_ftp.Stats(), threading.Lock(),
        tmp_path / "virtual", False, set(), {},
        tmp_path / "links.json", set(), seen, None, True, "mongodb://test",
    )

    assert src.exists()
    assert uploads.empty()
    assert str(src) in seen


def test_prune_completed_strm_removes_complete_movie_folder(tmp_path, monkeypatch):
    source = tmp_path / "source"
    movie = source / "Filmes" / "Movie Name (2026)"
    movie.mkdir(parents=True)
    (movie / "Movie Name (2026).strm").write_text("https://example.com/movie", encoding="utf-8")
    (movie / "poster.jpg").write_bytes(b"poster")
    monkeypatch.setattr(
        feed_ftp,
        "completed_media_identities",
        lambda _uri: ({("movie name", "2026")}, set()),
    )

    stats = feed_ftp.prune_completed_strm([source], "mongodb://test", apply=True)

    assert stats == {"scanned": 1, "matched": 1, "folders": 1, "files": 0}
    assert not movie.exists()


def test_prune_completed_strm_keeps_partial_season(tmp_path, monkeypatch):
    source = tmp_path / "source"
    season = source / "Series" / "Show Name" / "Season 01"
    season.mkdir(parents=True)
    completed = season / "Show Name - S01E01.strm"
    pending = season / "Show Name - S01E02.strm"
    completed.write_text("https://example.com/one", encoding="utf-8")
    pending.write_text("https://example.com/two", encoding="utf-8")
    monkeypatch.setattr(
        feed_ftp,
        "completed_media_identities",
        lambda _uri: (set(), {("show name", 1, 1)}),
    )

    stats = feed_ftp.prune_completed_strm([source], "mongodb://test", apply=True)

    assert stats == {"scanned": 2, "matched": 1, "folders": 0, "files": 1}
    assert season.exists()
    assert not completed.exists()
    assert pending.exists()


def test_episode_identity_accepts_dotted_separator():
    assert feed_ftp.episode_identity("Show Name", "Show Name - S01.E02.mkv") == (
        "show name",
        1,
        2,
    )


def test_prune_completed_strm_dry_run_changes_nothing(tmp_path, monkeypatch):
    source = tmp_path / "source"
    movie = source / "Filmes" / "Movie Name (2026)"
    movie.mkdir(parents=True)
    placeholder = movie / "Movie Name (2026).strm"
    placeholder.write_text("https://example.com/movie", encoding="utf-8")
    monkeypatch.setattr(
        feed_ftp,
        "completed_media_identities",
        lambda _uri: ({("movie name", "2026")}, set()),
    )

    stats = feed_ftp.prune_completed_strm([source], "mongodb://test")

    assert stats["folders"] == 1
    assert placeholder.exists()


def test_disk_capacity_waits_until_download_preserves_ten_percent(tmp_path, monkeypatch):
    usages = iter([
        SimpleNamespace(total=1000, free=250),
        SimpleNamespace(total=1000, free=400),
    ])
    sleeps = []
    monkeypatch.setattr(feed_ftp.shutil, "disk_usage", lambda path: next(usages))
    monkeypatch.setattr(feed_ftp.time, "sleep", sleeps.append)

    feed_ftp.wait_for_disk_capacity(tmp_path / "staging" / "movie.mp4", 200)

    assert sleeps == [10]


def test_is_completed_destination_checks_url_id(tmp_path, monkeypatch):
    dest_root = tmp_path / "virtual"
    destination = dest_root / "Filmes" / "Movie (2026)" / "Movie (2026).mkv"
    url = "http://127.0.0.1:2122/stream?id=60f123456789abcdef012345"

    class FakeFiles:
        def find_one(self, query, proj=None):
            if "status" in query and query["status"] == "completed":
                return {"_id": "60f123456789abcdef012345"}
            return None

    class FakeClient:
        ftp = SimpleNamespace(files=FakeFiles())
        def close(self): pass

    monkeypatch.setattr(feed_ftp, "MongoClient", lambda *a, **k: FakeClient())
    assert feed_ftp.is_completed_destination("mongodb://test", dest_root, destination, url)


def test_is_completed_destination_checks_stem_match(tmp_path, monkeypatch):
    dest_root = tmp_path / "virtual"
    destination = dest_root / "Filmes" / "Movie (2026)" / "Movie (2026).mkv"

    class FakeFiles:
        def find(self, query, proj=None):
            return [{"name": "Movie (2026).mp4", "status": "completed"}]

    class FakeClient:
        ftp = SimpleNamespace(files=FakeFiles())
        def close(self): pass

    monkeypatch.setattr(feed_ftp, "MongoClient", lambda *a, **k: FakeClient())
    assert feed_ftp.is_completed_destination("mongodb://test", dest_root, destination)


def test_extract_media_year():
    assert feed_ftp.extract_media_year("Movie (2026).strm") == 2026
    assert feed_ftp.extract_media_year("Movie.2025.1080p.mkv") == 2025
    assert feed_ftp.extract_media_year("Blade Runner 2049 (2017).strm") == 2017
    assert feed_ftp.extract_media_year("2012 (2009).mp4") == 2009
    assert feed_ftp.extract_media_year("Movie - 1999.avi") == 1999
    assert feed_ftp.extract_media_year("movie.strm", "The Matrix (1999)") == 1999
    assert feed_ftp.extract_media_year("Movie Without Year.strm") == 0


def test_iter_files_by_priority_orders_movies_by_year_descending(tmp_path):
    filmes_dir = tmp_path / "Filmes"
    filmes_dir.mkdir(parents=True)
    
    (filmes_dir / "Filme 2024.strm").write_text("http://example.com/2024", encoding="utf-8")
    (filmes_dir / "Filme (2026).strm").write_text("http://example.com/2026", encoding="utf-8")
    (filmes_dir / "Filme.2025.mkv").write_text("media", encoding="utf-8")
    (filmes_dir / "Filme Antigo (1994).mp4").write_text("media", encoding="utf-8")
    (filmes_dir / "Filme Sem Ano.strm").write_text("http://example.com/noyear", encoding="utf-8")
    
    results = list(feed_ftp.iter_files_by_priority([tmp_path], all_files=False, exclude_dirs=set()))
    names = [src.name for _, src in results]
    
    assert names == [
        "Filme (2026).strm",
        "Filme.2025.mkv",
        "Filme 2024.strm",
        "Filme Antigo (1994).mp4",
        "Filme Sem Ano.strm",
    ]


def test_materialize_strm_cleans_parts_on_failure_in_target_dir(tmp_path, monkeypatch):
    source = tmp_path / "source"
    stage = tmp_path / "stage"
    source.mkdir()
    stage.mkdir()
    src = source / "broken_movie.strm"
    src.write_text("https://example.com/broken_movie.mp4\n", encoding="utf-8")
    target = stage / "broken_movie.mp4"

    def fake_urlopen(request, timeout=0):
        if request.get_header("Range") == "bytes=0-0":
            res = _FakeResponse(b"x")
            res.headers["Content-Range"] = "bytes 0-0/1000"
            return res
        raise IOError("Connection aborted during part download")

    monkeypatch.setenv("STRM_DOWNLOAD_PARTS", "2")
    monkeypatch.setenv("STRM_PART_RETRIES", "1")
    monkeypatch.setattr(feed_ftp.time, "sleep", lambda _: None)
    monkeypatch.setattr(feed_ftp, "urlopen", fake_urlopen)

    import pytest
    with pytest.raises(IOError):
        feed_ftp.materialize_strm(src, target=target)

    # Garante que nenhum arquivo temporário .download ou .part ficou no stage
    remaining = list(stage.glob("*.download*")) + list(stage.glob(".*.download*"))
    assert remaining == []


def test_cleanup_stale_downloads_removes_immediate_if_media_exists(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "Movie (2026).mkv").write_bytes(b"final media content")
    
    # Arquivo temporário recente de uma tentativa anterior
    stale_part = stage / ".Movie (2026).abc12345.download.part1"
    stale_part.write_bytes(b"garbage part")
    
    # Como o arquivo final Movie (2026).mkv existe, deve remover mesmo que seja recente (sem esperar 1800s)
    removed, released = feed_ftp.cleanup_stale_downloads([stage], 1800)
    assert removed == 1
    assert released == len(b"garbage part")
    assert not stale_part.exists()
    assert (stage / "Movie (2026).mkv").exists()


def test_get_best_staging_root_prioritizes_fastest_disk_with_available_space(tmp_path, monkeypatch):
    disk_e = tmp_path / "E_NebulaStage"
    disk_f = tmp_path / "F_NebulaStage"
    disk_e.mkdir()
    disk_f.mkdir()

    # Ordem de velocidade: E primeiro, F segundo
    monkeypatch.setenv("STAGING_DIRS", f"{disk_e};{disk_f}")

    def fake_disk_usage(path):
        p_str = str(path)
        if "E_NebulaStage" in p_str:
            return SimpleNamespace(total=100 * 1024**3, free=60 * 1024**3)
        if "F_NebulaStage" in p_str:
            return SimpleNamespace(total=2000 * 1024**3, free=800 * 1024**3)
        return SimpleNamespace(total=1000, free=100)

    monkeypatch.setattr(feed_ftp.shutil, "disk_usage", fake_disk_usage)

    best_root = feed_ftp.get_best_staging_root()
    # Como Disk E é o mais rápido e possui 60 GB livres (> reserva), deve ser o escolhido
    assert best_root == disk_e.resolve()


def test_get_best_staging_root_falls_back_when_fastest_disk_full(tmp_path, monkeypatch):
    disk_e = tmp_path / "E_NebulaStage"
    disk_f = tmp_path / "F_NebulaStage"
    disk_e.mkdir()
    disk_f.mkdir()

    monkeypatch.setenv("STAGING_DIRS", f"{disk_e};{disk_f}")

    def fake_disk_usage(path):
        p_str = str(path)
        if "E_NebulaStage" in p_str:
            # Disk E com apenas 1 GB livre (cheio)
            return SimpleNamespace(total=100 * 1024**3, free=1 * 1024**3)
        if "F_NebulaStage" in p_str:
            return SimpleNamespace(total=2000 * 1024**3, free=800 * 1024**3)
        return SimpleNamespace(total=1000, free=100)

    monkeypatch.setattr(feed_ftp.shutil, "disk_usage", fake_disk_usage)

    best_root = feed_ftp.get_best_staging_root()
    # Como Disk E está cheio, faz fallback para Disk F
    assert best_root == disk_f.resolve()




