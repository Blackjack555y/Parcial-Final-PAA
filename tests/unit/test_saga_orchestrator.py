"""Lógica de la SAGA: orden de pasos, compensaciones en orden inverso, reintentos e idempotencia.

Los tres microservicios se simulan con httpx.MockTransport; la bitácora vive en memoria.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from graphql_gateway.clients import ServiceClient, ServiceClients
from graphql_gateway.saga.models import SagaRequest, SagaStatus, saga_id_for
from graphql_gateway.saga.orchestrator import SagaOrchestrator


class MemorySagaRepo:
    def __init__(self) -> None:
        self.sagas: dict[str, dict[str, Any]] = {}

    def create(self, saga_id, req, correlation_id):
        if saga_id in self.sagas:
            return self.get(saga_id), False
        self.sagas[saga_id] = {
            "saga_id": saga_id,
            "status": "STARTED",
            "station_id": req.station_id,
            "connector_id": req.connector_id,
            "fleet_id": req.fleet_id,
            "kwh": req.kwh,
            "events": [],
        }
        return self.get(saga_id), True

    def set_status(self, saga_id, status, **fields):
        self.sagas[saga_id].update({"status": str(status), **fields})

    def add_event(self, saga_id, step, action, outcome, detail=""):
        self.sagas[saga_id]["events"].append(
            {"step": step, "action": action, "outcome": outcome, "detail": detail}
        )

    def get(self, saga_id):
        saga = self.sagas.get(saga_id)
        return json.loads(json.dumps(saga)) if saga else None


class FakeServices:
    """Simula los tres servicios y registra el orden de las llamadas."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail: dict[str, tuple[int, str]] = {}  # "METHOD path-prefix" → (status, code)
        self.fail_times: dict[str, int] = {}  # cuántas veces falla antes de responder OK
        self.refunds = 0
        self.releases = 0

    def handler(self, service: str):
        def handle(request: httpx.Request) -> httpx.Response:
            key = f"{service} {request.method} {request.url.path}"
            self.calls.append(key)
            for prefix, (status, code) in self.fail.items():
                if key.startswith(prefix):
                    remaining = self.fail_times.get(prefix)
                    if remaining is None or remaining > 0:
                        if remaining is not None:
                            self.fail_times[prefix] = remaining - 1
                        return httpx.Response(status, json={"error": code, "message": code})
            return httpx.Response(200, json=self._ok(service, request))

        return handle

    def _ok(self, service: str, request: httpx.Request) -> dict[str, Any]:
        path = request.url.path
        if service == "res" and path == "/reservations":
            return {"connector_type": "CCS2", "power_kw": 150, "status": "RESERVED"}
        if service == "res" and path.endswith("/release"):
            self.releases += 1
            return {"released": self.releases == 1, "detail": "ok"}
        if service == "bil" and path == "/charges":
            return {"amount_cents": 1800, "surge_active": False, "status": "CHARGED"}
        if service == "bil" and path.endswith("/refund"):
            self.refunds += 1
            return {"refunded": self.refunds == 1, "amount_cents": 1800, "detail": "ok"}
        return {"status": "ACTIVE", "energy_kwh": 0.0}

    def clients(self) -> ServiceClients:
        def mk(name: str, svc: str) -> ServiceClient:
            return ServiceClient(
                name, "http://test", transport=httpx.MockTransport(self.handler(svc))
            )

        return ServiceClients(
            reservations=mk("station-reservation-service", "res"),
            billing=mk("billing-wallet-service", "bil"),
            dispatch=mk("energy-dispatch-service", "dis"),
        )


@pytest.fixture
def services() -> FakeServices:
    return FakeServices()


@pytest.fixture
def orchestrator(services: FakeServices) -> SagaOrchestrator:
    return SagaOrchestrator(
        MemorySagaRepo(),
        services.clients(),
        logging.getLogger("test"),
        compensation_attempts=3,
        backoff_seconds=0,
    )


REQ = SagaRequest("bog-norte", "c1", "fleet-a", 40.0)


def _compensations(saga: dict[str, Any]) -> list[tuple[str, str]]:
    return [(e["step"], e["outcome"]) for e in saga["events"] if e["action"] == "compensation"]


async def test_happy_path_runs_three_steps_in_order(orchestrator, services):
    saga = await orchestrator.start(REQ)
    assert saga["status"] == SagaStatus.COMPLETED
    assert [c.split(" ", 1)[1] for c in services.calls] == [
        "POST /reservations",
        "POST /charges",
        "POST /sessions",
        f"POST /reservations/{saga['saga_id']}/occupy",
    ]
    assert _compensations(saga) == []


async def test_charger_fault_refunds_then_releases(orchestrator, services):
    services.fail["dis POST /sessions"] = (503, "CHARGER_FAULT")
    saga = await orchestrator.start(REQ)

    assert saga["status"] == SagaStatus.COMPENSATED
    assert saga["failure_reason"] == "dispatch:CHARGER_FAULT"
    # Orden INVERSO: primero se deshace el cobro (paso 2), luego la reserva (paso 1).
    assert _compensations(saga) == [("charge", "OK"), ("reserve", "OK")]
    refund_idx = next(i for i, c in enumerate(services.calls) if c.endswith("/refund"))
    release_idx = next(i for i, c in enumerate(services.calls) if c.endswith("/release"))
    assert refund_idx < release_idx


async def test_insufficient_funds_only_releases_connector(orchestrator, services):
    services.fail["bil POST /charges"] = (402, "INSUFFICIENT_FUNDS")
    saga = await orchestrator.start(REQ)

    assert saga["status"] == SagaStatus.COMPENSATED
    assert _compensations(saga) == [("reserve", "OK")]
    assert services.refunds == 0
    assert not any("/sessions" in c for c in services.calls)


async def test_failure_in_first_step_has_nothing_to_compensate(orchestrator, services):
    services.fail["res POST /reservations"] = (409, "CONNECTOR_UNAVAILABLE")
    saga = await orchestrator.start(REQ)

    assert saga["status"] == SagaStatus.FAILED
    assert _compensations(saga) == []
    assert len(services.calls) == 1


async def test_compensation_is_retried_until_it_succeeds(orchestrator, services):
    services.fail["dis POST /sessions"] = (503, "CHARGER_FAULT")
    services.fail["bil POST /charges/"] = (503, "UNAVAILABLE")
    services.fail_times["bil POST /charges/"] = 2  # el reembolso falla 2 veces y luego funciona
    saga = await orchestrator.start(REQ)

    assert saga["status"] == SagaStatus.COMPENSATED
    assert sum(c.endswith("/refund") for c in services.calls) == 3


async def test_exhausted_retries_mark_compensation_failed(orchestrator, services):
    services.fail["dis POST /sessions"] = (503, "CHARGER_FAULT")
    services.fail["bil POST /charges/"] = (503, "UNAVAILABLE")  # siempre falla
    saga = await orchestrator.start(REQ)

    assert saga["status"] == SagaStatus.COMPENSATION_FAILED
    # La liberación del conector se intenta igual: una compensación fallida no detiene las demás.
    assert _compensations(saga) == [("charge", "FAILED"), ("reserve", "OK")]


async def test_retry_compensation_is_safe_because_compensations_are_idempotent(
    orchestrator, services
):
    services.fail["dis POST /sessions"] = (503, "CHARGER_FAULT")
    saga = await orchestrator.start(REQ)
    again = await orchestrator.retry_compensation(saga["saga_id"])

    assert again["status"] == SagaStatus.COMPENSATED
    assert services.refunds == 2  # se llamó dos veces…
    details = [e["detail"] for e in again["events"] if e["action"] == "compensation"]
    assert "refunded=False" in details[-2]  # …pero el servicio solo reembolsó la primera


async def test_idempotency_key_reuses_the_same_saga(orchestrator, services):
    req = SagaRequest("bog-norte", "c1", "fleet-a", 40.0, idempotency_key="pedido-123")
    first = await orchestrator.start(req)
    second = await orchestrator.start(req)

    assert first["saga_id"] == second["saga_id"] == saga_id_for("pedido-123")
    assert sum(c.endswith(" /charges") for c in services.calls) == 1


async def test_correlation_and_saga_headers_are_propagated(services):
    seen: list[httpx.Headers] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return services.handler("res")(request)

    from aegis_common.context import correlation_id_var, saga_id_var

    client = ServiceClient("svc", "http://test", transport=httpx.MockTransport(capture))
    c_token = correlation_id_var.set("corr-abc")
    s_token = saga_id_var.set("saga-123")
    try:
        await client.call("POST", "/reservations")
    finally:
        correlation_id_var.reset(c_token)
        saga_id_var.reset(s_token)
    assert seen[0]["x-correlation-id"] == "corr-abc"
    assert seen[0]["x-saga-id"] == "saga-123"
