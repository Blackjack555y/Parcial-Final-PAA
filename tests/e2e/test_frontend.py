"""E2E de la consola web: nginx sin root, cabeceras de seguridad, proxy /graphql y WSS."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest
import websockets

pytestmark = pytest.mark.e2e

FRONTEND = os.environ.get("FRONTEND_URL", "http://127.0.0.1:3000")


@pytest.fixture(scope="module")
def frontend() -> str:
    try:
        httpx.get(f"{FRONTEND}/healthz", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(f"Consola no disponible en {FRONTEND} ({exc})")
    return FRONTEND


def test_console_is_served_with_security_headers(frontend):
    response = httpx.get(f"{frontend}/", timeout=10)
    assert response.status_code == 200
    assert "AegisVolt" in response.text
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "nginx/" not in response.headers.get("server", "")  # server_tokens off


def test_spa_routes_fall_back_to_index(frontend):
    assert "AegisVolt" in httpx.get(f"{frontend}/cualquier/ruta", timeout=10).text


def test_graphql_is_proxied_to_the_environment_gateway(frontend):
    response = httpx.post(
        f"{frontend}/graphql", json={"query": "{ environment { name } }"}, timeout=10
    )
    assert response.json()["data"]["environment"]["name"] == os.environ.get("E2E_ENV", "dev")


def _ws_handshake(origin: str) -> str:
    async def run() -> str:
        url = FRONTEND.replace("http", "ws", 1) + "/graphql"
        try:
            async with websockets.connect(
                url, subprotocols=["graphql-transport-ws"], origin=origin, open_timeout=5
            ) as ws:
                await ws.send(json.dumps({"type": "connection_init"}))
                return json.loads(await asyncio.wait_for(ws.recv(), 5))["type"]
        except websockets.InvalidStatus as exc:
            return f"rechazado {exc.response.status_code}"

    return asyncio.run(run())


def test_websocket_from_the_console_origin_is_accepted(frontend):
    assert _ws_handshake(frontend) == "connection_ack"


def test_websocket_from_a_foreign_origin_is_rejected(frontend):
    assert _ws_handshake("https://sitio-malicioso.example") == "rechazado 403"
