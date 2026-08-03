import stat
from pathlib import Path

import generate_strm


def test_stream_endpoint_transcodes_browser_incompatible_formats():
    assert generate_strm.stream_endpoint("movie.mp4") == "stream"
    assert generate_strm.stream_endpoint("episode.avi") == "transcode"
    assert generate_strm.stream_endpoint("movie.mkv") == "transcode"


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
