from __future__ import annotations

import io
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import strm_downloader


class _FakeHTTPResponse(io.BytesIO):
    def __init__(self, data: bytes, headers: dict[str, str] | None = None, status: int = 200):
        super().__init__(data)
        self._headers = headers or {"Content-Length": str(len(data))}
        self.status = status

    @property
    def headers(self):
        return self._headers

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_extract_media_year():
    assert strm_downloader.extract_media_year("Avatar (2022).strm") == 2022
    assert strm_downloader.extract_media_year("Gladiador.2.2024.1080p.mkv") == 2024
    assert strm_downloader.extract_media_year("Filme Sem Ano", "Filmes (2026)") == 2026
    assert strm_downloader.extract_media_year("Stranger Things S01E01.strm") == 0


def test_movie_and_episode_identity():
    movie = strm_downloader.movie_identity("Avatar The Way of Water (2022)")
    assert movie == ("avatar the way of water", "2022")

    ep = strm_downloader.episode_identity("Breaking Bad", "Breaking.Bad.S02E05.strm")
    assert ep == ("breaking bad", 2, 5)

    ep_pt = strm_downloader.episode_identity("Bleach", "Bleach s04e12.mkv")
    assert ep_pt == ("bleach", 4, 12)


def test_m3u_priority_matches_relative_path_and_title(tmp_path):
    root = tmp_path / "midias"
    target = root / "Series" / "Demo" / "Season 01" / "Demo - S01E01.strm"
    target.parent.mkdir(parents=True)
    target.write_text("https://stream.example.test/demo", encoding="utf-8")

    assert strm_downloader.is_priority_item(
        root,
        target,
        {"paths": ["Series\\Demo\\Season 01\\Demo - S01E01.strm"], "terms": []},
    )
    assert strm_downloader.is_priority_item(
        root,
        target,
        {"paths": [], "terms": ["demo - s01e01"]},
    )


def test_iter_strm_files_prioritized(tmp_path):
    source = tmp_path / "source"
    filmes_dir = source / "Filmes"
    series_dir = source / "Series" / "Dark" / "Season 01"
    porno_dir = source / "Porno"

    filmes_dir.mkdir(parents=True)
    series_dir.mkdir(parents=True)
    porno_dir.mkdir(parents=True)

    f_2024 = filmes_dir / "Filme B (2024).strm"
    f_2026 = filmes_dir / "Filme A (2026).strm"
    f_2025 = filmes_dir / "Filme C (2025).strm"

    s_e02 = series_dir / "Dark.S01E02.strm"
    s_e01 = series_dir / "Dark.S01E01.strm"

    p_1 = porno_dir / "Video 1.strm"

    for f in [f_2024, f_2026, f_2025, s_e02, s_e01, p_1]:
        f.write_text("http://stream.example.com/video.mkv", encoding="utf-8")

    items = strm_downloader.iter_strm_files_prioritized([source])
    names = [it[1].name for it in items]

    # Filmes devem vir primeiro ordenados alfabeticamente A-Z
    assert names[0] == "Filme A (2026).strm"
    assert names[1] == "Filme B (2024).strm"
    assert names[2] == "Filme C (2025).strm"

    # Depois Séries em ordem de episódio (E01 -> E02)
    assert names[3] == "Dark.S01E01.strm"
    assert names[4] == "Dark.S01E02.strm"

    # Depois Porno
    assert names[5] == "Video 1.strm"


def test_iter_strm_file_batches_prioritized_starts_before_full_scan(tmp_path):
    source = tmp_path / "source"
    films = source / "Filmes"
    series = source / "Series" / "Dark" / "Season 01"
    adult = source / "Porno"
    films.mkdir(parents=True)
    series.mkdir(parents=True)
    adult.mkdir(parents=True)

    paths = [
        films / "Filme antigo (2024).strm",
        films / "Filme novo (2026).strm",
        series / "Dark.S01E01.strm",
        adult / "Video 1.strm",
    ]
    for path in paths:
        path.write_text("http://stream.example.com/video.mkv", encoding="utf-8")

    batches = list(
        strm_downloader.iter_strm_file_batches_prioritized(
            [source], batch_size=2
        )
    )
    names_by_batch = [[item[1].name for item in batch] for batch in batches]

    assert [len(batch) for batch in batches] == [2, 2]
    assert names_by_batch[0] == ["Filme antigo (2024).strm", "Filme novo (2026).strm"]
    assert sorted(name for batch in names_by_batch for name in batch) == sorted(
        path.name for path in paths
    )


def test_iter_strm_file_batches_sorts_initial_accents_with_ascii_titles(tmp_path):
    source = tmp_path / "source"
    films = source / "Filmes"
    films.mkdir(parents=True)
    for name in ("Zorro (2020).strm", "Àrvore (2020).strm", "Abacate (2020).strm"):
        (films / name).write_text("http://stream.example.com/video.mkv", encoding="utf-8")

    batches = list(strm_downloader.iter_strm_file_batches_prioritized([source], batch_size=3))

    assert [item[1].name for item in batches[0]] == [
        "Abacate (2020).strm", "Àrvore (2020).strm", "Zorro (2020).strm"
    ]
    assert [item[1].name for item in strm_downloader.iter_strm_files_prioritized([source])] == [
        "Abacate (2020).strm", "Àrvore (2020).strm", "Zorro (2020).strm"
    ]


def test_iter_strm_file_batches_reports_progress_during_large_scan(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "video.strm").write_text(
        "http://stream.example.com/video.mkv", encoding="utf-8"
    )
    progress = []

    batches = list(
        strm_downloader.iter_strm_file_batches_prioritized(
            [source],
            batch_size=10,
            progress_callback=lambda *values: progress.append(values),
            progress_interval=0,
        )
    )

    assert len(batches) == 1
    assert progress
    assert progress[-1][2] == 1


def test_destination_and_mongo_parent_mapping(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    src_movie = source / "Filmes" / "Matrix (1999).strm"

    destination = strm_downloader.destination_for(source, dest, src_movie)
    assert destination == dest / "Filmes" / "Matrix (1999)" / "Matrix (1999).strm"

    parent = strm_downloader.mongo_parent_for(dest, destination.parent, library_user="raphael")
    assert parent == "/raphael/Filmes/Matrix (1999)"


def test_relocate_strm_to_mongo_category_preserves_link_content(tmp_path):
    source = tmp_path / "library"
    misplaced = source / "Animações" / "Prisma" / "Season 01" / "Prisma - S01E01.strm"
    misplaced.parent.mkdir(parents=True)
    link = "http://stream.example.test/stream?id=507f1f77bcf86cd799439011"
    misplaced.write_text(link, encoding="utf-8")

    corrected = strm_downloader.relocate_strm_to_mongo_parent(
        misplaced,
        source,
        "/raphael/Series/Prisma/Season 01",
    )

    expected = source / "Series" / "Prisma" / "Season 01" / misplaced.name
    assert corrected == expected
    assert corrected.read_text(encoding="utf-8") == link
    assert not misplaced.exists()


def test_relocate_strm_to_mongo_category_preserves_both_files_on_collision(tmp_path):
    source = tmp_path / "library"
    misplaced = source / "Animações" / "Prisma" / "Prisma.strm"
    target = source / "Series" / "Prisma" / "Prisma.strm"
    misplaced.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    misplaced.write_text("http://stream.example.test/stream?id=one", encoding="utf-8")
    target.write_text("http://stream.example.test/stream?id=two", encoding="utf-8")

    corrected = strm_downloader.relocate_strm_to_mongo_parent(
        misplaced,
        source,
        "/raphael/Series/Prisma",
    )

    assert corrected is None
    assert misplaced.read_text(encoding="utf-8").endswith("id=one")
    assert target.read_text(encoding="utf-8").endswith("id=two")


def test_relocate_strm_to_mongo_category_dry_run_does_not_move_file(tmp_path):
    source = tmp_path / "library"
    misplaced = source / "Animações" / "Prisma" / "Prisma.strm"
    misplaced.parent.mkdir(parents=True)
    misplaced.write_text("http://stream.example.test/stream?id=one", encoding="utf-8")

    planned = strm_downloader.relocate_strm_to_mongo_parent(
        misplaced,
        source,
        "/raphael/Series/Prisma",
        dry_run=True,
    )

    assert planned == source / "Series" / "Prisma" / misplaced.name
    assert misplaced.is_file()
    assert not planned.exists()


def test_media_validator_completed_and_active():
    validator = strm_downloader.MediaValidator(mongo_uri="", db_name="ftp")
    validator.completed_movies = {("matrix", "1999")}
    validator.completed_episodes = {("dark", 1, 1)}
    validator.exact_completed = {("/raphael/filmes/avatar (2009)", "avatar (2009).mkv")}
    validator.active_paths_or_names = {"em andamento"}

    # Filme concluído por identidade
    is_dupe, reason = validator.is_already_completed_or_active(Path("Matrix (1999).strm"))
    assert is_dupe is True
    assert "já concluído" in reason

    # Episódio concluído por identidade
    is_dupe, reason = validator.is_already_completed_or_active(
        Path("Dark S01E01.strm"),
    )
    assert is_dupe is True
    assert "dark s01e01" in reason.lower()

    # Mídia em andamento na fila
    is_dupe, reason = validator.is_already_completed_or_active(Path("em andamento.strm"))
    assert is_dupe is True
    assert "já está ativa" in reason

    # Mídia nova não concluída
    is_dupe, _ = validator.is_already_completed_or_active(Path("Novo Filme (2026).strm"))
    assert is_dupe is False


def test_media_validator_indexes_completed_mongo_parent_by_stream_id(monkeypatch):
    file_id = "507f1f77bcf86cd799439011"

    class MockFiles:
        def find_one(self, filter_query, *args, **kwargs):
            if filter_query.get("_id") == strm_downloader.ObjectId(file_id):
                return {"parent": "/raphael/Series/Prisma/Season 01"}
            return None

    class MockClient:
        def __init__(self, *args, **kwargs):
            self.ftp = SimpleNamespace(files=MockFiles())

        def __getitem__(self, name):
            return self.ftp

        def close(self):
            pass

    monkeypatch.setattr(strm_downloader, "MongoClient", MockClient)
    validator = strm_downloader.MediaValidator(mongo_uri="mongodb://localhost:27017")
    validator.refresh_cache(force=True)

    parent = validator.canonical_parent_for_stream(
        f"http://stream.example.test/stream?id={file_id}"
    )

    assert parent == "/raphael/Series/Prisma/Season 01"


def test_media_validator_does_not_use_cached_category_after_mongo_error(monkeypatch):
    class MockFiles:
        def find_one(self, *args, **kwargs):
            raise strm_downloader.PyMongoError("offline")

    class MockClient:
        def __init__(self, *args, **kwargs):
            self.ftp = SimpleNamespace(files=MockFiles())

        def __getitem__(self, name):
            return self.ftp

        def close(self):
            pass

    monkeypatch.setattr(strm_downloader, "MongoClient", MockClient)
    validator = strm_downloader.MediaValidator(mongo_uri="mongodb://localhost:27017")

    parent = validator.canonical_parent_for_stream(
        "http://stream.example.test/stream?id=507f1f77bcf86cd799439011"
    )

    assert parent is None
    assert validator.last_parent_lookup_available is False


def test_recovery_strm_items_are_resolved_from_mongo_destination(tmp_path):
    source = tmp_path / "strm"
    dest_root = tmp_path / "dest"
    strm = source / "Filmes" / "Example (2026)" / "Example.strm"
    strm.parent.mkdir(parents=True)
    strm.write_text("http://stream.example.test/video.mkv", encoding="utf-8")
    validator = strm_downloader.MediaValidator(mongo_uri="", library_user="raphael")
    validator.recovery_records = [
        ("/raphael/Filmes/Example (2026)", "Example.mkv", "record-id")
    ]
    validator.recovery_targets = {
        ("/raphael/filmes/example (2026)", "example.mkv"),
        ("/raphael/filmes/example (2026)", "example"),
    }

    recovered = validator.find_recovery_strm_items([source], dest_root)

    assert len(recovered) == 1
    assert recovered[0][1] == strm


def test_recovery_strm_destination_is_rejected_when_link_id_differs(tmp_path):
    source = tmp_path / "strm"
    dest_root = tmp_path / "dest"
    strm = source / "Series" / "Example" / "Example.strm"
    strm.parent.mkdir(parents=True)
    strm.write_text(
        "http://stream.example.test/video.mkv?id=507f1f77bcf86cd799439012",
        encoding="utf-8",
    )
    validator = strm_downloader.MediaValidator(mongo_uri="", library_user="raphael")
    validator.recovery_records = [
        ("/raphael/Series/Example", "Example.mkv", "507f1f77bcf86cd799439011")
    ]
    validator.recovery_ids = {"507f1f77bcf86cd799439011"}

    recovered = validator.find_recovery_strm_items([source], dest_root)

    assert recovered == []
    assert strm.is_file()


def test_recovery_strm_with_external_provider_id_is_not_rejected(tmp_path):
    source = tmp_path / "strm"
    dest_root = tmp_path / "dest"
    strm = source / "Series" / "Example" / "Example.strm"
    strm.parent.mkdir(parents=True)
    strm.write_text("https://provider.example.test/series/u/p/102317.mkv", encoding="utf-8")
    validator = strm_downloader.MediaValidator(mongo_uri="", library_user="raphael")
    validator.recovery_records = [
        ("/raphael/Series/Example", "Example.mkv", "507f1f77bcf86cd799439011")
    ]
    validator.recovery_targets = {
        ("/raphael/series/example", "example.mkv"),
        ("/raphael/series/example", "example"),
    }

    recovered = validator.find_recovery_strm_items([source], dest_root)

    assert [item[1] for item in recovered] == [strm]


def test_recovery_strm_misfiled_category_is_found_without_moving_during_scan(tmp_path):
    source = tmp_path / "strm"
    dest_root = tmp_path / "dest"
    strm = source / "Animações" / "Prisma" / "Season 01" / "Prisma - S01E01.strm"
    strm.parent.mkdir(parents=True)
    strm.write_text(
        "http://stream.example.test/stream?id=507f1f77bcf86cd799439011",
        encoding="utf-8",
    )
    validator = strm_downloader.MediaValidator(mongo_uri="", library_user="raphael")
    validator.recovery_records = [
        ("/raphael/Series/Prisma/Season 01", "Prisma - S01E01.mkv", "507f1f77bcf86cd799439011")
    ]
    validator.recovery_ids = {"507f1f77bcf86cd799439011"}

    recovered = validator.find_recovery_strm_items([source], dest_root)

    assert len(recovered) == 1
    assert recovered[0][1] == strm
    assert recovered[0][2] == "animacoes"
    assert strm.is_file()


def test_register_in_nebula_queue(tmp_path, monkeypatch):
    file_path = tmp_path / "video.mkv"
    file_path.write_bytes(b"content-12345")
    dest_file = tmp_path / "dest" / "Filmes" / "Filme (2026)" / "video.mkv"
    dest_root = tmp_path / "dest"

    inserted_docs = []
    updated_docs = []

    class MockFiles:
        def find_one(self, filter_query, *args, **kwargs):
            return None

        def update_one(self, filter_query, update_data, upsert=False):
            updated_docs.append((filter_query, update_data, upsert))

        def insert_one(self, doc):
            inserted_docs.append(doc)

    class MockClient:
        def __init__(self, *args, **kwargs):
            self.ftp = SimpleNamespace(files=MockFiles())

        def __getitem__(self, name):
            return self.ftp

        def close(self):
            pass

    monkeypatch.setattr(strm_downloader, "MongoClient", MockClient)

    success = strm_downloader.register_in_nebula_queue(
        downloaded_file=file_path,
        destination=dest_file,
        dest_root=dest_root,
        mongo_uri="mongodb://localhost:27017",
        db_name="ftp",
        library_user="raphael",
        delete_source=True,
    )

    assert success is True
    assert len(updated_docs) >= 1
    # Verifica documento enfileirado
    set_data = updated_docs[-1][1]["$setOnInsert"]
    assert set_data["status"] == "queued"
    assert set_data["name"] == "video.mkv"
    assert set_data["parent"] == "/raphael/Filmes/Filme (2026)"
    assert set_data["size"] == 13
    assert set_data["delete_source"] is True


def test_register_recovery_preserves_existing_mongo_parts(tmp_path, monkeypatch):
    file_path = tmp_path / "recovered.mkv"
    file_path.write_bytes(b"recovered media")
    dest_root = tmp_path / "dest"
    destination = dest_root / "Filmes" / "Example (2026)" / "Example.mkv"
    recovery_id = "507f1f77bcf86cd799439011"
    updates = []

    class MockFiles:
        def find_one(self, filter_query, *args, **kwargs):
            if filter_query.get("_id"):
                return {"_id": filter_query["_id"]}
            return None

        def update_one(self, filter_query, update_data, upsert=False):
            updates.append((filter_query, update_data, upsert))
            return SimpleNamespace(matched_count=1)

    class MockClient:
        def __init__(self, *args, **kwargs):
            self.ftp = SimpleNamespace(files=MockFiles())

        def __getitem__(self, name):
            return self.ftp

        def close(self):
            pass

    monkeypatch.setattr(strm_downloader, "MongoClient", MockClient)
    monkeypatch.setattr(
        strm_downloader,
        "ensure_mongo_parent_structure",
        lambda *args, **kwargs: None,
    )

    assert strm_downloader.register_in_nebula_queue(
        downloaded_file=file_path,
        destination=destination,
        dest_root=dest_root,
        mongo_uri="mongodb://localhost:27017",
        library_user="raphael",
        recovery_file_id=recovery_id,
    )

    query, update, _ = updates[-1]
    assert query["_id"]
    assert update["$set"]["recovery_required"] is False
    assert update["$set"]["status"] == "queued"
    assert "parts" not in update["$set"]
    assert update["$unset"] == {"failed_at": ""}


def test_recovery_strm_is_retained_after_redownload_is_queued(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    stage_dir = tmp_path / "stage"
    dest_root = tmp_path / "dest"
    strm_path = source_root / "Animações" / "Example (2026)" / "Example.strm"
    strm_path.parent.mkdir(parents=True)
    stage_dir.mkdir()
    dest_root.mkdir()
    strm_path.write_text(
        "http://stream.example.test/video.mkv?id=507f1f77bcf86cd799439011",
        encoding="utf-8",
    )

    validator = strm_downloader.MediaValidator(mongo_uri="")
    monkeypatch.setattr(validator, "refresh_cache", lambda force=False: None)
    def current_parent(_url):
        validator.last_parent_lookup_available = True
        return "/raphael/Series/Example (2026)"

    monkeypatch.setattr(validator, "canonical_parent_for_stream", current_parent)
    monkeypatch.setattr(validator, "is_recovery_candidate", lambda *args, **kwargs: True)
    monkeypatch.setattr(validator, "is_already_completed_or_active", lambda *args, **kwargs: (False, "recovery"))
    monkeypatch.setattr(strm_downloader, "remote_content_size", lambda _url: 1024)
    monkeypatch.setattr(strm_downloader, "get_best_staging_root", lambda *args, **kwargs: stage_dir)

    downloaded = stage_dir / "Series" / "Example (2026)" / "Example.mkv"
    downloaded.parent.mkdir(parents=True)
    downloaded.write_bytes(b"recovered")
    monkeypatch.setattr(strm_downloader, "download_strm_multipart", lambda **kwargs: downloaded)
    registered = []

    def mock_register(**kwargs):
        registered.append(kwargs)
        return True

    monkeypatch.setattr(strm_downloader, "register_in_nebula_queue", mock_register)

    assert strm_downloader.process_strm_item(
        source_root=source_root,
        strm_path=strm_path,
        category="animações",
        year=2026,
        dest_root=dest_root,
        staging_dirs=[stage_dir],
        validator=validator,
        mongo_uri="mongodb://test",
        db_name="ftp",
        library_user="raphael",
    )

    assert not strm_path.exists()
    assert (source_root / "Series" / "Example (2026)" / strm_path.name).is_file()
    assert registered[0]["recovery_file_id"] == "507f1f77bcf86cd799439011"


def test_download_strm_multipart_direct_and_resume(tmp_path, monkeypatch):
    target = tmp_path / "output.mp4"
    data = b"0123456789" * 100  # 1000 bytes

    def mock_urlopen(request, timeout=20):
        headers = dict(request.headers)
        rng = headers.get("Range", "")
        if rng == "bytes=0-0":
            return _FakeHTTPResponse(b"0", headers={"Content-Range": f"bytes 0-0/{len(data)}"}, status=206)
        if rng.startswith("bytes="):
            start_str, end_str = rng.replace("bytes=", "").split("-")
            start, end = int(start_str), int(end_str)
            chunk = data[start:end + 1]
            return _FakeHTTPResponse(chunk, headers={"Content-Length": str(len(chunk))}, status=206)
        return _FakeHTTPResponse(data, headers={"Content-Length": str(len(data))}, status=200)

    monkeypatch.setattr(strm_downloader, "urlopen", mock_urlopen)
    monkeypatch.setattr(strm_downloader, "wait_for_disk_capacity", lambda *_args, **_kwargs: None)

    res = strm_downloader.download_strm_multipart(
        url="http://example.com/video.mp4",
        target_path=target,
        parts_count=4,
        read_timeout=10,
    )

    assert res.exists()
    assert res.read_bytes() == data


def test_delete_strm_and_empty_parents(tmp_path):
    source_root = tmp_path / "source"
    movie_dir = source_root / "Filmes" / "Avatar (2009)"
    movie_dir.mkdir(parents=True)
    strm_file = movie_dir / "Avatar (2009).strm"
    strm_file.write_text("http://example.com/movie.mkv", encoding="utf-8")

    assert strm_file.exists()
    assert movie_dir.exists()

    # Deleta strm e limpa pasta que ficou vazia
    strm_downloader.delete_strm_and_empty_parents(strm_file, source_root)

    assert not strm_file.exists()
    assert not movie_dir.exists()
    # Raiz source_root nunca deve ser apagada
    assert source_root.exists()


def test_delete_strm_preserves_folder_with_other_media(tmp_path):
    source_root = tmp_path / "source"
    series_dir = source_root / "Series" / "Dark" / "Season 01"
    series_dir.mkdir(parents=True)
    ep1 = series_dir / "Dark.S01E01.strm"
    ep2 = series_dir / "Dark.S01E02.strm"
    ep1.write_text("http://example.com/e1.mkv", encoding="utf-8")
    ep2.write_text("http://example.com/e2.mkv", encoding="utf-8")

    strm_downloader.delete_strm_and_empty_parents(ep1, source_root)

    assert not ep1.exists()
    # A pasta continua existindo pois contém ep2
    assert ep2.exists()
    assert series_dir.exists()


def test_gui_parse_downloader_lines():
    from unittest.mock import MagicMock
    from gui import NebulaGUI

    gui = NebulaGUI.__new__(NebulaGUI)
    gui.dl_name_lbl = MagicMock()
    gui.dl_meta_lbl = MagicMock()
    gui.dl_pbar = {"value": 0}
    gui.dl_pct_lbl = MagicMock()

    # 1. Teste de Início
    start_line = "[12:55:56][INFO][STRM] [1 MÍDIA POR VEZ] Iniciando download: Não Deseje Boa Sorte (2026).strm [FILMES/2026] -> Stage: E:\\NebulaStage\\strm"
    gui._parse_downloader_line(start_line)
    gui.dl_name_lbl.config.assert_called_with(text="Baixando: Não Deseje Boa Sorte (2026).strm")
    gui.dl_meta_lbl.config.assert_called_with(text="Destino em Stage: E:\\NebulaStage\\strm")

    # 2. Teste de Progresso
    prog_line = "[12:56:00][INFO][STRM] Baixando Não Deseje Boa Sorte (2026).mkv: 18.8 MB / 1873.6 MB (1%) - Vel: 7.5 MB/s"
    gui._parse_downloader_line(prog_line)
    gui.dl_name_lbl.config.assert_called_with(text="Baixando: Não Deseje Boa Sorte (2026).mkv")
    assert gui.dl_pbar["value"] == 1.0
    gui.dl_pct_lbl.config.assert_called_with(text="1.0% (18.8 MB / 1873.6 MB | Vel: 7.5 MB/s)")

    # 3. Teste de Mídia Pronta Detectada
    ready_line = "[12:56:10][INFO][STRM] [1 MÍDIA POR VEZ] Mídia pronta detectada: Filme Pronto (2025).mkv [FILMES/2025] -> Movendo para Stage: E:\\NebulaStage\\strm"
    gui._parse_downloader_line(ready_line)
    gui.dl_name_lbl.config.assert_called_with(text="Mídia Pronta: Filme Pronto (2025).mkv")
    gui.dl_meta_lbl.config.assert_called_with(text="Movendo para Stage: E:\\NebulaStage\\strm")
    assert gui.dl_pbar["value"] == 50

    # 4. Teste de Conclusão
    done_line = "[12:55:55][INFO][STRM] [1 MÍDIA POR VEZ] Conclusão do processamento de: Nossa Vizinhança (2026).strm. Pronto para a próxima mídia."
    gui._parse_downloader_line(done_line)
    gui.dl_name_lbl.config.assert_called_with(text="Concluído: Nossa Vizinhança (2026).strm")
    assert gui.dl_pbar["value"] == 100


def test_iter_strm_files_prioritized_includes_ready_media_files(tmp_path):
    source = tmp_path / "source"
    movie_dir = source / "Filmes"
    movie_dir.mkdir(parents=True)

    strm_file = movie_dir / "Filme 2026 (2026).strm"
    strm_file.write_text("http://example.com/f2026.mkv", encoding="utf-8")

    mkv_file = movie_dir / "Filme 2025 (2025).mkv"
    mkv_file.write_bytes(b"dummy mkv content")

    mp4_file = movie_dir / "Filme 2024 (2024).mp4"
    mp4_file.write_bytes(b"dummy mp4 content")

    part_file = movie_dir / "Filme 2023.part"
    part_file.write_bytes(b"temporary part")

    items = strm_downloader.iter_strm_files_prioritized([source])
    # Deve incluir strm, mkv e mp4 em ordem alfabética, mas NÃO .part
    names = [it[1].name for it in items]
    assert names == ["Filme 2024 (2024).mp4", "Filme 2025 (2025).mkv", "Filme 2026 (2026).strm"]


def test_process_strm_item_moves_ready_media_to_stage(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    stage_dir = tmp_path / "stage"
    dest_root = tmp_path / "dest"
    source_root.mkdir()
    stage_dir.mkdir()
    dest_root.mkdir()

    movie_folder = source_root / "Filmes" / "Matrix (1999)"
    movie_folder.mkdir(parents=True)
    media_file = movie_folder / "Matrix (1999).mkv"
    media_file.write_bytes(b"media binary content 12345")

    validator = strm_downloader.MediaValidator(mongo_uri="mongodb://localhost:27017")
    # Mock do validador para não conectar ao Mongo/Telegram
    monkeypatch.setattr(validator, "is_already_completed_or_active", lambda *args, **kwargs: (False, "new"))
    monkeypatch.setattr(strm_downloader, "wait_for_disk_capacity", lambda *_args, **_kwargs: None)

    registered_items = []
    def mock_register(downloaded_file, destination, dest_root, mongo_uri, db_name, library_user, delete_source=True):
        registered_items.append({
            "downloaded_file": downloaded_file,
            "destination": destination,
            "delete_source": delete_source,
        })
        return True

    monkeypatch.setattr(strm_downloader, "register_in_nebula_queue", mock_register)

    success = strm_downloader.process_strm_item(
        source_root=source_root,
        strm_path=media_file,
        category="filmes",
        year=1999,
        dest_root=dest_root,
        staging_dirs=[stage_dir],
        validator=validator,
        mongo_uri="mongodb://localhost:27017",
        db_name="nebula_test",
        library_user="main_user",
        parts_count=20,
    )

    assert success is True
    # O arquivo deve ter sido movido da origem para o stage
    assert not media_file.exists()
    # A pasta do filme que ficou vazia na origem deve ter sido deletada
    assert not movie_folder.exists()

    expected_stage_file = stage_dir / "Filmes" / "Matrix (1999)" / "Matrix (1999).mkv"
    assert expected_stage_file.exists()
    assert expected_stage_file.read_bytes() == b"media binary content 12345"

    assert len(registered_items) == 1
    assert registered_items[0]["delete_source"] is True
    assert registered_items[0]["downloaded_file"] == expected_stage_file


def test_failure_tracker_record_and_skip(tmp_path):
    cache_file = tmp_path / "failures.json"
    tracker = strm_downloader.FailureTracker(cache_file=cache_file, cooldown_seconds=3600)
    fake_strm = tmp_path / "Filme Quebrado (2026).strm"
    fake_strm.write_text("http://dead.example.com", encoding="utf-8")

    should_skip, _ = tracker.should_skip(fake_strm)
    assert should_skip is False

    # Registra erro 401
    tracker.record_failure(fake_strm, "HTTP Error 401: Unauthorized")
    should_skip, reason = tracker.should_skip(fake_strm)
    assert should_skip is True
    assert "401" in reason

    # Novo tracker carregando o mesmo arquivo JSON
    tracker2 = strm_downloader.FailureTracker(cache_file=cache_file, cooldown_seconds=3600)
    should_skip2, _ = tracker2.should_skip(fake_strm)
    assert should_skip2 is True

    # Sucesso limpa o histórico
    tracker2.record_success(fake_strm)
    should_skip3, _ = tracker2.should_skip(fake_strm)
    assert should_skip3 is False


def test_find_existing_staging_target(tmp_path):
    dest_root = tmp_path / "dest"
    stage1 = tmp_path / "stage1"
    stage2 = tmp_path / "stage2"
    stage1.mkdir()
    stage2.mkdir()

    dest = dest_root / "Filmes" / "Filme A (2025)" / "Filme A (2025).mp4"

    # Inicialmente não existe em nenhum stage
    target, has_existing = strm_downloader.find_existing_staging_target(dest_root, dest, [stage1, stage2])
    assert target is None
    assert has_existing is False

    # Cria download parcial no stage2 (simulando parada inesperada em sessão anterior)
    stage2_movie_dir = stage2 / "Filmes" / "Filme A (2025)"
    stage2_movie_dir.mkdir(parents=True)
    part_file = stage2_movie_dir / ".Filme A (2025).download.part0"
    part_file.write_bytes(b"partial-data")

    target, has_existing = strm_downloader.find_existing_staging_target(dest_root, dest, [stage1, stage2])
    assert has_existing is True
    assert target == stage2 / "Filmes" / "Filme A (2025)" / "Filme A (2025).mp4"


def test_wait_for_disk_capacity_switches_stage_and_preserves_partials(tmp_path, monkeypatch):
    stage_f = tmp_path / "stage-f"
    stage_e = tmp_path / "stage-e"
    stage_f.mkdir()
    stage_e.mkdir()
    relative = Path("Filmes") / "Example" / "Example.mkv"
    target_f = stage_f / relative
    target_f.parent.mkdir(parents=True)
    partials = {
        f".{target_f.stem}.download.meta": b'{"total": 6}',
        f".{target_f.stem}.download.part0": b"part-zero",
        f".{target_f.stem}.download.part1.tmp": b"part-one",
    }
    for name, content in partials.items():
        (target_f.parent / name).write_bytes(content)

    def disk_usage(path):
        path = Path(path)
        if path.is_relative_to(stage_f):
            return SimpleNamespace(total=100, free=10)
        if path.is_relative_to(stage_e):
            return SimpleNamespace(total=100, free=80)
        raise AssertionError(f"Unexpected disk checked: {path}")

    monkeypatch.setattr(strm_downloader.shutil, "disk_usage", disk_usage)

    target = strm_downloader.wait_for_disk_capacity(
        target_f,
        required_bytes=1,
        min_free_percent=10,
        poll_seconds=0,
        staging_dirs=[stage_f, stage_e],
    )

    assert target == stage_e / relative
    for name, content in partials.items():
        assert (target_f.parent / name).read_bytes() == content
        assert (target.parent / name).read_bytes() == content


def test_cleanup_staging_partials_removes_copies_after_final_file_exists(tmp_path):
    stage_f = tmp_path / "stage-f"
    stage_e = tmp_path / "stage-e"
    relative = Path("Filmes") / "Example" / "Example.mkv"
    target_f = stage_f / relative
    target_e = stage_e / relative
    target_f.parent.mkdir(parents=True)
    target_e.parent.mkdir(parents=True)
    target_e.write_bytes(b"complete-media")
    for root in (stage_f, stage_e):
        (root / relative.parent / ".Example.download.part0").write_bytes(b"partial")
        (root / relative.parent / ".Example.download.meta").write_text("{}", encoding="utf-8")

    strm_downloader.cleanup_staging_partials(target_e, [stage_f, stage_e])

    assert target_e.read_bytes() == b"complete-media"
    assert list(target_f.parent.glob(".Example.download*")) == []
    assert list(target_e.parent.glob(".Example.download*")) == []


def test_relocate_staging_partials_preserves_conflicting_files(tmp_path):
    source = tmp_path / "stage-f" / "Example"
    destination = tmp_path / "stage-e" / "Example"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    source_partial = source / ".Example.download.part0"
    destination_partial = destination / source_partial.name
    source_partial.write_bytes(b"source-progress")
    destination_partial.write_bytes(b"different-progress")

    assert not strm_downloader.relocate_staging_partials(
        source / "Example.mkv",
        destination / "Example.mkv",
    )
    assert source_partial.read_bytes() == b"source-progress"
    assert destination_partial.read_bytes() == b"different-progress"


def test_relocate_staging_partials_keeps_source_changed_during_copy(tmp_path, monkeypatch):
    source = tmp_path / "stage-f" / "Example"
    destination = tmp_path / "stage-e" / "Example"
    source.mkdir(parents=True)
    source_partial = source / ".Example.download.part0"
    source_partial.write_bytes(b"source-progress")
    copy2 = strm_downloader.shutil.copy2

    def changing_copy(src, dst):
        result = copy2(src, dst)
        Path(src).write_bytes(b"new-progress")
        return result

    monkeypatch.setattr(strm_downloader.shutil, "copy2", changing_copy)

    assert not strm_downloader.relocate_staging_partials(
        source / "Example.mkv",
        destination / "Example.mkv",
    )
    assert source_partial.read_bytes() == b"new-progress"
    assert not (destination / source_partial.name).exists()


def test_failure_tracker_allows_interrupted_and_partial_downloads(tmp_path):
    cache_file = tmp_path / "failures.json"
    tracker = strm_downloader.FailureTracker(cache_file=cache_file, cooldown_seconds=3600)
    fake_strm = tmp_path / "Filme Interrompido (2026).strm"
    fake_strm.write_text("http://stream.example.com", encoding="utf-8")

    # Erro transitório (interrupção inesperada, timeout, socket error)
    tracker.record_failure(fake_strm, "Connection reset by peer")
    # Tentativa 1: NÃO deve pular, permitindo retomar na próxima sessão
    should_skip, _ = tracker.should_skip(fake_strm)
    assert should_skip is False

    # Tentativa 2
    tracker.record_failure(fake_strm, "Timeout reading part 2")
    should_skip, _ = tracker.should_skip(fake_strm)
    assert should_skip is False

    # Se atingir 3 tentativas, normalmente entraria em cooldown
    tracker.record_failure(fake_strm, "Timeout reading part 3")
    should_skip, reason = tracker.should_skip(fake_strm)
    assert should_skip is True
    assert "Timeout" in reason

    # Mas se houver arquivos de download parcial no disco de stage, NUNCA pula (retoma a sessão!)
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    partial = stage_dir / ".Filme Interrompido (2026).download.part0"
    partial.write_bytes(b"data")
    should_skip_resumed, _ = tracker.should_skip(fake_strm, staging_dirs=[stage_dir])
    assert should_skip_resumed is False


def test_download_strm_multipart_resumes_partial_part_file(tmp_path, monkeypatch):
    target = tmp_path / "resumed_video.mp4"
    data = b"0123456789" * 100  # 1000 bytes (4 partes de 250 bytes)
    tmp_path_base = tmp_path / ".resumed_video.download"

    # Pre-cria part0 completa (250 bytes) e part1 parcial (100 bytes) em .tmp
    part0 = Path(f"{tmp_path_base}.part0")
    part0.write_bytes(data[0:250])
    part1_tmp = Path(f"{tmp_path_base}.part1.tmp")
    part1_tmp.write_bytes(data[250:350])

    requested_ranges = []

    def mock_urlopen(request, timeout=20):
        rng = request.headers.get("Range", "")
        if rng == "bytes=0-0":
            return _FakeHTTPResponse(b"0", headers={"Content-Range": f"bytes 0-0/{len(data)}"}, status=206)
        if rng.startswith("bytes="):
            start_str, end_str = rng.replace("bytes=", "").split("-")
            start, end = int(start_str), int(end_str)
            requested_ranges.append((start, end))
            chunk = data[start:end + 1]
            return _FakeHTTPResponse(chunk, headers={"Content-Length": str(len(chunk))}, status=206)
        return _FakeHTTPResponse(data, headers={"Content-Length": str(len(data))}, status=200)

    monkeypatch.setattr(strm_downloader, "urlopen", mock_urlopen)
    monkeypatch.setattr(strm_downloader, "wait_for_disk_capacity", lambda *_args, **_kwargs: None)

    res = strm_downloader.download_strm_multipart(
        url="http://example.com/video.mp4",
        target_path=target,
        parts_count=4,
        read_timeout=10,
    )

    assert res.exists()
    assert res.read_bytes() == data
    # part1 deve ter sido retomada a partir de 350 (250 + 100) ate 499!
    assert (350, 499) in requested_ranges
    # part0 já estava completa, portanto range 0-249 NÃO deve ter sido solicitado via HTTP
    assert (0, 249) not in requested_ranges


def test_gui_parse_downloader_lines_resume():
    from unittest.mock import MagicMock
    from gui import NebulaGUI

    gui = NebulaGUI.__new__(NebulaGUI)
    gui.dl_name_lbl = MagicMock()
    gui.dl_meta_lbl = MagicMock()
    gui.dl_pbar = {"value": 0}
    gui.dl_pct_lbl = MagicMock()

    resume_line = "[12:56:00][INFO][STRM] Resumindo Filme.mp4: 50.0 MB / 100.0 MB (50%) já no disco."
    gui._parse_downloader_line(resume_line)

    gui.dl_name_lbl.config.assert_called_with(text="Resumindo: Filme.mp4")
    assert gui.dl_pbar["value"] == 50.0
    gui.dl_pct_lbl.config.assert_called_with(text="50.0% (50.0 MB / 100.0 MB já no disco)")


def test_format_download_error_http_406_asn_blocked():
    from urllib.error import HTTPError
    from io import BytesIO

    headers = {
        "X-Debug-ASNAllowed": "0",
        "X-Debug-ASNOrg": "TELEFONICA BRASIL S.A",
    }
    err = HTTPError("http://example.com/stream.mkv", 406, "Not Acceptable", headers, BytesIO(b""))
    formatted = strm_downloader.format_download_error(err)
    assert "HTTP Error 406" in formatted
    assert "TELEFONICA BRASIL S.A" in formatted
    assert "VPN" in formatted


def test_failure_tracker_circuit_breaker_on_repeated_fatal_errors(tmp_path):
    cache_file = tmp_path / "failures.json"
    tracker = strm_downloader.FailureTracker(cache_file=cache_file, cooldown_seconds=3600)

    ep1 = tmp_path / "Animações" / "Os Simpsons" / "Season 01" / "Os Simpsons - S01E01.strm"
    ep2 = tmp_path / "Animações" / "Os Simpsons" / "Season 01" / "Os Simpsons - S01E02.strm"
    ep3 = tmp_path / "Animações" / "Os Simpsons" / "Season 01" / "Os Simpsons - S01E03.strm"
    ep4 = tmp_path / "Animações" / "Os Simpsons" / "Season 01" / "Os Simpsons - S01E04.strm"
    for ep in (ep1, ep2, ep3, ep4):
        ep.parent.mkdir(parents=True, exist_ok=True)
        ep.write_text("http://example.com", encoding="utf-8")

    # Inicialmente nenhuma série está pausada
    assert tracker.is_series_broken(ep1)[0] is False

    # 1ª falha fatal
    tracker.record_failure(ep1, "HTTP Error 403: Forbidden")
    assert tracker.is_series_broken(ep1)[0] is False

    # 2ª falha fatal
    tracker.record_failure(ep2, "HTTP Error 403: Forbidden")
    assert tracker.is_series_broken(ep1)[0] is False

    # 3ª falha fatal -> deve disparar o circuit breaker para a série!
    tracker.record_failure(ep3, "HTTP Error 403: Forbidden")
    is_broken, reason = tracker.is_series_broken(ep4)
    assert is_broken is True
    assert "Os Simpsons" in reason

    # ep4 deve ser pulado automaticamente pelo should_skip sem precisar tentar requisição de rede
    should_skip, skip_reason = tracker.should_skip(ep4)
    assert should_skip is True
    assert "Os Simpsons" in skip_reason

    # Reset do ciclo deve limpar o circuit breaker
    tracker.reset_cycle()
    assert tracker.is_series_broken(ep4)[0] is False


def test_failure_tracker_http_406_does_not_pause_sibling_episodes(tmp_path):
    tracker = strm_downloader.FailureTracker(cache_file=tmp_path / "failures.json")
    episodes = [
        tmp_path / "Animações" / "You and I Are Polar Opposites" / "Season 01" / f"episode{n}.strm"
        for n in range(1, 4)
    ]
    for episode in episodes:
        episode.parent.mkdir(parents=True, exist_ok=True)
        episode.write_text("https://example.test/stream", encoding="utf-8")
        tracker.record_failure(episode, "HTTP Error 406: Not Acceptable")

    assert tracker.is_series_broken(episodes[0])[0] is False
    assert tracker.should_skip(episodes[0])[0] is True  # cooldown remains per-file




def test_resolve_directory_path_rebuilds_objectid_parent_chain():
    class ObjectId:  # bson é substituído por mocks em outros módulos de teste
        pass

    root, series, season = ObjectId(), ObjectId(), ObjectId()
    nodes = {
        season: {"name": "Season 02", "parent": series},
        series: {"name": "Adolepeixes", "parent": root},
        root: {"name": "Animações", "parent": "/raphael"},
    }

    class Files:
        def find_one(self, query, *_args):
            return next(
                (node for key, node in nodes.items() if key is query["_id"]),
                None,
            )

    memo: dict[str, str] = {}
    assert (
        strm_downloader.resolve_directory_path(Files(), season, memo)
        == "/raphael/Animações/Adolepeixes/Season 02"
    )
    assert strm_downloader.is_object_id_parent(season)
    assert not strm_downloader.is_object_id_parent("/raphael/Filmes")
