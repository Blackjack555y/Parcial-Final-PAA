"""E2E contra el stack desplegado (contenedores o Kubernetes), solo por su API pública.

Variables:
  GATEWAY_URL        http://127.0.0.1:8000   (por defecto)
  FLAGS_ADMIN_TOKEN  token admin; si falta, se lee de .run/admin-token
  E2E_ENV            ambiente esperado (dev por defecto)
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
FLAG_CACHE_SECONDS = 6  # caché de flags de cada servicio (5 s) + margen


class Gateway:
    def __init__(self, url: str, token: str, env: str) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.env = env
        self.http = httpx.Client(base_url=self.url, timeout=30)

    def gql(self, query: str, variables: dict[str, Any] | None = None, *, admin=False):
        headers = {"x-admin-token": self.token} if admin else {}
        response = self.http.post(
            "/graphql", json={"query": query, "variables": variables or {}}, headers=headers
        )
        response.raise_for_status()
        body = response.json()
        assert not body.get("errors"), body["errors"]
        return body["data"]

    def set_flag(self, name: str, enabled: bool) -> None:
        self.gql(
            "mutation($n: String!, $e: Boolean!) "
            "{ setFeatureFlag(name: $n, enabled: $e) { name } }",
            {"n": name, "e": enabled},
            admin=True,
        )
        time.sleep(FLAG_CACHE_SECONDS)  # que todos los servicios vean el cambio

    def free_connector(self, connector_type: str = "CCS2") -> tuple[str, str]:
        stations = self.gql("{ stations { stationId connectors { connectorId type status } } }")
        for station in stations["stations"]:
            for c in station["connectors"]:
                if c["type"] == connector_type and c["status"] == "AVAILABLE":
                    return station["stationId"], c["connectorId"]
        pytest.fail(f"No hay conectores {connector_type} libres para la prueba")


@pytest.fixture(scope="session")
def gateway() -> Gateway:
    url = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000")
    token = os.environ.get("FLAGS_ADMIN_TOKEN") or (
        (ROOT / ".run" / "admin-token").read_text().strip()
        if (ROOT / ".run" / "admin-token").exists()
        else ""
    )
    try:
        httpx.get(f"{url}/healthz", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(f"Gateway no disponible en {url} ({exc}); levanta ./scripts/dev.sh up-apps")
    if not token:
        pytest.fail("Falta FLAGS_ADMIN_TOKEN (o .run/admin-token)")
    return Gateway(url, token, os.environ.get("E2E_ENV", "dev"))


@pytest.fixture
def unique_key() -> str:
    return f"e2e-{uuid.uuid4().hex}"
