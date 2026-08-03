from __future__ import annotations

import asyncio

import pytest
from aiohttp import ClientSession

from control_plane import ControlPlane, validate_ftp_security
from ftp import server as ftp_server

TOKEN = "test-control-token-with-at-least-32-chars"  # noqa: S105


class Files:
    async def count_documents(self, query):
        return {
            "queued": 2,
            "staging": 1,
            "uploading": 1,
            "completed": 7,
            "failed": 3,
        }[query["status"]]


class Mongo:
    files = Files()

    async def command(self, name):
        assert name == "ping"
        return {"ok": 1}


async def _set_event(event):
    event.set()


@pytest.mark.asyncio
async def test_control_plane_auth_readiness_queue_and_drain():
    drained = asyncio.Event()
    queue = asyncio.Queue()
    await queue.put("work")
    plane = ControlPlane(
        token=TOKEN,
        mongo=Mongo(),
        upload_queue=queue,
        drain_callback=lambda: _set_event(drained),
        status_provider=lambda: {"mode": "full", "ftp_connections": 0},
    )
    await plane.start(port=0)
    plane.set_ready(True)
    base = f"http://127.0.0.1:{plane.bound_port}"
    headers = {"Authorization": f"Bearer {TOKEN}"}

    try:
        async with ClientSession() as client:
            async with client.get(f"{base}/v1/health") as response:
                assert response.status == 401
                assert "Access-Control-Allow-Origin" not in response.headers

            async with client.get(f"{base}/v1/readiness", headers=headers) as response:
                assert response.status == 200
                assert (await response.json())["ready"] is True

            async with client.get(f"{base}/v1/queue", headers=headers) as response:
                body = await response.json()
                assert response.status == 200
                assert body["pending"] == 4
                assert body["in_memory"] == 1

            async with client.post(f"{base}/v1/drain-stop", headers=headers) as response:
                assert response.status == 202
            await asyncio.wait_for(drained.wait(), timeout=1)

            async with client.get(f"{base}/v1/readiness", headers=headers) as response:
                assert response.status == 503
                assert (await response.json())["draining"] is True
    finally:
        await plane.close()


def test_control_plane_rejects_short_token():
    with pytest.raises(ValueError, match="32"):
        ControlPlane(
            token="short",  # noqa: S106
            mongo=Mongo(),
            upload_queue=asyncio.Queue(),
            drain_callback=lambda: _set_event(asyncio.Event()),
            status_provider=dict,
        )


def test_ftp_security_configuration_fails_closed():
    assert validate_ftp_security("ftp", None, None, False) is False
    assert validate_ftp_security("ftps-explicit", "cert.pem", "key.pem", True) is True
    assert validate_ftp_security("ftps-implicit", "cert.pem", "key.pem", True) is True
    with pytest.raises(ValueError, match="FTPS security mode"):
        validate_ftp_security("ftp", "cert.pem", "key.pem", False)
    with pytest.raises(ValueError, match="requires"):
        validate_ftp_security("ftps-explicit", None, None, False)
    with pytest.raises(ValueError, match="forbids"):
        validate_ftp_security("ftp", None, None, True)


@pytest.mark.asyncio
async def test_ftps_mode_fails_closed_without_tls_context():
    server = ftp_server.Server(object(), object, security_mode="ftps-explicit")
    with pytest.raises(RuntimeError, match="TLS context"):
        await server.start()


@pytest.mark.asyncio
async def test_passive_listener_uses_configured_range(monkeypatch):
    attempts = []
    bound = object()

    async def fake_start_server(_handler, _host, port, **kwargs):
        attempts.append(port)
        assert kwargs["ssl"] is None
        if port == 60000:
            raise OSError("busy")
        return bound

    monkeypatch.setattr(ftp_server, "start_server", fake_start_server)
    server = ftp_server.Server(object(), object, passive_ports=range(60000, 60002))
    server._start_server_extra_arguments = {}

    assert await server._start_passive_listener(object(), "127.0.0.1") is bound
    assert attempts == [60000, 60001]


@pytest.mark.asyncio
async def test_explicit_and_implicit_ftps_do_not_mix_control_modes(monkeypatch):
    calls = []

    async def fake_start_server(_handler, _host, _port, **kwargs):
        calls.append(kwargs["ssl"])
        return type("Listener", (), {"sockets": []})()

    monkeypatch.setattr(ftp_server, "start_server", fake_start_server)
    tls = object()
    explicit = ftp_server.Server(object(), object, security_mode="ftps-explicit")
    explicit.set_ssl_context(tls)
    await explicit.start()
    await explicit._start_passive_listener(object(), "127.0.0.1", protected=True)
    implicit = ftp_server.Server(object(), object, security_mode="ftps-implicit")
    implicit.set_ssl_context(tls)
    await implicit.start()

    assert calls == [None, tls, tls]
