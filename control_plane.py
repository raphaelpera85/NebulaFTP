"""Authenticated local control plane for the MulletaFlix plugin."""

from __future__ import annotations

import asyncio
import hmac
import inspect
import re
import secrets
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from ftp.auth import hash_password
from ftp.pathio import MongoDBPathIO

StatusProvider = Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]]
DrainCallback = Callable[[], Awaitable[None]]
DisconnectUser = Callable[[str], Awaitable[None]]
LOGIN_RE = re.compile(r"^[a-zA-Z0-9_]{1,64}$")


class ConflictError(RuntimeError):
    pass


def validate_ftp_security(mode: str, certfile: str | None, keyfile: str | None, required: bool) -> bool:
    if mode not in {"ftp", "ftps-explicit", "ftps-implicit"}:
        raise ValueError(f"invalid FTP_SECURITY_MODE: {mode}")
    if bool(certfile) != bool(keyfile):
        raise ValueError("TLS_CERTFILE and TLS_KEYFILE must be configured together")
    tls_enabled = mode != "ftp"
    if tls_enabled and not certfile:
        raise ValueError(f"{mode} requires TLS_CERTFILE and TLS_KEYFILE")
    if not tls_enabled and (certfile or keyfile):
        raise ValueError("TLS files require an FTPS security mode")
    if required and not tls_enabled:
        raise ValueError("TLS_REQUIRED forbids plain FTP mode")
    return tls_enabled


def _resolve_allowed(path: str, roots: tuple[Path, ...]) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not roots or not any(resolved == root or root in resolved.parents for root in roots):
        raise ValueError(f"path is outside configured roots: {resolved}")
    return resolved


def _normalize_permissions(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("permissions must be an array")
    result = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("permission must be an object")
        path = PurePosixPath(str(item.get("path", "")))
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("permission path must be absolute and normalized")
        normalized = path.as_posix()
        if normalized in seen:
            raise ValueError(f"duplicate permission path: {normalized}")
        seen.add(normalized)
        writable = bool(item.get("writable", False))
        result.append(
            {
                "path": normalized,
                "readable": bool(item.get("readable", False)) or writable,
                "writable": writable,
            }
        )
    return result


class FeederSupervisor:
    def __init__(
        self,
        script: Path,
        *,
        source_roots: tuple[Path, ...],
        destination_roots: tuple[Path, ...],
        state_dir: Path,
        stop_timeout: int = 15,
    ) -> None:
        self._script = script.resolve()
        self._source_roots = source_roots
        self._destination_roots = destination_roots
        self._state_dir = state_dir.resolve()
        self._stop_timeout = max(1, stop_timeout)
        self._process: asyncio.subprocess.Process | None = None
        self._started_at: float | None = None
        self._lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        process = self._process
        running = bool(process and process.returncode is None)
        return {
            "running": running,
            "pid": process.pid if running else None,
            "exitCode": None if running or process is None else process.returncode,
            "startedAt": self._started_at,
        }

    async def start(self, config: dict[str, Any]) -> dict[str, Any]:  # noqa: PLR0912
        async with self._lock:
            if self._process and self._process.returncode is None:
                raise ConflictError("feeder is already running")
            sources_raw = config.get("sources")
            if not isinstance(sources_raw, list) or not sources_raw:
                raise ValueError("sources must be a non-empty array")
            sources = [_resolve_allowed(str(path), self._source_roots) for path in sources_raw]
            if any(not path.is_dir() for path in sources):
                raise ValueError("every source must be an existing directory")

            transport = str(config.get("transport", "direct-mongo"))
            if transport not in {"direct-mongo", "ftp-copy"}:
                raise ValueError("transport must be direct-mongo or ftp-copy")
            run_mode = str(config.get("runMode", "once"))
            if run_mode not in {"once", "watch"}:
                raise ValueError("runMode must be once or watch")
            destination = str(config.get("destination", ".nebula_virtual_root"))
            if transport == "ftp-copy":
                destination = str(_resolve_allowed(destination, self._destination_roots))

            workers = min(max(int(config.get("workers", 2)), 1), 16)
            max_active = min(max(int(config.get("maxActive", 20)), 1), 1000)
            poll_seconds = min(max(int(config.get("pollSeconds", 60)), 1), 3600)
            retries = min(max(int(config.get("retries", 3)), 0), 20)
            exclude = config.get("excludeDirectories") or []
            if not isinstance(exclude, list):
                raise ValueError("excludeDirectories must be an array")

            self._state_dir.mkdir(parents=True, exist_ok=True)
            argv = [
                sys.executable,
                str(self._script),
                "--dest",
                destination,
                "--workers",
                str(workers),
                "--max-active",
                str(max_active),
                "--poll-seconds",
                str(poll_seconds),
                "--retries",
                str(retries),
                "--state-file",
                str(self._state_dir / "feeder.json"),
            ]
            for source in sources:
                argv.extend(["--source", str(source)])
            for name in exclude:
                if not name or Path(str(name)).name != str(name):
                    raise ValueError("excludeDirectories accepts names only")
                argv.extend(["--exclude-dir", str(name)])
            if transport == "direct-mongo":
                argv.append("--direct-mongo")
            if run_mode == "watch":
                argv.append("--watch")
            if bool(config.get("overwrite", False)):
                argv.append("--overwrite")
            if bool(config.get("allFiles", False)):
                argv.append("--all-files")
            if config.get("sourcePolicy", "preserve") == "delete-after-success":
                argv.append("--delete-source")
            elif config.get("sourcePolicy", "preserve") != "preserve":
                raise ValueError("sourcePolicy must be preserve or delete-after-success")

            self._process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self._script.parent.parent),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._started_at = time.time()
            return self.status()

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            process = self._process
            if not process or process.returncode is not None:
                return self.status()
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=self._stop_timeout)
            except TimeoutError:
                process.kill()
                await process.wait()
            return self.status()


class ControlPlane:
    def __init__(
        self,
        *,
        token: str,
        mongo: Any,
        upload_queue: asyncio.Queue,
        drain_callback: DrainCallback,
        status_provider: StatusProvider,
        feeder: FeederSupervisor | None = None,
        mongo_uri: str = "",
        database: str = "ftp",
        source_roots: tuple[Path, ...] = (),
        output_roots: tuple[Path, ...] = (),
        disconnect_user: DisconnectUser | None = None,
        prune_ttl: int = 300,
    ) -> None:
        if len(token) < 32:
            raise ValueError("CONTROL_TOKEN must contain at least 32 characters")
        self._token = token
        self._mongo = mongo
        self._upload_queue = upload_queue
        self._drain_callback = drain_callback
        self._status_provider = status_provider
        self._feeder = feeder
        self._mongo_uri = mongo_uri
        self._database = database
        self._source_roots = source_roots
        self._output_roots = output_roots
        self._disconnect_user = disconnect_user
        self._prune_ttl = max(30, prune_ttl)
        self._previews: dict[str, dict[str, Any]] = {}
        self._strm_status: dict[str, Any] = {"running": False, "lastResult": None}
        self._strm_lock = asyncio.Lock()
        self._started_at = time.monotonic()
        self._ready = False
        self._draining = False
        self._drain_task: asyncio.Task[None] | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.bound_port: int | None = None

        app = web.Application(
            middlewares=[self._errors, self._authenticate],
            client_max_size=256 * 1024,
        )
        app.add_routes(
            [
                web.get("/v1/health", self._health),
                web.get("/v1/readiness", self._readiness),
                web.get("/v1/status", self._status),
                web.get("/v1/queue", self._queue),
                web.get("/v1/feeder/status", self._feeder_status),
                web.post("/v1/feeder/start", self._feeder_start),
                web.post("/v1/feeder/stop", self._feeder_stop),
                web.get("/v1/strm/status", self._get_strm_status),
                web.post("/v1/strm/generate", self._generate_strm),
                web.post("/v1/prune/preview", self._prune_preview),
                web.post("/v1/prune/apply", self._prune_apply),
                web.get("/v1/cache", self._cache_status),
                web.post("/v1/cache/clear", self._cache_clear),
                web.get("/v1/users", self._users),
                web.post("/v1/users/sync", self._users_sync),
                web.post("/v1/drain-stop", self._drain_stop),
            ]
        )
        self._app = app

    @web.middleware
    async def _errors(self, request, handler):
        try:
            return await handler(request)
        except ConflictError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except web.HTTPException:
            raise
        except Exception:
            return web.json_response({"error": "internal_error"}, status=500)

    @web.middleware
    async def _authenticate(self, request, handler):
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {self._token}"):
            return web.json_response(
                {"error": "unauthorized"},
                status=401,
                headers={"Cache-Control": "no-store", "WWW-Authenticate": "Bearer"},
            )
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def start(self, host: str = "127.0.0.1", port: int = 2130) -> None:
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()
        server = self._site._server
        if server and server.sockets:
            self.bound_port = int(server.sockets[0].getsockname()[1])

    def set_ready(self, ready: bool) -> None:
        self._ready = ready

    @property
    def draining(self) -> bool:
        return self._draining

    async def close(self) -> None:
        self._ready = False
        if self._feeder:
            await self._feeder.stop()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def _json(self, request):
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return value

    async def _health(self, _request):
        return web.json_response(
            {"api": "v1", "status": "alive", "uptime_seconds": int(time.monotonic() - self._started_at)}
        )

    async def _readiness(self, _request):
        try:
            database_ready = bool((await self._mongo.command("ping")).get("ok"))
        except Exception:
            database_ready = False
        ready = self._ready and not self._draining and database_ready
        return web.json_response(
            {"api": "v1", "ready": ready, "draining": self._draining, "database_ready": database_ready},
            status=200 if ready else 503,
        )

    async def _status(self, _request):
        status = self._status_provider()
        if inspect.isawaitable(status):
            status = await status
        return web.json_response(
            {"api": "v1", "ready": self._ready and not self._draining, "draining": self._draining, **status}
        )

    async def queue_summary(self) -> dict[str, int]:
        states = ("queued", "staging", "uploading", "completed", "failed")
        counts = {
            state: int(await self._mongo.files.count_documents({"type": "file", "status": state}))
            for state in states
        }
        counts["pending"] = counts["queued"] + counts["staging"] + counts["uploading"]
        counts["in_memory"] = self._upload_queue.qsize()
        return counts

    async def _queue(self, _request):
        return web.json_response({"api": "v1", **await self.queue_summary()})

    def _require_feeder(self) -> FeederSupervisor:
        if not self._feeder:
            raise ConflictError("feeder is not configured")
        return self._feeder

    async def _feeder_status(self, _request):
        return web.json_response({"api": "v1", **self._require_feeder().status()})

    async def _feeder_start(self, request):
        return web.json_response(
            {"api": "v1", **await self._require_feeder().start(await self._json(request))},
            status=202,
        )

    async def _feeder_stop(self, _request):
        return web.json_response({"api": "v1", **await self._require_feeder().stop()})

    async def _get_strm_status(self, _request):
        return web.json_response({"api": "v1", **self._strm_status})

    async def _generate_strm(self, request):
        if self._strm_lock.locked():
            raise ConflictError("STRM generation is already running")
        body = await self._json(request)
        output = _resolve_allowed(str(body.get("outputRoot", "")), self._output_roots)
        base_url = str(body.get("streamBaseUrl", "")).rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("streamBaseUrl must be an HTTP(S) URL without credentials")
        library_user = str(body.get("libraryUser", "")).strip()
        if not LOGIN_RE.fullmatch(library_user):
            raise ValueError("invalid libraryUser")

        async with self._strm_lock:
            self._strm_status = {"running": True, "lastResult": self._strm_status.get("lastResult")}
            try:
                from generate_strm import generate_strm_files  # noqa: PLC0415

                result = await asyncio.to_thread(
                    generate_strm_files,
                    mongo_url=self._mongo_uri,
                    database=self._database,
                    output_root=output,
                    stream_base_url=base_url,
                    library_user=library_user,
                    prune=False,
                )
                self._strm_status = {"running": False, "lastResult": result}
                return web.json_response({"api": "v1", **result})
            except Exception:
                self._strm_status = {"running": False, "lastResult": {"status": "failed"}}
                raise

    def _prune_config(self, body):
        raw_sources = body.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise ValueError("sources must be a non-empty array")
        sources = [_resolve_allowed(str(path), self._source_roots) for path in raw_sources]
        destination = _resolve_allowed(str(body.get("destination", sources[0])), self._output_roots)
        exclude = body.get("excludeDirectories") or []
        if not isinstance(exclude, list):
            raise ValueError("excludeDirectories must be an array")
        return {
            "sources": sources,
            "destination": destination,
            "exclude": {str(x).casefold() for x in exclude},
        }

    async def _run_prune(self, config, apply):
        from tools.feed_ftp import prune_completed_strm  # noqa: PLC0415

        return await asyncio.to_thread(
            prune_completed_strm,
            config["sources"],
            self._mongo_uri,
            apply,
            config["exclude"],
            config["destination"],
        )

    async def _prune_preview(self, request):
        config = self._prune_config(await self._json(request))
        result = await self._run_prune(config, False)
        preview_id = secrets.token_urlsafe(32)
        expires_at = time.time() + self._prune_ttl
        self._previews[preview_id] = {"expires": expires_at, "config": config, "result": result}
        return web.json_response(
            {"api": "v1", "previewId": preview_id, "expiresAt": expires_at, **result}
        )

    async def _prune_apply(self, request):
        preview_id = str((await self._json(request)).get("previewId", ""))
        preview = self._previews.pop(preview_id, None)
        if not preview or preview["expires"] < time.time():
            raise ValueError("previewId is invalid or expired")
        result = await self._run_prune(preview["config"], True)
        return web.json_response({"api": "v1", **result})

    async def _cache_status(self, _request):
        async with MongoDBPathIO._cache_lock:
            entries = len(MongoDBPathIO._memory_cache)
            maximum = MongoDBPathIO._memory_cache.maxsize
        return web.json_response({"api": "v1", "entries": entries, "maximum": maximum})

    async def _cache_clear(self, _request):
        async with MongoDBPathIO._cache_lock:
            removed = len(MongoDBPathIO._memory_cache)
            MongoDBPathIO._memory_cache.clear()
        return web.json_response({"api": "v1", "removed": removed})

    async def _users(self, _request):
        users = []
        async for user in self._mongo.users.find({}, {"login": 1, "permissions": 1, "_id": 0}):
            users.append({"login": user["login"], "permissions": user.get("permissions", [])})
        users.sort(key=lambda item: item["login"].casefold())
        return web.json_response({"api": "v1", "users": users})

    async def _users_sync(self, request):
        body = await self._json(request)
        raw_users = body.get("users")
        if not isinstance(raw_users, list):
            raise ValueError("users must be an array")
        changed = []
        seen = set()
        created = updated = 0
        for raw in raw_users:
            if not isinstance(raw, dict):
                raise ValueError("user must be an object")
            login = str(raw.get("login", ""))
            if not LOGIN_RE.fullmatch(login) or login in seen:
                raise ValueError(f"invalid or duplicate login: {login}")
            seen.add(login)
            existing = await self._mongo.users.find_one({"login": login})
            password = raw.get("password")
            if not existing and not isinstance(password, str):
                raise ValueError(f"password is required for new user: {login}")
            permissions = raw.get(
                "permissions",
                existing.get("permissions", []) if existing else [],
            )
            update: dict[str, Any] = {"permissions": _normalize_permissions(permissions)}
            unset = {}
            if password is not None:
                if not isinstance(password, str) or not password:
                    raise ValueError(f"password must be non-empty for user: {login}")
                update["password_hash"] = await asyncio.to_thread(hash_password, password)
                unset["password"] = ""
            await self._mongo.users.update_one(
                {"login": login},
                {"$set": update, **({"$unset": unset} if unset else {})},
                upsert=True,
            )
            created += int(existing is None)
            updated += int(existing is not None)
            changed.append(login)

        deleted = []
        if bool(body.get("replaceAll", False)):
            async for row in self._mongo.users.find({"login": {"$nin": list(seen)}}, {"login": 1}):
                deleted.append(row["login"])
            if deleted:
                await self._mongo.users.delete_many({"login": {"$in": deleted}})
        if self._disconnect_user:
            for login in [*changed, *deleted]:
                await self._disconnect_user(login)
        return web.json_response(
            {"api": "v1", "created": created, "updated": updated, "deleted": len(deleted)}
        )

    async def _drain_stop(self, _request):
        if not self._draining:
            self._draining = True
            self._ready = False

            async def drain():
                if self._feeder:
                    await self._feeder.stop()
                await self._drain_callback()

            self._drain_task = asyncio.create_task(drain())
        return web.json_response({"api": "v1", "accepted": True, "draining": True}, status=202)
