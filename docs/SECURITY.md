# NebulaFTP — Security & Audit Notes

This document tracks the 2026-07-06 audit (`/gsd-debug system-audit`) and
what was changed.

## Audit Outcome

| Severity | Issue | Action |
|----------|-------|--------|
| CRITICAL | FTP ran over plaintext (no FTPS) | TLS now wired through `TLS_CERTFILE`/`TLS_KEYFILE` env; `Server.set_ssl_context()`. |
| CRITICAL | Passwords stored in plaintext in Mongo | Switched to `bcrypt` with hash-prefix detection in `ftp/auth.py`; auto-migration on next successful login. |
| CRITICAL | `network_mode: host` on compose | Bridge network `nebula_net`, Mongo bound to `127.0.0.1`. |
| HIGH | 4 files obfuscated with `exec(zlib.decompress(base64.b64decode(...)))` | All de-obfuscated. Decoded payloads verified safe (no `requests`/`subprocess`/`eval`). |
| HIGH | `requirements.txt` unpinned | Pinned upper bounds; `requirements.in` introduced for `pip-compile`. |
| HIGH | Dockerfile ran as root, EOL base image | Multi-stage build, `python:3.12-slim`, non-root user `nebula`, `tini` as PID-1, `HEALTHCHECK`. |
| MEDIUM | 14+ bare `except: pass` swallows errors | Narrowed to specific exceptions; logger added. |
| MEDIUM | `_memory_cache` unbounded → memory leak | Replaced with bounded `BoundedLRUCache` (10 000 entries default). |
| MEDIUM | `accounts_manager.py` interactive-only | Added argparse subcommands: `list`, `add`, `set-password`, `delete`, `migrate-passwords`. |
| MEDIUM | `FTP_Bot.session` mounted without 0600 | Replaced bind-mount with named volume `nebula_session`. |
| MEDIUM | GC ran every 600 s | Configurable `GC_INTERVAL_SECONDS` default 60. |
| MEDIUM | `MongoDBUserManager.users` grew forever | `notify_logout` now evicts. |
| LOW | README pointed to non-existent `CONTRIBUTING.md`, `README-en.md`, screenshots | Dropped badges; added stub `SECURITY.md` (this file). |
| LOW | `.gitignore` incomplete | Expanded. |
| LOW | No tests | Added `tests/test_audit_fixes.py` covering bcrypt, LRU, TLS env, Dockerfile, ACLs. |
| LOW | No CI / lint | `pyproject.toml` (ruff + mypy), GitHub Actions matrix on 3.10/3.11/3.12. |
| LOW | Useless `async for dialog in bot.get_dialogs(limit=50): pass` | Removed (no functional effect). |
| LOW | Obfuscation obscured provenance | Source de-obfuscated; this `SECURITY.md` records what was changed. |

## How to enable FTPS

```bash
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout nebula.key -out nebula.crt -days 365 \
  -subj "/CN=your.host"

chmod 0600 nebula.key

# .env
TLS_CERTFILE=/app/nebula.crt
TLS_KEYFILE=/app/nebula.key
```

Restart `docker compose up -d`. Clients should connect with `ftps://...`
(FileZilla: encryption = "Explicit FTP over TLS").

## How to migrate legacy plaintext passwords

1. After upgrade, on the next successful login per user the server
   re-hashes the legacy password in the background. No action required
   for end users.
2. Admins can audit remaining legacy rows with:

   ```bash
   python accounts_manager.py migrate-passwords
   ```

   and force a reset with:

   ```bash
   python accounts_manager.py set-password --login alice --password 'new-secret'
   ```

## Reporting regressions

If you find a security regression, please open a private issue at
<https://github.com/samucamg/NebulaFTP/issues> and tag `security`.
Do **not** include credentials or session files in the report.
