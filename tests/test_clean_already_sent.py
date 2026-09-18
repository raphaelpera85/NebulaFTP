import os
import sys
from pathlib import Path

# Add tools to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import clean_already_sent


def test_clean_sources_preserves_folder_with_pending_strm(tmp_path):
    source = tmp_path / "midias"
    movie_folder = source / "Filmes" / "Movie Name (2026)"
    movie_folder.mkdir(parents=True)
    strm_file = movie_folder / "Movie Name (2026).strm"
    strm_file.write_text("https://example.com/movie.mkv", encoding="utf-8")
    poster = movie_folder / "poster.jpg"
    poster.write_bytes(b"poster")

    # Completed items does NOT contain this movie
    completed_items = {clean_already_sent.normalize_string("Other Movie (2020)")}

    removed = clean_already_sent.clean_sources([str(source)], completed_items, dry_run=False)

    assert removed == 0
    assert movie_folder.exists()
    assert strm_file.exists()
    assert poster.exists()


def test_clean_sources_deletes_folder_when_strm_is_completed(tmp_path):
    source = tmp_path / "midias"
    movie_folder = source / "Filmes" / "Movie Name (2026)"
    movie_folder.mkdir(parents=True)
    strm_file = movie_folder / "Movie Name (2026).strm"
    strm_file.write_text("https://example.com/movie.mkv", encoding="utf-8")
    poster = movie_folder / "poster.jpg"
    poster.write_bytes(b"poster")

    # Completed items contains this movie
    completed_items = {clean_already_sent.normalize_string("Movie Name (2026)")}

    removed = clean_already_sent.clean_sources([str(source)], completed_items, dry_run=False)

    assert removed > 0
    assert not movie_folder.exists()


def test_clean_sources_partial_series_strm_preserves_uncompleted_episodes(tmp_path):
    source = tmp_path / "midias"
    season_folder = source / "Series" / "My Series (2026)" / "Season 01"
    season_folder.mkdir(parents=True)
    ep1 = season_folder / "My Series (2026) - S01E01.strm"
    ep2 = season_folder / "My Series (2026) - S01E02.strm"
    ep1.write_text("https://example.com/s01e01.mkv", encoding="utf-8")
    ep2.write_text("https://example.com/s01e02.mkv", encoding="utf-8")

    # Only episode 1 is completed
    completed_items = {clean_already_sent.normalize_string("My Series (2026) - S01E01")}

    removed = clean_already_sent.clean_sources([str(source)], completed_items, dry_run=False)

    assert not ep1.exists()
    assert ep2.exists()
    assert season_folder.exists()


def test_get_completed_telegram_items_handles_objectid_parent():
    from bson import ObjectId

    class FakeCollection:
        def __init__(self, docs):
            self.docs = docs

        def find(self, query, projection=None):
            results = []
            for d in self.docs:
                matches = True
                for k, v in query.items():
                    if d.get(k) != v:
                        matches = False
                        break
                if matches:
                    results.append(d)
            return results

    dir_id = ObjectId()
    fake_db = type("FakeDB", (), {
        "files": FakeCollection([
            {"_id": dir_id, "type": "dir", "name": "Avatar (2009)"},
            {"_id": ObjectId(), "type": "file", "status": "completed", "name": "Avatar (2009).mkv", "parent": dir_id},
            {"_id": ObjectId(), "type": "file", "status": "completed", "name": "Matrix (1999).mp4", "parent": "/Filmes/Matrix (1999)"},
        ])
    })

    items = clean_already_sent.get_completed_telegram_items(fake_db)
    assert clean_already_sent.normalize_string("Avatar (2009)") in items
    assert clean_already_sent.normalize_string("Matrix (1999)") in items

