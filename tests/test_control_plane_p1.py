from __future__ import annotations

import asyncio
import json
import re
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession

import generate_strm
from control_plane import ControlPlane, FeederSupervisor
from ftp.pathio import MongoDBPathIO
from tools import feed_ftp

TOKEN = "control-plane-p1-test-token-32-chars"  # noqa: S105


class Cursor:
    def __init__(self, rows):
        self._rows = iter(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._rows)
        except StopIteration:
            raise StopAsyncIteration from None


class Files:
    def __init__(self):
        self.deleted_many = []

    async def count_documents(self, query):
        return 0

    async def delete_one(self, query):
        return None

    async def delete_many(self, query):
        self.deleted_many.append(query)


class Users:
    def __init__(self):
        self.rows = {}

    async def find_one(self, query):
        return self.rows.get(query["login"])

    def find(self, query, _projection):
        rows = list(self.rows.values())
        if "login" in query and "$nin" in query["login"]:
            rows = [row for row in rows if row["login"] not in query["login"]["$nin"]]
        return Cursor([dict(row) for row in rows])

    async def update_one(self, query, update, upsert=False):
        row = self.rows.setdefault(query["login"], {"login": query["login"]})
        row.update(update.get("$set", {}))
        for key in update.get("$unset", {}):
            row.pop(key, None)

    async def delete_many(self, query):
        for login in query["login"]["$in"]:
            self.rows.pop(login, None)


class Mongo:
    def __init__(self):
        self.files = Files()
        self.users = Users()

    async def command(self, name):
        return {"ok": int(name == "ping")}


class Feeder:
    def __init__(self):
        self.running = False
        self.stops = 0

    def status(self):
        return {"running": self.running, "pid": 123 if self.running else None, "exitCode": None}

    async def start(self, config):
        assert config["runMode"] == "watch"
        self.running = True
        return self.status()

    async def stop(self):
        self.running = False
        self.stops += 1
        return self.status()


class FakeProcess:
    pid = 42
    returncode = None

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_feeder_supervisor_uses_argv_and_rejects_outside_roots(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    supervisor = FeederSupervisor(
        tmp_path / "tools" / "feed_ftp.py",
        source_roots=(tmp_path.resolve(),),
        destination_roots=(tmp_path.resolve(),),
        state_dir=tmp_path / "state",
    )

    await supervisor.start(
        {
            "sources": [str(source)],
            "transport": "direct-mongo",
            "excludeDirectories": ["safe-name"],
        }
    )

    assert captured["argv"][captured["argv"].index("--source") + 1] == str(source.resolve())
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL
    await supervisor.stop()

    with pytest.raises(ValueError, match="outside configured roots"):
        await supervisor.start({"sources": [str(tmp_path.parent)]})


@pytest.mark.asyncio
async def test_rmdir_does_not_match_prefix_sibling():
    files = Files()
    pathio = MongoDBPathIO()
    pathio.db = SimpleNamespace(files=files)
    pathio.cwd = PurePosixPath("/")

    await pathio.rmdir(PurePosixPath("/Foo"))

    pattern = files.deleted_many[0]["parent"]["$regex"]
    assert re.match(pattern, "/Foo/Child")
    assert not re.match(pattern, "/FooBar/Child")


@pytest.mark.asyncio
async def test_canonical_p1_routes(monkeypatch, tmp_path):  # noqa: PLR0915
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    mongo = Mongo()
    feeder = Feeder()
    disconnected = []
    drained = asyncio.Event()
    prune_calls = []

    def fake_generate(**kwargs):
        assert kwargs["output_root"] == output.resolve()
        return {"generated": 2, "removed": 0, "output": str(output)}

    def fake_prune(sources, _mongo_uri, apply, _exclude, destination):
        prune_calls.append((apply, sources, destination))
        return {"scanned": 2, "matched": 1, "folders": int(apply), "files": 0}

    monkeypatch.setattr(generate_strm, "generate_strm_files", fake_generate)
    monkeypatch.setattr(feed_ftp, "prune_completed_strm", fake_prune)
    plane = ControlPlane(
        token=TOKEN,
        mongo=mongo,
        upload_queue=asyncio.Queue(),
        drain_callback=lambda: _set_event(drained),
        status_provider=dict,
        feeder=feeder,
        mongo_uri="mongodb://unused",
        source_roots=(source.resolve(),),
        output_roots=(output.resolve(),),
        disconnect_user=lambda login: _append(disconnected, login),
    )
    await plane.start(port=0)
    plane.set_ready(True)
    base = f"http://127.0.0.1:{plane.bound_port}"
    headers = {"Authorization": f"Bearer {TOKEN}"}

    try:
        async with ClientSession(headers=headers) as client:
            response = await client.post(
                f"{base}/v1/feeder/start",
                json={"runMode": "watch"},
            )
            assert response.status == 202
            assert (await response.json())["running"] is True

            response = await client.post(
                f"{base}/v1/strm/generate",
                json={
                    "outputRoot": str(output),
                    "streamBaseUrl": "http://127.0.0.1:2122",
                    "libraryUser": "raphael",
                },
            )
            assert (await response.json())["generated"] == 2

            response = await client.post(f"{base}/v1/prune/apply", json={})
            assert response.status == 400

            response = await client.post(
                f"{base}/v1/prune/preview",
                json={"sources": [str(source)], "destination": str(output)},
            )
            preview = await response.json()
            assert preview["matched"] == 1
            response = await client.post(
                f"{base}/v1/prune/apply",
                json={"previewId": preview["previewId"]},
            )
            assert (await response.json())["folders"] == 1
            response = await client.post(
                f"{base}/v1/prune/apply",
                json={"previewId": preview["previewId"]},
            )
            assert response.status == 400
            assert [call[0] for call in prune_calls] == [False, True]

            response = await client.post(
                f"{base}/v1/users/sync",
                json={
                    "users": [
                        {
                            "login": "raphael",
                            "password": "new-password",
                            "permissions": [{"path": "/Media", "readable": True}],
                        }
                    ],
                    "replaceAll": True,
                },
            )
            assert (await response.json())["created"] == 1
            assert "password" not in mongo.users.rows["raphael"]
            assert mongo.users.rows["raphael"]["password_hash"].startswith("$2")

            response = await client.post(
                f"{base}/v1/users/sync",
                json={"users": [{"login": "raphael", "password": "another-password"}]},
            )
            assert (await response.json())["updated"] == 1

            response = await client.get(f"{base}/v1/users")
            raw = await response.text()
            body = json.loads(raw)
            assert body["users"] == [
                {
                    "login": "raphael",
                    "permissions": [{"path": "/Media", "readable": True, "writable": False}],
                }
            ]
            assert "password_hash" not in raw

            MongoDBPathIO._memory_cache["test"] = {"name": "cached"}
            response = await client.post(f"{base}/v1/cache/clear")
            assert (await response.json())["removed"] >= 1

            response = await client.post(f"{base}/v1/drain-stop")
            assert response.status == 202
            await asyncio.wait_for(drained.wait(), timeout=1)
            assert feeder.stops >= 1
            assert disconnected == ["raphael", "raphael"]
    finally:
        await plane.close()


async def _set_event(event):
    event.set()


async def _append(target, value):
    target.append(value)
