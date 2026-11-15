"""Esquema GraphQL unificado de AegisVolt.

Queries        → consultar estaciones, billeteras, sagas, sesiones, telemetría y flags.
Mutations      → iniciar la SAGA, detener la carga, reintentar compensaciones, cambiar flags.
Subscriptions  → cambios en tiempo real por WebSocket (graphql-transport-ws): el servidor
                 consulta DynamoDB cada ~1,5 s y emite SOLO cuando el resultado cambia, por
                 eso funciona igual con varias réplicas del gateway (sin pub/sub).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import json
import os
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import strawberry
from strawberry.types import Info

from aegis_common.flags import KNOWN_FLAGS
from aegis_common.logging import log_event
from graphql_gateway.clients import ServiceError
from graphql_gateway.saga.models import SagaRequest

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
POLL_SECONDS = float(os.environ.get("SUBSCRIPTION_POLL_SECONDS", "1.5"))


class UserError(Exception):
    """Error que SÍ se muestra al cliente (validación, permisos, reglas de negocio)."""


# --- Tipos ------------------------------------------------------------------------------
@strawberry.type
class Connector:
    connector_id: str
    type: str
    power_kw: float
    status: str
    reserved_by: str | None
    fleet_id: str | None


@strawberry.type
class Station:
    station_id: str
    name: str
    city: str
    address: str
    connectors: list[Connector]

    @strawberry.field
    def available_connectors(self) -> int:
        return sum(1 for c in self.connectors if c.status == "AVAILABLE")


@strawberry.type
class Transaction:
    saga_id: str
    type: str
    status: str
    amount_cents: int
    kwh: float
    unit_price_cents: int
    surge_active: bool
    connector_type: str
    created_at: str
    refunded_at: str | None
    refund_reason: str | None


@strawberry.type
class Wallet:
    fleet_id: str
    name: str
    balance_cents: int
    currency: str
    transactions: list[Transaction]


@strawberry.type
class Quote:
    connector_type: str
    kwh: float
    unit_price_cents: int
    surge_active: bool
    surge_multiplier: float
    amount_cents: int


@strawberry.type
class SagaEvent:
    at: str
    step: str
    action: str
    outcome: str
    detail: str


@strawberry.type
class Saga:
    saga_id: str
    status: str
    station_id: str
    connector_id: str
    fleet_id: str
    kwh: float
    correlation_id: str | None
    amount_cents: int | None
    surge_active: bool | None
    failure_reason: str | None
    failure_message: str | None
    energy_kwh: float | None
    created_at: str
    updated_at: str
    events: list[SagaEvent]


@strawberry.type
class SagaPayload:
    success: bool
    message: str
    saga: Saga | None


@strawberry.type
class ChargingSession:
    saga_id: str
    status: str
    station_id: str
    connector_id: str
    power_kw: float
    started_at: str
    ended_at: str | None
    failure_reason: str | None


@strawberry.type
class Telemetry:
    saga_id: str
    status: str
    elapsed_s: float
    power_kw: float
    energy_kwh: float
    soc_percent: float
    voltage_v: float
    current_a: float


@strawberry.type
class FeatureFlag:
    name: str
    enabled: bool
    description: str
    updated_by: str
    updated_at: str


@strawberry.type
class EnvironmentInfo:
    name: str
    version: str
    region: str


@strawberry.input
class ReserveChargeInput:
    station_id: str
    connector_id: str
    fleet_id: str
    kwh: float
    idempotency_key: str | None = None


# --- Conversión dict → tipo -------------------------------------------------------------
def _pick(cls: type, data: dict[str, Any], **extra: Any) -> Any:
    kwargs = {name: data.get(name) for name in cls.__annotations__ if name not in extra}
    return cls(**kwargs, **extra)


def to_station(data: dict[str, Any]) -> Station:
    return Station(
        station_id=data["station_id"],
        name=data.get("name", data["station_id"]),
        city=data.get("city", ""),
        address=data.get("address", ""),
        connectors=[_pick(Connector, c) for c in data.get("connectors", [])],
    )


def to_saga(data: dict[str, Any]) -> Saga:
    return _pick(Saga, data, events=[_pick(SagaEvent, e) for e in data.get("events", [])])


def to_flag(flag: Any) -> FeatureFlag:
    return FeatureFlag(
        name=flag.name,
        enabled=flag.enabled,
        description=flag.description,
        updated_by=flag.updated_by,
        updated_at=flag.updated_at,
    )


# --- Utilidades ---------------------------------------------------------------------------
def _ctx(info: Info) -> dict[str, Any]:
    return info.context


def _require_admin(info: Info) -> str:
    expected = os.environ.get("FLAGS_ADMIN_TOKEN", "")
    request = _ctx(info).get("request")
    provided = request.headers.get("x-admin-token", "") if request is not None else ""
    if not expected or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise UserError("Operación administrativa: falta un X-Admin-Token válido")
    return "admin-token"


def _validate_id(value: str, field: str) -> str:
    if not ID_RE.match(value):
        raise UserError(f"{field} inválido")
    return value


async def _service(call: Awaitable[Any]) -> Any:
    try:
        return await call
    except ServiceError as err:
        raise UserError(f"{err.code}: {err.message}") from err


async def _fetch_wallet(info: Info, fleet_id: str) -> Wallet:
    billing = _ctx(info)["clients"].billing
    wallet, txns = await asyncio.gather(
        _service(billing.call("GET", f"/wallets/{fleet_id}")),
        _service(billing.call("GET", f"/wallets/{fleet_id}/transactions")),
    )
    return Wallet(
        fleet_id=wallet["fleet_id"],
        name=wallet["name"],
        balance_cents=wallet["balance_cents"],
        currency=wallet["currency"],
        transactions=[_pick(Transaction, t) for t in txns],
    )


async def _watch(fetch: Callable[[], Awaitable[Any]]) -> AsyncGenerator[Any, None]:
    """Emite el valor inicial y luego solo cuando cambia."""
    last = None
    while True:
        value = await fetch()
        fingerprint = json.dumps(value, sort_keys=True, default=str)
        if fingerprint != last:
            last = fingerprint
            yield value
        await asyncio.sleep(POLL_SECONDS)


# --- Query --------------------------------------------------------------------------------
@strawberry.type
class Query:
    @strawberry.field(description="Ambiente al que está conectado este endpoint GraphQL.")
    def environment(self, info: Info) -> EnvironmentInfo:
        s = _ctx(info)["settings"]
        return EnvironmentInfo(name=s.env.value, version=s.version, region=s.region)

    @strawberry.field
    async def stations(self, info: Info) -> list[Station]:
        data = await _service(_ctx(info)["clients"].reservations.call("GET", "/stations"))
        return [to_station(s) for s in data]

    @strawberry.field
    async def station(self, info: Info, station_id: str) -> Station:
        _validate_id(station_id, "stationId")
        path = f"/stations/{station_id}"
        return to_station(await _service(_ctx(info)["clients"].reservations.call("GET", path)))

    @strawberry.field
    async def wallet(self, info: Info, fleet_id: str) -> Wallet:
        return await _fetch_wallet(info, _validate_id(fleet_id, "fleetId"))

    @strawberry.field
    async def quote(self, info: Info, connector_type: str, kwh: float) -> Quote:
        params = {"connector_type": connector_type, "kwh": kwh}
        data = await _service(_ctx(info)["clients"].billing.call("GET", "/quotes", params=params))
        return _pick(Quote, data)

    @strawberry.field
    async def sagas(self, info: Info, limit: int = 20) -> list[Saga]:
        limit = max(1, min(limit, 100))
        repo = _ctx(info)["orchestrator"].repo
        return [to_saga(s) for s in await asyncio.to_thread(repo.list_recent, limit)]

    @strawberry.field
    async def saga(self, info: Info, saga_id: str) -> Saga | None:
        data = await _ctx(info)["orchestrator"].get(saga_id)
        return to_saga(data) if data else None

    @strawberry.field
    async def charging_sessions(
        self, info: Info, status: str | None = None
    ) -> list[ChargingSession]:
        params = {"status": status} if status else None
        data = await _service(
            _ctx(info)["clients"].dispatch.call("GET", "/sessions", params=params)
        )
        return [_pick(ChargingSession, s) for s in data]

    @strawberry.field
    async def telemetry(self, info: Info, saga_id: str) -> Telemetry:
        path = f"/sessions/{saga_id}/telemetry"
        return _pick(Telemetry, await _service(_ctx(info)["clients"].dispatch.call("GET", path)))

    @strawberry.field
    async def feature_flags(self, info: Info) -> list[FeatureFlag]:
        flags = await asyncio.to_thread(_ctx(info)["flags"].all)
        return [to_flag(f) for f in sorted(flags.values(), key=lambda f: f.name)]


# --- Mutation -----------------------------------------------------------------------------
@strawberry.type
class Mutation:
    @strawberry.mutation(description="Inicia la SAGA: reservar → cobrar → activar el cargador.")
    async def reserve_and_start_charging(
        self, info: Info, input: ReserveChargeInput
    ) -> SagaPayload:
        for value, name in (
            (input.station_id, "stationId"),
            (input.connector_id, "connectorId"),
            (input.fleet_id, "fleetId"),
        ):
            _validate_id(value, name)
        if not (0 < input.kwh <= 500):
            raise UserError("kwh debe estar entre 0 y 500")
        if input.idempotency_key and len(input.idempotency_key) > 128:
            raise UserError("idempotencyKey demasiado larga")
        saga = await _ctx(info)["orchestrator"].start(
            SagaRequest(
                input.station_id,
                input.connector_id,
                input.fleet_id,
                input.kwh,
                input.idempotency_key,
            )
        )
        ok = saga["status"] == "COMPLETED"
        message = (
            "Carga activa"
            if ok
            else f"SAGA {saga['status']}: {saga.get('failure_message') or saga['status']}"
        )
        return SagaPayload(success=ok, message=message, saga=to_saga(saga))

    @strawberry.mutation(description="Detiene la carga y libera el conector.")
    async def stop_charging(self, info: Info, saga_id: str) -> SagaPayload:
        try:
            saga = await _ctx(info)["orchestrator"].stop(saga_id)
        except (LookupError, ValueError) as exc:
            raise UserError(str(exc)) from exc
        except ServiceError as err:
            raise UserError(f"{err.code}: {err.message}") from err
        return SagaPayload(success=True, message="Carga finalizada", saga=to_saga(saga))

    @strawberry.mutation(description="Re-ejecuta las compensaciones (idempotentes). Admin.")
    async def retry_compensation(self, info: Info, saga_id: str) -> SagaPayload:
        _require_admin(info)
        try:
            saga = await _ctx(info)["orchestrator"].retry_compensation(saga_id)
        except (LookupError, ValueError) as exc:
            raise UserError(str(exc)) from exc
        return SagaPayload(
            success=saga["status"] == "COMPENSATED",
            message=f"Compensación re-ejecutada: {saga['status']}",
            saga=to_saga(saga),
        )

    @strawberry.mutation(description="Cambia una feature flag de ESTE ambiente. Admin.")
    async def set_feature_flag(self, info: Info, name: str, enabled: bool) -> FeatureFlag:
        actor = _require_admin(info)
        if name not in KNOWN_FLAGS:
            raise UserError(f"Flag desconocida: {name}")
        flag = await asyncio.to_thread(_ctx(info)["flags"].set, name, enabled, actor)
        log_event(_ctx(info)["log"], "feature_flag_changed", flag=name, enabled=enabled)
        return to_flag(flag)

    @strawberry.mutation(description="Recarga saldo de una flota (demo). Admin.")
    async def top_up_wallet(self, info: Info, fleet_id: str, amount_cents: int) -> Wallet:
        _require_admin(info)
        _validate_id(fleet_id, "fleetId")
        billing = _ctx(info)["clients"].billing
        await _service(
            billing.call("POST", f"/wallets/{fleet_id}/topup", json={"amount_cents": amount_cents})
        )
        return await _fetch_wallet(info, fleet_id)


# --- Subscription -------------------------------------------------------------------------
@strawberry.type
class Subscription:
    @strawberry.subscription(description="Estado de estaciones y conectores en tiempo real.")
    async def stations(self, info: Info) -> AsyncGenerator[list[Station], None]:
        clients = _ctx(info)["clients"]
        async for data in _watch(lambda: clients.reservations.call("GET", "/stations")):
            yield [to_station(s) for s in data]

    @strawberry.subscription(description="Sagas recientes y sus compensaciones en tiempo real.")
    async def sagas(self, info: Info, limit: int = 20) -> AsyncGenerator[list[Saga], None]:
        repo = _ctx(info)["orchestrator"].repo
        limit = max(1, min(limit, 100))
        async for data in _watch(lambda: asyncio.to_thread(repo.list_recent, limit)):
            yield [to_saga(s) for s in data]

    @strawberry.subscription(description="Feature flags del ambiente en tiempo real.")
    async def feature_flags(self, info: Info) -> AsyncGenerator[list[FeatureFlag], None]:
        flags = _ctx(info)["flags"]

        async def fetch() -> list[dict[str, Any]]:
            current = await asyncio.to_thread(flags.all, fresh=True)
            return [dataclasses.asdict(f) for f in current.values()]

        async for _ in _watch(fetch):
            current = await asyncio.to_thread(flags.all)
            yield [to_flag(f) for f in sorted(current.values(), key=lambda f: f.name)]


schema_types = (Query, Mutation, Subscription)
