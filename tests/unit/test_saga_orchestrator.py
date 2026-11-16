"""Lógica de la SAGA: orden de pasos, compensaciones en orden inverso, reintentos, idempotencia,
reserva anticipada (larga duración con timeout), liquidación y fin automático.

Los tres microservicios se simulan con httpx.MockTransport; la bitácora vive en memoria.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from graphql_gateway.clients import ServiceClient, ServiceClients
from graphql_gateway.saga.models import SagaRequest, SagaStatus, saga_id_for
from graphql_gateway.saga.orchestrator import SagaOrchestrator
from graphql_gateway.saga.supervisor import SagaSupervisor


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

    def list_by_status(self, status):
        return [self.get(s) for s, v in self.sagas.items() if v["status"] == str(status)]


class FakeServices:
    """Simula los tres servicios y registra el orden de las llamadas."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail: dict[str, tuple[int, str]] = {}  # "svc METHOD path-prefix" → (status, code)
        self.fail_times: dict[str, int] = {}  # cuántas veces falla antes de responder OK
        self.fund_releases = 0
        self.target_reached = False

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
            return {"released": True, "detail": "ok"}
        if service == "bil" and path == "/holds":
            return {"amount_cents": 1800, "surge_active": False, "status": "HELD"}
        if service == "bil" and path.endswith("/release"):
            self.fund_releases += 1
            return {"released": self.fund_releases == 1, "amount_cents": 1800, "detail": "ok"}
        if service == "bil" and path.endswith("/capture"):
            return {"captured_cents": 1200, "released_cents": 600, "status": "CAPTURED"}
        if service == "dis" and path.endswith("/stop"):
            return {"status": "COMPLETED", "energy_kwh": 26.7}
        if service == "dis" and path.endswith("/telemetry"):
            return {"status": "ACTIVE", "target_reached": self.target_reached}
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
AHEAD = SagaRequest("bog-norte", "c1", "fleet-a", 40.0, arrival_minutes=5)


def _compensations(saga: dict[str, Any]) -> list[tuple[str, str]]:
    return [(e["step"], e["outcome"]) for e in saga["events"] if e["action"] == "compensation"]


def _paths(services: FakeServices) -> list[str]:
    return [c.split(" ", 1)[1] for c in services.calls]


# --- Flujo inmediato --------------------------------------------------------------------------
async def test_happy_path_reserves_holds_and_dispatches_in_order(orchestrator, services):
    saga = await orchestrator.start(REQ)
    assert saga["status"] == SagaStatus.COMPLETED
    assert _paths(services) == [
        "POST /reservations",
        "POST /holds",
        "POST /sessions",
        f"POST /reservations/{saga['saga_id']}/occupy",
    ]
    assert _compensations(saga) == []


async def test_charger_fault_releases_funds_then_connector(orchestrator, services):
    services.fail["dis POST /sessions"] = (503, "CHARGER_FAULT")
    saga = await orchestrator.start(REQ)

    assert saga["status"] == SagaStatus.COMPENSATED
    assert saga["failure_reason"] == "dispatch:CHARGER_FAULT"
    # Orden INVERSO: primero se libera la retención (paso 2), luego la bahía (paso 1).
    assert _compensations(saga) == [("charge", "OK"), ("reserve", "OK")]
    paths = _paths(services)
    assert paths.index(f"POST /holds/{saga['saga_id']}/release") < paths.index(
        f"POST /reservations/{saga['saga_id']}/release"
    )


async def test_insufficient_funds_only_releases_connector(orchestrator, services):
    services.fail["bil POST /holds"] = (402, "INSUFFICIENT_FUNDS")
    saga = await orchestrator.start(REQ)

    assert saga["status"] == SagaStatus.COMPENSATED
    assert _compensations(saga) == [("reserve", "OK")]
    assert services.fund_releases == 0
    assert not any("/sessions" in c for c in services.calls)


async def test_failure_in_first_step_has_nothing_to_compensate(orchestrator, services):
    services.fail["res POST /reservations"] = (409, "CONNECTOR_UNAVAILABLE")
    saga = await orchestrator.start(REQ)

    assert saga["status"] == SagaStatus.FAILED
    assert _compensations(saga) == []
    assert len(services.calls) == 1


async def test_compensation_is_retried_until_it_succeeds(orchestrator, services):
    services.fail["dis POST /sessions"] = (503, "CHARGER_FAULT")
    services.fail["bil POST /holds/"] = (503, "UNAVAILABLE")
    services.fail_times["bil POST /holds/"] = 2  # la liberación falla 2 veces y luego funciona
    saga = await orchestrator.start(REQ)

    assert saga["status"] == SagaStatus.COMPENSATED
    assert sum(c.endswith("/release") and c.startswith("bil") for c in services.calls) == 3


async def test_exhausted_retries_mark_compensation_failed(orchestrator, services):
    services.fail["dis POST /sessions"] = (503, "CHARGER_FAULT")
    services.fail["bil POST /holds/"] = (503, "UNAVAILABLE")  # siempre falla
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
    assert services.fund_releases == 2  # se llamó dos veces…
    details = [e["detail"] for e in again["events"] if e["action"] == "compensation"]
    assert "released=False" in details[-2]  # …pero el servicio solo liberó la primera


async def test_idempotency_key_reuses_the_same_saga(orchestrator, services):
    req = SagaRequest("bog-norte", "c1", "fleet-a", 40.0, idempotency_key="pedido-123")
    first = await orchestrator.start(req)
    second = await orchestrator.start(req)

    assert first["saga_id"] == second["saga_id"] == saga_id_for("pedido-123")
    assert sum(c.endswith(" /holds") for c in services.calls) == 1


# --- Liquidación ---------------------------------------------------------------------------
async def test_stop_settles_real_energy_and_frees_the_connector(orchestrator, services):
    saga = await orchestrator.start(REQ)
    done = await orchestrator.stop(saga["saga_id"])

    assert done["status"] == SagaStatus.FINISHED
    assert done["energy_kwh"] == 26.7
    assert (done["captured_cents"], done["released_cents"]) == (1200, 600)
    sid = saga["saga_id"]
    assert _paths(services)[-3:] == [
        f"POST /sessions/{sid}/stop",
        f"POST /holds/{sid}/capture",
        f"POST /reservations/{sid}/complete",
    ]
    # Detener otra vez no vuelve a liquidar.
    calls = len(services.calls)
    assert (await orchestrator.stop(sid))["status"] == SagaStatus.FINISHED
    assert len(services.calls) == calls


# --- Reserva anticipada: SAGA de larga duración -----------------------------------------------
async def test_reservation_ahead_waits_after_steps_1_and_2(orchestrator, services):
    saga = await orchestrator.start(AHEAD)
    assert saga["status"] == SagaStatus.AWAITING_ARRIVAL
    assert saga["arrival_deadline"]
    assert not any("/sessions" in c for c in services.calls)  # el cargador NO se activa aún


async def test_confirm_arrival_runs_step_3(orchestrator, services):
    saga = await orchestrator.start(AHEAD)
    active = await orchestrator.confirm_arrival(saga["saga_id"])
    assert active["status"] == SagaStatus.COMPLETED
    assert "POST /sessions" in _paths(services)


async def test_failure_after_arrival_compensates_steps_2_and_1(orchestrator, services):
    services.fail["dis POST /sessions"] = (503, "CHARGER_FAULT")
    saga = await orchestrator.start(AHEAD)
    result = await orchestrator.confirm_arrival(saga["saga_id"])
    assert result["status"] == SagaStatus.COMPENSATED
    assert _compensations(result) == [("charge", "OK"), ("reserve", "OK")]


async def test_cancel_releases_funds_and_bay(orchestrator, services):
    saga = await orchestrator.start(AHEAD)
    cancelled = await orchestrator.cancel(saga["saga_id"])
    assert cancelled["status"] == SagaStatus.COMPENSATED
    assert cancelled["failure_reason"] == "arrival:CANCELLED"
    assert _compensations(cancelled) == [("charge", "OK"), ("reserve", "OK")]


async def test_arrival_after_the_deadline_is_rejected(orchestrator, services):
    saga = await orchestrator.start(AHEAD)
    orchestrator.repo.sagas[saga["saga_id"]]["arrival_deadline"] = (
        datetime.now(UTC) - timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(ValueError, match="venció"):
        await orchestrator.confirm_arrival(saga["saga_id"])
    assert not any("/sessions" in c for c in services.calls)


# --- Supervisor -------------------------------------------------------------------------------
async def test_supervisor_expires_overdue_reservations(orchestrator, services):
    saga = await orchestrator.start(AHEAD)
    supervisor = SagaSupervisor(orchestrator, logging.getLogger("test"))

    assert await supervisor.tick() == {"expired": [], "finished": []}  # dentro del plazo
    later = datetime.now(UTC) + timedelta(minutes=10)
    assert (await supervisor.tick(now=later))["expired"] == [saga["saga_id"]]

    expired = await orchestrator.get(saga["saga_id"])
    assert expired["status"] == SagaStatus.COMPENSATED
    assert expired["failure_reason"] == "arrival:EXPIRED"


async def test_supervisor_finishes_charges_that_reached_their_target(orchestrator, services):
    saga = await orchestrator.start(REQ)
    supervisor = SagaSupervisor(orchestrator, logging.getLogger("test"))

    assert (await supervisor.tick())["finished"] == []  # aún cargando
    services.target_reached = True
    assert (await supervisor.tick())["finished"] == [saga["saga_id"]]

    finished = await orchestrator.get(saga["saga_id"])
    assert finished["status"] == SagaStatus.FINISHED
    assert finished["finish_reason"] == "target_reached"


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
