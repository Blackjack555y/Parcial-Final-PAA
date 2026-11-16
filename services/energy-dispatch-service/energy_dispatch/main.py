"""energy-dispatch-service: orden de activación física del cargador y registro de telemetría.

Paso 3 de la SAGA. Si el cargador reporta una falla física, o si la flag
SIMULATE_CHARGER_HARDWARE_FAULT está activa en el ambiente, la sesión queda FAILED y la
respuesta es 503 CHARGER_FAULT: el orquestador dispara entonces las compensaciones
(reembolso en billing → liberación del conector en reservations).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

from aegis_common import Settings
from aegis_common.aws import client
from aegis_common.errors import ChargerFault
from aegis_common.flags import SIMULATE_CHARGER_FAULT, FlagStore
from aegis_common.logging import configure_logging, log_event
from aegis_common.web import install
from energy_dispatch.repository import ACTIVE, FAILED, DispatchRepository
from energy_dispatch.telemetry import sample

SERVICE = "energy-dispatch-service"
ID = r"^[a-z0-9][a-z0-9-]{0,63}$"
SAGA_ID = r"^saga-[a-f0-9-]{8,64}$"


class StartRequest(BaseModel):
    saga_id: str = Field(pattern=SAGA_ID)
    station_id: str = Field(pattern=ID)
    connector_id: str = Field(pattern=ID)
    power_kw: float = Field(gt=0, le=400)
    target_kwh: float = Field(gt=0, le=500)


def _elapsed(session: dict[str, Any], speedup: float) -> float:
    """Segundos SIMULADOS de carga (tiempo real × factor de aceleración)."""
    started = datetime.fromisoformat(session["started_at"])
    return (datetime.now(UTC) - started).total_seconds() * speedup


def create_app(settings: Settings | None = None, flags: FlagStore | None = None) -> FastAPI:
    settings = settings or Settings.from_env(service=SERVICE)
    log = configure_logging(settings)
    repo = DispatchRepository(client("dynamodb", settings), settings.table("dispatch"))
    flags = flags or FlagStore(settings)
    # Simulación acelerada (dev/test = 20): 30 kWh a 150 kW toman ~12 min reales y ~40 s en demo.
    speedup = max(1.0, float(os.environ.get("CHARGING_SIMULATION_SPEEDUP", "1")))

    app = FastAPI(title=SERVICE, version=settings.version)
    install(app, log)

    @app.post("/sessions", status_code=201)
    def start_session(body: StartRequest) -> dict[str, Any]:
        fault = flags.enabled(SIMULATE_CHARGER_FAULT)
        session, created = repo.create_session(
            body.saga_id,
            status=FAILED if fault else ACTIVE,
            fields={
                "station_id": body.station_id,
                "connector_id": body.connector_id,
                "power_kw": str(body.power_kw),
                "target_kwh": str(body.target_kwh),
                **({"failure_reason": "SIMULATE_CHARGER_HARDWARE_FAULT"} if fault else {}),
            },
        )
        if session["status"] == FAILED:
            if created:
                log_event(
                    log,
                    "charger_fault",
                    "El cargador reportó una falla física",
                    level=logging.ERROR,
                    station_id=body.station_id,
                    connector_id=body.connector_id,
                    reason=session.get("failure_reason"),
                )
            raise ChargerFault(
                f"Falla del cargador {body.station_id}/{body.connector_id}: "
                f"{session.get('failure_reason', 'hardware')}"
            )
        if created:
            log_event(
                log,
                "charging_started",
                station_id=body.station_id,
                connector_id=body.connector_id,
                power_kw=body.power_kw,
            )
        return session

    @app.get("/sessions")
    def list_sessions(
        status: str | None = Query(default=None, pattern="^(ACTIVE|FAILED|COMPLETED)$"),
    ):
        return repo.list_sessions(status)

    @app.get("/sessions/{saga_id}")
    def get_session(saga_id: str) -> dict[str, Any]:
        return repo.require_session(saga_id)

    @app.get("/sessions/{saga_id}/telemetry")
    def telemetry(saga_id: str) -> dict[str, Any]:
        session = repo.require_session(saga_id)
        if session["status"] == ACTIVE:
            target = float(session["target_kwh"])
            data = sample(_elapsed(session, speedup), float(session["power_kw"]), target).as_dict()
            data["target_kwh"] = target
            data["target_reached"] = data["energy_kwh"] >= target - 1e-6
            repo.put_telemetry(saga_id, data)  # registro histórico (TTL 24 h)
        else:
            data = {
                "elapsed_s": 0.0,
                "power_kw": 0.0,
                "energy_kwh": float(session.get("energy_kwh", 0.0)),
                "soc_percent": 0.0,
                "voltage_v": 0.0,
                "current_a": 0.0,
                "target_kwh": float(session.get("target_kwh", 0)),
                "target_reached": session["status"] == "COMPLETED",
            }
        return {"saga_id": saga_id, "status": session["status"], **data}

    @app.post("/sessions/{saga_id}/stop")
    def stop(saga_id: str) -> dict[str, Any]:
        session = repo.require_session(saga_id)
        energy = 0.0
        if session["status"] == ACTIVE:
            energy = sample(
                _elapsed(session, speedup), float(session["power_kw"]), float(session["target_kwh"])
            ).energy_kwh
        result = repo.complete(saga_id, energy)
        log_event(log, "charging_stopped", energy_kwh=energy)
        return result

    app.state.repo = repo
    return app
