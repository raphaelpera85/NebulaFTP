"""Centralized password hashing helpers for NebulaFTP.

Keeps `bcrypt` as the single optional dependency for the FTP server.
Falls back to a clear-text-only comparison if bcrypt is unavailable so the
server can still boot in legacy dev environments, but logs a loud warning —
see `verify_password` below.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

logger = logging.getLogger("NebulaFTP")

try:
    import bcrypt
    _HAVE_BCRYPT = True
except ImportError:
    bcrypt = None  # type: ignore[assignment]
    _HAVE_BCRYPT = False

_BCRYPT_ROUNDS = int(os.environ.get("BCRYPT_ROUNDS", "12"))


def hash_password(plain: str) -> str:
    """Hash a plaintext password. Raises if bcrypt is unavailable or password is empty."""
    if not plain:
        raise ValueError("empty password")
    if not _HAVE_BCRYPT:
        raise RuntimeError(
            "bcrypt is required for secure password hashing. Install with: pip install bcrypt"
        )
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("ascii")


def is_hashed(stored: Optional[str]) -> bool:
    return bool(stored) and stored.startswith(("$2a$", "$2b$", "$2y$"))


def verify_password(plain: str, stored: Optional[str]) -> bool:
    """Constant-time bcrypt verification. Accepts legacy plaintext stored=plain."""
    if not plain or not stored:
        return False
    if is_hashed(stored):
        if not _HAVE_BCRYPT:
            logger.error("bcrypt missing — cannot verify hashed password")
            return False
        try:
            return bcrypt.checkpw(plain.encode("utf-8"), stored.encode("ascii"))
        except (ValueError, TypeError):
            logger.warning("Malformed password hash rejected", exc_info=False)
            return False
    # Legacy plaintext path (migration window). Use a constant-time
    # comparison so a timing oracle cannot leak the stored password.
    return hmac.compare_digest(plain.encode("utf-8"), stored.encode("utf-8"))
