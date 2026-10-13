import stat
from pathlib import Path
from types import SimpleNamespace

import generate_strm
import pytest


def patch_mongo_documents(monkeypatch, documents):
    class FakeCollection:
        def find(self, *_args, **_kwargs):
            return documents

    class FakeClient:
        def __init__(self, _url):
            self.database = SimpleNamespace(files=FakeCollection())

        def __getitem__(self, _name):
            return self.database

        def close(self):
            pass

    monkeypatch.setattr(generate_strm, "MongoClient", FakeClient)


def test_stream_endpoint_always_points_native_players_at_raw_stream():
    # .strm files target native media-center players (VLC, Kodi, Jellyfin,
    # Plex, Emby) that decode any container/codec themselves. /stream serves
    # raw bytes with Accept-Ranges/Content-Length so the player can seek;
    # /transcode is chunked with no Range support and is only meant for the
    # HTML5 <video> fallback in /play, never for generated .strm files.
    assert generate_strm.stream_endpoint("movie.mp4") == "stream"
    assert generate_strm.stream_endpoint("episode.avi") == "stream"
    assert generate_strm.stream_endpoint("movie.mkv") == "stream"


def test_safe_windows_name_replaces_invalid_path_characters():
    assert generate_strm.safe_windows_name("A Mulher Invis?vel: 2009") == "A Mulher Invis - vel - 2009"
    assert generate_strm.safe_windows_name("title. ") == "title"


def test_remove_stale_strm_files_keeps_current_files(tmp_path):
    current = tmp_path / "Filmes" / "Clean (2024)" / "Clean (2024).strm"
    stale = tmp_path / "Filmes" / "Movie.2024.1080p" / "Movie.2024.1080p.strm"
    current.parent.mkdir(parents=True)
    stale.parent.mkdir(parents=True)
    current.write_text("current", encoding="utf-8")
    stale.write_text("stale", encoding="utf-8")
    stale.parent.chmod(stat.S_IREAD)

    assert generate_strm.remove_stale_strm_files(tmp_path, {current}) == 1
    assert current.exists()
    assert not stale.parent.exists()


def test_transcode_treats_telegram_http_input_as_non_seekable():
    source = Path(generate_strm.__file__).with_name("main.py").read_text(encoding="utf-8")

    assert '"-seekable", "0", "-i", input_url' in source


def test_generate_strm_creates_only_missing_media_and_preserves_valid_links(monkeypatch, tmp_path):
    documents = [
        {"_id": "media-one", "name": "One.mkv", "parent": "/raphael/Filmes/One"},
        {"_id": "media-two", "name": "Two.mkv", "parent": "/raphael/Series/Two/Season 01"},
    ]
    patch_mongo_documents(monkeypatch, documents)
    output = tmp_path / "library"
    existing = output / "Filmes" / "One" / "One.strm"
    existing.parent.mkdir(parents=True)
    original_url = "https://old-host/stream?id=media-one"
    existing.write_text(original_url, encoding="utf-8")
    original_mtime = existing.stat().st_mtime_ns
    unrelated_invalid = output / "legacy" / "broken.strm"
    unrelated_invalid.parent.mkdir()
    unrelated_invalid.write_text("not-a-link", encoding="utf-8")

    result = generate_strm.generate_strm_files(
        output_root=output,
        stream_base_url="http://nebula.local",
        library_user="raphael",
    )

    created = output / "Series" / "Two" / "Season 01" / "Two.strm"
    assert result["generated"] == 1
    assert result["updated"] == 0
    assert result["skipped"] == 1
    assert result["validated"] == 2
    assert result["invalid_links"] == 1
    assert existing.read_text(encoding="utf-8") == original_url
    assert existing.stat().st_mtime_ns == original_mtime
    assert created.read_text(encoding="utf-8") == "http://nebula.local/stream?id=media-two"

    second_run = generate_strm.generate_strm_files(
        output_root=output,
        stream_base_url="http://nebula.local",
        library_user="raphael",
    )
    assert second_run["generated"] == 0
    assert second_run["updated"] == 0
    assert second_run["skipped"] == 2
    assert existing.stat().st_mtime_ns == original_mtime


def test_generate_strm_repairs_invalid_target_without_rewriting_valid_media(monkeypatch, tmp_path):
    documents = [{"_id": "current-id", "name": "Movie.mkv", "parent": "/raphael/Filmes/Movie"}]
    patch_mongo_documents(monkeypatch, documents)
    output = tmp_path / "library"
    target = output / "Filmes" / "Movie" / "Movie.strm"
    target.parent.mkdir(parents=True)
    target.write_text("http://nebula.local/transcode?id=expired-id", encoding="utf-8")
    unrelated_invalid = output / "legacy.strm"
    unrelated_invalid.write_text("broken", encoding="utf-8")

    result = generate_strm.generate_strm_files(
        output_root=output,
        stream_base_url="http://nebula.local",
        library_user="raphael",
    )

    assert result["generated"] == 0
    assert result["updated"] == 1
    assert result["invalid_links"] == 2
    assert target.read_text(encoding="utf-8") == "http://nebula.local/stream?id=current-id"
    assert unrelated_invalid.read_text(encoding="utf-8") == "broken"


def test_generate_strm_does_not_duplicate_valid_link_found_elsewhere(monkeypatch, tmp_path):
    documents = [{"_id": "media-id", "name": "Episode.mkv", "parent": "/raphael/Series/Show/Season 01"}]
    patch_mongo_documents(monkeypatch, documents)
    output = tmp_path / "library"
    existing = output / "legacy" / "Episode.strm"
    existing.parent.mkdir(parents=True)
    existing.write_text("http://old-host/stream?id=media-id", encoding="utf-8")

    result = generate_strm.generate_strm_files(
        output_root=output,
        stream_base_url="http://nebula.local",
        library_user="raphael",
        prune=True,
    )

    assert result["generated"] == 0
    assert result["skipped"] == 1
    assert not (output / "Series" / "Show" / "Season 01" / "Episode.strm").exists()
    assert existing.read_text(encoding="utf-8") == "http://old-host/stream?id=media-id"


def test_generic_movie_info_hash_uses_folder_title_without_changing_mongo_name(tmp_path):
    document = {
        "_id": "hashed-movie",
        "name": "movie.info.96496eeb.mkv",
        "parent": "/raphael/Filmes/Amanhecer Sangrento (2025)",
    }

    targets = generate_strm.eligible_strm_targets([document], tmp_path, "raphael")

    assert targets["hashed-movie"][1] == (
        tmp_path / "Filmes" / "Amanhecer Sangrento (2025)" / "Amanhecer Sangrento (2025).strm"
    )
    assert document["name"] == "movie.info.96496eeb.mkv"


def test_nfo_sidecars_are_not_eligible_for_strm_but_nfo_named_media_is(tmp_path):
    documents = [
        {
            "_id": "movie-nfo-sidecar",
            "name": "movie.nfo.e67f9296.partial",
            "parent": "/raphael/Filmes/Os Roses (2025)",
        },
        {
            "_id": "episode-nfo-sidecar",
            "name": "Show - S01E01.nfo.85e60cc1.partial",
            "parent": "/raphael/Series/Show/Season 01",
        },
        {
            "_id": "playable-media-with-nfo-in-name",
            "name": "movie.nfo.backup.mkv",
            "parent": "/raphael/Filmes/Playable",
        },
    ]

    targets = generate_strm.eligible_strm_targets(documents, tmp_path, "raphael")

    assert set(targets) == {"playable-media-with-nfo-in-name"}


def test_completed_media_upload_creates_canonical_strm_and_is_idempotent(tmp_path):
    document = {
        "_id": "episode-id",
        "type": "file",
        "status": "completed",
        "name": "Show - S01E02.mkv",
        "parent": "/raphael/Series/Show/Season 01",
        "parts": [{"part_id": 0, "file_size": 1024}],
    }

    outcome, target = generate_strm.create_completed_media_strm(
        document,
        output_root=tmp_path,
        stream_base_url="http://nebula.local:2122",
        library_user="raphael",
    )

    assert outcome == "generated"
    assert target == tmp_path / "Series" / "Show" / "Season 01" / "Show - S01E02.strm"
    assert target.read_text(encoding="utf-8") == "http://nebula.local:2122/stream?id=episode-id"

    second_outcome, second_target = generate_strm.create_completed_media_strm(
        document,
        output_root=tmp_path,
        stream_base_url="http://nebula.local:2122",
        library_user="raphael",
    )

    assert second_outcome == "skipped"
    assert second_target == target


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "uploading"},
        {"parts": []},
        {"name": "movie.nfo.12345678.partial"},
        {"name": "movie.srt"},
        {"name": "movie.txt"},
    ],
)
def test_completed_upload_does_not_create_strm_for_incomplete_or_non_media_files(tmp_path, overrides):
    document = {
        "_id": "not-media-id",
        "type": "file",
        "status": "completed",
        "name": "Movie.mkv",
        "parent": "/raphael/Filmes/Movie",
        "parts": [{"part_id": 0, "file_size": 1024}],
        **overrides,
    }

    outcome, target = generate_strm.create_completed_media_strm(
        document,
        output_root=tmp_path,
        stream_base_url="http://nebula.local:2122",
        library_user="raphael",
    )

    assert outcome == "ineligible"
    assert target is None
    assert not list(tmp_path.rglob("*.strm"))


def test_completed_upload_strm_does_not_overwrite_conflicting_destination(tmp_path):
    document = {
        "_id": "new-media-id",
        "type": "file",
        "status": "completed",
        "name": "Movie.mkv",
        "parent": "/raphael/Filmes/Movie",
        "parts": [{"part_id": 0, "file_size": 1024}],
    }
    target = tmp_path / "Filmes" / "Movie" / "Movie.strm"
    target.parent.mkdir(parents=True)
    target.write_text("http://nebula.local/stream?id=another-id", encoding="utf-8")

    outcome, actual_target = generate_strm.create_completed_media_strm(
        document,
        output_root=tmp_path,
        stream_base_url="http://nebula.local:2122",
        library_user="raphael",
    )

    assert outcome == "conflict"
    assert actual_target == target
    assert target.read_text(encoding="utf-8") == "http://nebula.local/stream?id=another-id"


def test_generic_movie_strm_is_relocated_only_when_single_valid_link_exists(tmp_path):
    source = tmp_path / "Filmes" / "Amanhecer Sangrento (2025)" / "movie.info.96496eeb.strm"
    target = source.with_name("Amanhecer Sangrento (2025).strm")
    source.parent.mkdir(parents=True)
    source.write_text("http://old-host/stream?id=hashed-movie", encoding="utf-8")

    assert generate_strm.relocate_generic_movie_strm(
        [source], target, "hashed-movie", "http://nebula.local/stream?id=hashed-movie"
    )
    assert not source.exists()
    assert target.read_text(encoding="utf-8") == "http://nebula.local/stream?id=hashed-movie"


def test_generic_movie_strm_conflict_and_unverified_links_are_preserved(tmp_path):
    source = tmp_path / "Filmes" / "Movie (2025)" / "movie.info.96496eeb.strm"
    target = source.with_name("Movie (2025).strm")
    source.parent.mkdir(parents=True)
    source.write_text("http://old-host/stream?id=other-id", encoding="utf-8")
    target.write_text("manual content", encoding="utf-8")

    assert not generate_strm.relocate_generic_movie_strm(
        [source], target, "hashed-movie", "http://nebula.local/stream?id=hashed-movie"
    )
    assert source.read_text(encoding="utf-8") == "http://old-host/stream?id=other-id"
    assert target.read_text(encoding="utf-8") == "manual content"


def test_generate_strm_repairs_existing_generic_movie_filename(monkeypatch, tmp_path):
    document = {
        "_id": "hashed-movie",
        "name": "movie.info.96496eeb.mkv",
        "parent": "/raphael/Filmes/Amanhecer Sangrento (2025)",
    }
    patch_mongo_documents(monkeypatch, [document])
    output = tmp_path / "library"
    old_path = output / "Filmes" / "Amanhecer Sangrento (2025)" / "movie.info.96496eeb.strm"
    old_path.parent.mkdir(parents=True)
    old_path.write_text("http://old-host/stream?id=hashed-movie", encoding="utf-8")

    result = generate_strm.generate_strm_files(
        output_root=output,
        stream_base_url="http://nebula.local",
        library_user="raphael",
    )

    corrected_path = old_path.with_name("Amanhecer Sangrento (2025).strm")
    assert result["renamed"] == 1
    assert result["generated"] == 0
    assert not old_path.exists()
    assert corrected_path.read_text(encoding="utf-8") == (
        "http://nebula.local/stream?id=hashed-movie"
    )


def test_generate_strm_counts_duplicate_mongo_target_as_conflict(monkeypatch, tmp_path):
    documents = [
        {"_id": "first-id", "name": "Movie.mkv", "parent": "/raphael/Filmes/Movie"},
        {"_id": "second-id", "name": "Movie.mkv", "parent": "/raphael/Filmes/Movie"},
    ]
    patch_mongo_documents(monkeypatch, documents)
    target = tmp_path / "Filmes" / "Movie" / "Movie.strm"
    target.parent.mkdir(parents=True)
    target.write_text("http://nebula.local/stream?id=first-id", encoding="utf-8")

    result = generate_strm.generate_strm_files(
        output_root=tmp_path,
        stream_base_url="http://nebula.local",
        library_user="raphael",
    )

    assert result["skipped"] == 1
    assert result["conflicts"] == 1
    assert target.read_text(encoding="utf-8") == "http://nebula.local/stream?id=first-id"
