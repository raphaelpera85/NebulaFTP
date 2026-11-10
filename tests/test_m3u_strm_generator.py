"""
tests/test_m3u_strm_generator.py
Tests for tools.m3u_strm_generator
"""

from __future__ import annotations

import io
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.m3u_strm_generator import (
    M3UEntry,
    _fix_recovery_items,
    _normalise_for_match,
    _parse_extinf_attrs,
    canonical_strm_path,
    group_to_category,
    normalize_m3u_url,
    parse_m3u,
    parse_movie_name,
    parse_series_name,
    redact_url,
    write_strm_if_new,
)


# ---------------------------------------------------------------------------
# group_to_category
# ---------------------------------------------------------------------------

class TestGroupToCategory(unittest.TestCase):
    def test_animes(self):
        assert group_to_category("ANIMES") == "Animações"

    def test_animes_mixed_case(self):
        assert group_to_category("Animes") == "Animações"

    def test_doramas(self):
        assert group_to_category("DORAMAS") == "Doramas"

    def test_novelas(self):
        assert group_to_category("NOVELAS") == "Novelas"

    def test_filmes_prefix(self):
        assert group_to_category("FILMES: AÇÃO") == "Filmes"
        assert group_to_category("FILMES") == "Filmes"
        assert group_to_category("Filmes: Comédia") == "Filmes"

    def test_platform_series_url(self):
        url = "https://cn53.org:443/series/u/p/12345.mkv"
        assert group_to_category("NETFLIX", url) == "Series"

    def test_platform_movie_url(self):
        url = "https://cn53.org:443/movie/u/p/12345.mkv"
        assert group_to_category("NETFLIX", url) == "Filmes"

    def test_platform_live_skipped(self):
        # live TV under a known platform (no /series/ or /movie/) → None
        url = "https://cn53.org:443/u/p/12345"
        assert group_to_category("NETFLIX", url) is None

    def test_existing_media_root_categories(self):
        assert group_to_category("FILMES") == "Filmes"
        assert group_to_category("SERIES") == "Series"
        assert group_to_category("NOVELAS") == "Novelas"
        assert group_to_category("DORAMAS") == "Doramas"
        assert group_to_category("ANIMAÇÕES") == "Animações"

    def test_url_prefix_and_query_secret_redaction(self):
        raw = "@url:`https://cn53.org/get.php?username=user123&password=pass123&type=m3u_plus`"
        normalized = normalize_m3u_url(raw)
        assert normalized.startswith("https://cn53.org/get.php?")
        assert "@url:" not in normalized
        safe = redact_url(normalized)
        assert "user123" not in safe
        assert "pass123" not in safe
        assert "[REDACTED]" in safe

    def test_unknown_group_series_url(self):
        url = "https://cn53.org:443/series/u/p/999.mkv"
        assert group_to_category("PROGRAMAS DE TV", url) == "Series"

    def test_unknown_group_movie_url(self):
        url = "https://cn53.org:443/movie/u/p/999.mkv"
        assert group_to_category("PROGRAMAS DE TV", url) == "Filmes"

    def test_live_tv_skipped(self):
        url = "https://cn53.org:443/user/pass/999"
        assert group_to_category("PROGRAMAS DE TV", url) is None

    def test_hbo_max(self):
        url = "https://cn53.org:443/series/u/p/999.mkv"
        assert group_to_category("HBO MAX", url) == "Series"


# ---------------------------------------------------------------------------
# parse_series_name
# ---------------------------------------------------------------------------

class TestParseSeriesName(unittest.TestCase):
    def test_basic(self):
        result = parse_series_name("1 Contra Todos S01 E01")
        assert result == ("1 Contra Todos", 1, 1)

    def test_compact_format(self):
        result = parse_series_name("Digimon Frontier S01E39")
        assert result == ("Digimon Frontier", 1, 39)

    def test_with_trailing_tag(self):
        result = parse_series_name("My Show S02 E05 [FHD]")
        assert result == ("My Show", 2, 5)

    def test_no_match(self):
        result = parse_series_name("É Fada! (2016)")
        assert result is None

    def test_three_digit_ep(self):
        result = parse_series_name("Dragon Ball Z S01 E150")
        assert result == ("Dragon Ball Z", 1, 150)


# ---------------------------------------------------------------------------
# parse_movie_name
# ---------------------------------------------------------------------------

class TestParseMovieName(unittest.TestCase):
    def test_basic(self):
        title, year = parse_movie_name("É Natal, Carol! (2012)")
        assert year == 2012
        assert "Carol" in title

    def test_no_year(self):
        title, year = parse_movie_name("Filme Sem Ano")
        assert year is None
        assert title == "Filme Sem Ano"

    def test_trailing_tag_stripped(self):
        title, year = parse_movie_name("Inception (2010) [HD]")
        assert year == 2010
        # Title should not contain [HD]
        assert "[HD]" not in title

    def test_year_1999(self):
        _, year = parse_movie_name("The Matrix (1999)")
        assert year == 1999


# ---------------------------------------------------------------------------
# canonical_strm_path
# ---------------------------------------------------------------------------

class TestCanonicalStrmPath(unittest.TestCase):
    ROOT = Path("D:/midias")

    def _entry(self, tvg_name, group_title, media_type, url=""):
        if not url:
            if media_type == "series":
                url = "https://host/series/u/p/1.mkv"
            else:
                url = "https://host/movie/u/p/1.mkv"
        return M3UEntry(url=url, tvg_name=tvg_name, group_title=group_title, media_type=media_type)

    def test_series_path(self):
        e = self._entry("1 Contra Todos S01 E01", "NETFLIX", "series")
        p = canonical_strm_path(self.ROOT, e)
        assert p is not None
        assert p.parts[-1] == "1 Contra Todos - S01E01.strm"
        assert "Season 01" in p.parts
        assert "Series" in p.parts

    def test_anime_series_path(self):
        e = self._entry("Digimon Frontier S01 E39", "ANIMES", "series")
        p = canonical_strm_path(self.ROOT, e)
        assert p is not None
        assert "Animações" in p.parts
        assert "Season 01" in p.parts
        assert p.name == "Digimon Frontier - S01E39.strm"

    def test_movie_path(self):
        e = self._entry("É Fada! (2016)", "FILMES: AVENTURA", "movie")
        p = canonical_strm_path(self.ROOT, e)
        assert p is not None
        assert "Filmes" in p.parts
        assert p.name.endswith(".strm")
        assert "2016" in str(p)

    def test_live_tv_skipped(self):
        e = self._entry("Canal 1", "PROGRAMAS DE TV", "live", url="https://host/user/pass/1")
        p = canonical_strm_path(self.ROOT, e)
        assert p is None

    def test_dorama_series(self):
        e = self._entry("My Love S01 E01", "DORAMAS", "series")
        p = canonical_strm_path(self.ROOT, e)
        assert p is not None
        assert "Doramas" in p.parts


# ---------------------------------------------------------------------------
# write_strm_if_new
# ---------------------------------------------------------------------------

class TestWriteStrmIfNew(unittest.TestCase):
    def test_creates_file(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sub" / "test.strm"
            result = write_strm_if_new(p, "https://example.com/stream.mkv")
            assert result is True
            assert p.exists()
            assert p.read_text().strip() == "https://example.com/stream.mkv"

    def test_skips_same_url(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test.strm"
            write_strm_if_new(p, "https://example.com/stream.mkv")
            result = write_strm_if_new(p, "https://example.com/stream.mkv")
            assert result is False

    def test_updates_different_url(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test.strm"
            write_strm_if_new(p, "https://example.com/old.mkv")
            result = write_strm_if_new(p, "https://example.com/new.mkv")
            assert result is True
            assert p.read_text().strip() == "https://example.com/new.mkv"


# ---------------------------------------------------------------------------
# parse_m3u (mocked HTTP)
# ---------------------------------------------------------------------------

SAMPLE_M3U = textwrap.dedent("""\
    #EXTM3U
    #EXTINF:-1 tvg-id="123" tvg-name="1 Contra Todos S01 E01" group-title="NETFLIX",1 Contra Todos S01 E01
    https://cn53.org:443/series/u/p/23911.mkv
    #EXTINF:-1 tvg-id="456" tvg-name="É Fada! (2016)" group-title="FILMES: INFANTIS",É Fada! (2016)
    https://cn53.org:443/movie/u/p/130730.mkv
    #EXTINF:-1 tvg-id="789" tvg-name="Canal Esportes" group-title="AO VIVO",Canal Esportes
    https://cn53.org:443/user/pass/999
""").encode("utf-8")


class TestParseM3U(unittest.TestCase):
    def test_empty_attribute_is_empty_string(self):
        attrs = _parse_extinf_attrs('#EXTINF:-1 tvg-id="" tvg-name="Movie" group-title="FILMES",Movie')
        assert attrs["tvg-id"] == ""

    @patch("urllib.request.urlopen")
    def test_yields_vod_only(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.__iter__ = lambda s: iter(SAMPLE_M3U.split(b"\n"))
        mock_urlopen.return_value = mock_resp

        entries = list(parse_m3u("http://fake/m3u"))
        # live entry should be excluded
        assert len(entries) == 2

    @patch("urllib.request.urlopen")
    def test_series_entry(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.__iter__ = lambda s: iter(SAMPLE_M3U.split(b"\n"))
        mock_urlopen.return_value = mock_resp

        entries = list(parse_m3u("http://fake/m3u"))
        series = [e for e in entries if e.media_type == "series"]
        assert len(series) == 1
        assert "Contra Todos" in series[0].tvg_name

    @patch("urllib.request.urlopen")
    def test_movie_entry(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.__iter__ = lambda s: iter(SAMPLE_M3U.split(b"\n"))
        mock_urlopen.return_value = mock_resp

        entries = list(parse_m3u("http://fake/m3u"))
        movies = [e for e in entries if e.media_type == "movie"]
        assert len(movies) == 1
        assert "Fada" in movies[0].tvg_name


# ---------------------------------------------------------------------------
# _normalise_for_match
# ---------------------------------------------------------------------------

class TestNormaliseForMatch(unittest.TestCase):
    def test_accent_removal(self):
        n = _normalise_for_match("É Fada!")
        assert "e" in n
        assert "fada" in n

    def test_case_insensitive(self):
        assert _normalise_for_match("DIGIMON FRONTIER") == _normalise_for_match("Digimon Frontier")


# ---------------------------------------------------------------------------
# _fix_recovery_items (mocked MongoDB)
# ---------------------------------------------------------------------------

class TestFixRecoveryItems(unittest.TestCase):
    def _make_mock_client(self, docs):
        mock_client = MagicMock()
        mock_db = MagicMock()
        mock_files = MagicMock()
        mock_client.__getitem__ = lambda s, k: mock_db
        mock_db.__getitem__ = lambda s, k: mock_files
        mock_files.find.return_value = docs
        mock_files.update_one = MagicMock()
        return mock_client, mock_files

    def test_fixes_matching_doc(self):
        import tempfile
        entries = [
            M3UEntry(
                url="https://host/series/u/p/350253.mkv",
                tvg_name="Digimon Frontier S01 E39",
                group_title="ANIMES",
                media_type="series",
            )
        ]
        docs = [{
            "_id": "docid1",
            "name": "Digimon Frontier - S01E39.mkv",
            "parent": "",
            "recovery_required": True,
            "failed_reason": "local_path_missing",
        }]

        mock_client, mock_files = self._make_mock_client(docs)

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td)
            # MongoClient is imported inside _fix_recovery_items via local import;
            # patch pymongo.MongoClient directly since the local import resolves there.
            with patch("tools.m3u_strm_generator.MongoClient", return_value=mock_client):
                fixed = _fix_recovery_items(entries, dest, "mongodb://localhost:27017", "ftp")

        assert fixed == 1
        mock_files.update_one.assert_called_once()
        call_args = mock_files.update_one.call_args
        assert call_args[0][1]["$set"]["recovery_required"] is False

    def test_no_match_returns_zero(self):
        import tempfile
        entries = [
            M3UEntry(
                url="https://host/series/u/p/999.mkv",
                tvg_name="Completely Different Show S01 E01",
                group_title="NETFLIX",
                media_type="series",
            )
        ]
        docs = [{
            "_id": "docid2",
            "name": "Digimon Frontier - S01E39.mkv",
            "parent": "",
            "recovery_required": True,
            "failed_reason": "local_path_missing",
        }]

        mock_client, mock_files = self._make_mock_client(docs)

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td)
            with patch("tools.m3u_strm_generator.MongoClient", return_value=mock_client):
                fixed = _fix_recovery_items(entries, dest, "mongodb://localhost:27017", "ftp")

        assert fixed == 0
        mock_files.update_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
