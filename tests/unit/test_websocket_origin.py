"""Protección contra Cross-Site WebSocket Hijacking en el gateway (allowed_origins)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aegis_common import Environment, Settings
from graphql_gateway.main import create_app

PROTOCOL = ["graphql-transport-ws"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "http://127.0.0.1:3001")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    settings = Settings(env=Environment.DEV, aws_endpoint_url="http://127.0.0.1:9", service="gw")
    return TestClient(create_app(settings))


def test_websocket_from_the_environment_console_is_accepted(client):
    with client.websocket_connect(
        "/graphql", subprotocols=PROTOCOL, headers={"origin": "http://127.0.0.1:3001"}
    ) as ws:
        ws.send_json({"type": "connection_init"})
        assert ws.receive_json()["type"] == "connection_ack"


def test_websocket_from_a_foreign_site_is_rejected(client):
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/graphql", subprotocols=PROTOCOL, headers={"origin": "https://sitio-malicioso.example"}
        ) as ws,
    ):
        ws.send_json({"type": "connection_init"})
        ws.receive_json()
