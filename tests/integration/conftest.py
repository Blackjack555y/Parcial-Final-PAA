"""Entorno de integración: los 4 servicios en proceso contra DynamoDB real (Floci).

Las tablas se crean con un prefijo aleatorio (it<hex>-<tabla>-dev) y se borran al terminar:
las pruebas nunca tocan los datos de los ambientes dev/test/prod.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from typing import Any

import boto3
import httpx
import pytest

from aegis_common import Environment, Settings
from aegis_common.aws import to_item
from aegis_common.flags import KNOWN_FLAGS, FlagStore

ROOT = Path(__file__).resolve().parents[2]
ADMIN_TOKEN = "test-admin-token"  # noqa: S105 — valor de prueba


def _load_seed():
    spec = importlib.util.spec_from_file_location("seed", ROOT / "scripts" / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_tables(ddb: Any, settings: Settings) -> list[str]:
    """Mismo esquema de claves que infra/modules/dynamodb (pk/sk, GSI by-status en sagas)."""
    names = []
    for resource in ("reservations", "billing", "dispatch", "sagas", "flags"):
        name = settings.table(resource)
        keys = [{"AttributeName": "pk", "KeyType": "HASH"}]
        attrs = [{"AttributeName": "pk", "AttributeType": "S"}]
        if resource != "flags":
            keys.append({"AttributeName": "sk", "KeyType": "RANGE"})
            attrs.append({"AttributeName": "sk", "AttributeType": "S"})
        kwargs: dict[str, Any] = {}
        if resource == "sagas":
            attrs += [
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
            ]
            kwargs["GlobalSecondaryIndexes"] = [
                {
                    "IndexName": "by-status",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ]
        ddb.create_table(
            TableName=name,
            KeySchema=keys,
            AttributeDefinitions=attrs,
            BillingMode="PAY_PER_REQUEST",
            **kwargs,
        )
        names.append(name)
    return names


class Platform:
    def __init__(
        self, gateway: httpx.AsyncClient, settings: Settings, ddb: Any, apps: dict, gateway_app
    ) -> None:
        self.gateway = gateway
        self.settings = settings
        self.ddb = ddb
        self.apps = apps
        self.gateway_app = gateway_app

    async def gql(
        self, query: str, variables: dict[str, Any] | None = None, *, admin: bool = False
    ) -> dict[str, Any]:
        headers = {"x-admin-token": ADMIN_TOKEN} if admin else {}
        response = await self.gateway.post(
            "/graphql", json={"query": query, "variables": variables or {}}, headers=headers
        )
        response.raise_for_status()
        return response.json()

    @property
    def reservations_repo(self):
        return self.apps["reservations"].state.repo

    @property
    def supervisor(self):
        return self.gateway_app.state.supervisor

    def item(self, resource: str, pk: str, sk: str | None = "META") -> dict[str, Any] | None:
        from aegis_common.aws import from_item

        key = {"pk": pk} if sk is None else {"pk": pk, "sk": sk}
        raw = self.ddb.get_item(
            TableName=self.settings.table(resource), Key=to_item(key), ConsistentRead=True
        ).get("Item")
        return from_item(raw)


@pytest.fixture
async def platform(aws_endpoint, monkeypatch):
    from billing_wallet.main import create_app as billing_app
    from energy_dispatch.main import create_app as dispatch_app
    from graphql_gateway.clients import ServiceClient, ServiceClients
    from graphql_gateway.main import create_app as gateway_app
    from station_reservation.main import create_app as reservation_app

    monkeypatch.setenv("PEAK_HOURS", "0-24")  # tarifa determinista: siempre "hora pico"
    monkeypatch.setenv("CHARGING_SIMULATION_SPEEDUP", "2000")  # la carga termina en segundos
    monkeypatch.setenv("FLAGS_ADMIN_TOKEN", ADMIN_TOKEN)

    prefix = f"it{uuid.uuid4().hex[:8]}"

    def settings(service: str) -> Settings:
        return Settings(
            env=Environment.DEV,
            aws_endpoint_url=aws_endpoint,
            service=service,
            table_prefix=prefix,
        )

    base = settings("integration")
    ddb = boto3.client("dynamodb", endpoint_url=aws_endpoint)
    tables = _create_tables(ddb, base)
    _load_seed().seed(base, reset=True)
    for name in KNOWN_FLAGS:  # valores iniciales de dev
        ddb.put_item(
            TableName=base.table("flags"),
            Item=to_item({"pk": name, "enabled": name != "SIMULATE_CHARGER_HARDWARE_FAULT"}),
        )

    flags = FlagStore(base, ttl_seconds=0)  # sin caché: cada lectura ve el último valor
    apps = {
        "reservations": reservation_app(settings("station-reservation-service"), flags),
        "billing": billing_app(settings("billing-wallet-service"), flags),
        "dispatch": dispatch_app(settings("energy-dispatch-service"), flags),
    }
    clients = ServiceClients(
        **{
            key: ServiceClient(key, "http://svc", transport=httpx.ASGITransport(app=app))
            for key, app in apps.items()
        }
    )
    gateway = gateway_app(settings("graphql-gateway"), clients, flags)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as http:
        yield Platform(http, base, ddb, apps, gateway)
    await clients.aclose()
    for name in tables:
        ddb.delete_table(TableName=name)
