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

    # Filmes devem vir primeiro ordenados por ano decrescente (2026 -> 2025 -> 2024)
    assert names[0] == "Filme A (2026).strm"
    assert names[1] == "Filme C (2025).strm"
    assert names[2] == "Filme B (2024).strm"

    # Depois Porno
    assert names[3] == "Video 1.strm"

    # Depois Séries em ordem de episódio (E01 -> E02)
    assert names[4] == "Dark.S01E01.strm"
    assert names[5] == "Dark.S01E02.strm"


def test_destination_and_mongo_parent_mapping(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    src_movie = source / "Filmes" / "Matrix (1999).strm"

    destination = strm_downloader.destination_for(source, dest, src_movie)
    assert destination == dest / "Filmes" / "Matrix (1999)" / "Matrix (1999).strm"

    parent = strm_downloader.mongo_parent_for(dest, destination.parent, library_user="raphael")
    assert parent == "/raphael/Filmes/Matrix (1999)"


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
    set_data = updated_docs[-1][1]["$set"]
    assert set_data["status"] == "queued"
    assert set_data["name"] == "video.mkv"
    assert set_data["parent"] == "/raphael/Filmes/Filme (2026)"
    assert set_data["size"] == 13
    assert set_data["delete_source"] is True


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
    # Deve incluir strm, mkv e mp4, mas NÃO .part
    names = [it[1].name for it in items]
    assert names == ["Filme 2026 (2026).strm", "Filme 2025 (2025).mkv", "Filme 2024 (2024).mp4"]


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

    expected_stage_file = stage_dir / "strm" / "Matrix (1999).mkv"
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

