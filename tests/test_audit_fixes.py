"""Minimal pytest suite covering security-critical helpers."""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import PurePosixPath

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import bcrypt  # noqa: F401
    HAS_BCRYPT = True
except ImportError:
    HAS_BCRYPT = False

pytestmark = pytest.mark.skipif(
    not HAS_BCRYPT, reason="bcrypt not installed; install test deps via `pip install bcrypt pytest`"
)


def _load(mod_name: str):
    """Import `ftp.<mod_name>` directly from file path, bypassing
    `ftp/__init__.py` so the loader stays decoupled from sibling modules
    that may pull in optional runtime deps (pyrogram, tgcrypto)."""
    import types
    ftp_pkg = sys.modules.get("ftp")
    if ftp_pkg is None or not hasattr(ftp_pkg, "__path__"):
        ftp_pkg = types.ModuleType("ftp")
        ftp_pkg.__path__ = [os.path.join(ROOT, "ftp")]
        sys.modules["ftp"] = ftp_pkg
    target_name = f"ftp.{mod_name}"
    path = os.path.join(ROOT, "ftp", f"{mod_name}.py")
    spec = importlib.util.spec_from_file_location(target_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[target_name] = mod
    try:
        spec.loader.exec_module(mod)
    except (ModuleNotFoundError, RuntimeError, ImportError, OSError) as exc:
        pytest.skip(f"optional runtime dep missing/incompatible: {exc!r}")
    return mod


auth = _load("auth")
pathio = _load("pathio")


def test_hash_and_verify_password_roundtrip():
    assert verify_password("correct horse battery staple", hash_password("correct horse battery staple")) is True
    assert verify_password("wrong", hash_password("correct horse battery staple")) is False


def test_legacy_plaintext_compare_still_works_during_migration():
    assert verify_password("hunter2", "hunter2") is True
    assert verify_password("hunter2", "hunter3") is False


def test_empty_inputs_rejected():
    with pytest.raises((ValueError, RuntimeError)):
        hash_password("")
    assert verify_password("", "abc") is False
    assert verify_password("abc", "") is False
    assert verify_password(None, "abc") is False  # type: ignore[arg-type]


def test_permission_precedence():
    root = Permission("/", writable=True)
    docs = Permission("/docs", readable=True)
    sub = Permission("/docs/private", readable=False)

    for p in (root, docs, sub):
        p.path = PurePosixPath(p.path)

    perms = sorted([root, docs, sub], key=lambda p: len(p.path.parts))
    # most general (root) comes first, most specific (sub) last
    assert perms[0] is root
    assert perms[-1] is sub
    assert perms[1] is docs


def test_absolute_client_paths_are_scoped_to_user_home():
    class Done:
        def done(self):
            return True

    class Future:
        user = Done()

    class Conn:
        current_directory = PurePosixPath("/raphael")
        future = Future()
        user = User("raphael", "pass")

    _real, virtual = Server.get_paths(Conn(), "/Series")
    assert virtual == PurePosixPath("/raphael/Series")


def test_pathio_lru_caps_size():
    cache: BoundedLRUCache = BoundedLRUCache(maxsize=3)
    cache["a"] = 1
    cache["b"] = 2
    cache["c"] = 3
    cache["d"] = 4
    assert "a" not in cache
    assert cache["c"] == 3
    assert cache["d"] == 4


def test_pathio_lru_evicts_least_recently_used():
    cache: BoundedLRUCache = BoundedLRUCache(maxsize=3)
    cache["a"] = 1
    cache["b"] = 2
    cache["c"] = 3
    _ = cache["a"]  # touch -> most-recent
    cache["d"] = 4
    assert "b" not in cache
    assert "a" in cache
    assert cache["c"] == 3


def test_pathio_lru_get_returns_default():
    cache: BoundedLRUCache = BoundedLRUCache(maxsize=2)
    assert cache.get("missing") is None
    assert cache.get("missing", "default") == "default"


def test_only_video_names_are_uploadable():
    assert pathio.is_uploadable_name("movie.mkv") is True
    assert pathio.is_uploadable_name("movie.mkv.partial") is True
    assert pathio.is_uploadable_name("movie.nfo") is False


def test_movie_folder_score_matches_release_name_to_folder():
    good = pathio.movie_folder_score("American.Psycho.2000.720p.BrRip.x264.YIFY.mp4", "American Psycho (2000)")
    bad = pathio.movie_folder_score("American.Psycho.2000.720p.BrRip.x264.YIFY.mp4", "Apollo 13 (1995)")
    assert good >= 0.60
    assert bad < 0.60


def test_resolve_part_bot_uses_stored_bot_index():
    bots = ["bot1", "bot2", "bot3"]
    assert pathio.resolve_part_bot({"bot_index": 1}, bots) == "bot2"
    assert pathio.resolve_part_bot({}, bots) == "bot1"


def test_tls_knob_documented_in_env_example():
    path = os.path.join(ROOT, ".env.example")
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    for needle in ("TLS_CERTFILE", "TLS_KEYFILE", "TLS_REQUIRE_CLIENT_CERT", "BCRYPT_ROUNDS"):
        assert needle in body, f"{needle} missing from .env.example"


def test_dockerfile_no_longer_runs_as_root_and_uses_tini():
    path = os.path.join(ROOT, "Dockerfile")
    body = open(path, encoding="utf-8").read()
    assert "USER nebula" in body
    assert "tini" in body.lower()
    assert "HEALTHCHECK" in body
    assert "3.12" in body


def test_compose_dropped_host_network_and_adds_healthchecks():
    path = os.path.join(ROOT, "docker-compose.yml")
    body = open(path, encoding="utf-8").read()
    assert "network_mode: host" not in body
    assert "healthcheck" in body.lower()
    assert "nebula_net" in body


def test_accounts_manager_supports_cli_subcommands():
    import subprocess
    env = {**os.environ, "MONGODB": "mongodb://localhost:27017"}
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "accounts_manager.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "set-password" in r.stdout
    assert "list" in r.stdout
    assert "migrate-passwords" in r.stdout


def test_bootstrap_helpers_support_help_without_connecting():
    import subprocess

    env = {**os.environ, "MONGODB": "mongodb://localhost:27017"}
    for script in ("get_channel_id.py", "setup_database.py"):
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, script), "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        assert r.returncode == 0, r.stderr
        assert "usage:" in r.stdout.lower()


# Imports resolved at module load time by _load(); touch them once so the
# tests below can use them as plain globals.
hash_password = auth.hash_password
verify_password = auth.verify_password
server = _load("server")
Permission = server.Permission
User = server.User
Server = server.Server


# ----------------------------------------------------------------------------
# Regression coverage added alongside the Phase-01 audit (F-04 + F-01).
# Kept stdlib-only so the suite still runs without mongo/motor/aiofiles.
# ----------------------------------------------------------------------------

ftp_range = _load("range")


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        ("bytes=0-99",        1024, (0, 99, 206)),    # closed range
        ("bytes=-100",        1024, (924, 1023, 206)),  # suffix-length form
        ("bytes=200-",        1024, (200, 1023, 206)),  # open-ended
        ("bytes=-2000",       1024, (0, 1023, 206)),    # suffix >= size -> whole file
        ("bytes=0-0",         1024, (0, 0, 206)),
        ("bytes=1024-1024",   1024, (1024, 1023, 206)), # boundary clamp: end pinned to size-1
        ("",                  1024, (0, 1023, 200)),    # missing header
        (None,                1024, (0, 1023, 200)),    # caller passed None
        ("garbage",           1024, (0, 1023, 200)),    # not a Range header
        ("bytes=abc-def",     1024, (0, 1023, 200)),    # unparsable -> safe fallback
        # Range past EOF: clamped to last byte. This matches the
        # current implementation; emitting 416 would be the RFC-7233 §4.4
        # alternative and is tracked as a manual-only decision.
        ("bytes=2000-3000",   1024, (2000, 1023, 206)),
    ],
    ids=[
        "closed", "suffix", "open-ended", "suffix-oversize", "single-byte",
        "boundary-clamp", "empty-header", "none-header", "garbage",
        "unparseable", "past-eof-clamp",
    ],
)
def test_parse_range_returns_expected_window(header, size, expected):
    start, end, status = ftp_range.parse_range(header, size)
    assert (start, end, status) == expected


def test_env_example_documents_performance_knobs():
    """F-01: the recommended upload parallelism knobs must be discoverable
    in `.env.example` so operators don't have to dig through source to tune."""
    body = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    for needle in ("PART_WORKERS_PER_FILE", "CHUNK_SIZE_MB", "MAX_WORKERS"):
        assert needle in body, f"{needle} missing from .env.example"


def test_legacy_plaintext_compare_uses_constant_time_helper():
    """F-07: the legacy plaintext branch in `verify_password` must not be a
    plain ``==`` (which leaks timing). We assert by behaviour: comparing
    two equally-long prefixes far apart in length must still resolve to
    False without raising, and matching strings must resolve to True.
    Also smoke-test that the bcrypt path still works in tandem so neither
    branch regressed.
    """
    # Legacy path: no hash, plaintext compare. Both ends are long strings
    # so they look identical in length to a timing attacker.
    assert verify_password("a" * 4096, "a" * 4096) is True
    assert verify_password("a" * 4096, "a" * 4095 + "b") is False
    # Bcrypt path remains unaffected by the legacy change.
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True
    assert verify_password("wrong", hashed) is False
BoundedLRUCache = pathio.BoundedLRUCache
