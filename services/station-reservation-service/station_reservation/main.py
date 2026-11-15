"""station-reservation-service: disponibilidad y bloqueo temporal de bahías de carga."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from aegis_common import Settings
from aegis_common.aws import client
from aegis_common.errors import FeatureDisabled, NotFound
from aegis_common.flags import FAST_CHARGE_BOOST, FlagStore
from aegis_common.logging import configure_logging, log_event
from aegis_common.web import install
from station_reservation.repository import ReservationRepository

SERVICE = "station-reservation-service"
ULTRA = "ULTRA"
ID = r"^[a-z0-9][a-z0-9-]{0,63}$"
SAGA_ID = r"^saga-[a-f0-9-]{8,64}$"


class ReserveRequest(BaseModel):
    saga_id: str = Field(pattern=SAGA_ID)
    station_id: str = Field(pattern=ID)
    connector_id: str = Field(pattern=ID)
    fleet_id: str = Field(pattern=ID)


class ReleaseRequest(BaseModel):
    reason: str = Field(default="compensation", max_length=200)


def create_app(settings: Settings | None = None, flags: FlagStore | None = None) -> FastAPI:
    settings = settings or Settings.from_env(service=SERVICE)
    log = configure_logging(settings)
    repo = ReservationRepository(client("dynamodb", settings), settings.table("reservations"))
    flags = flags or FlagStore(settings)

    app = FastAPI(title=SERVICE, version=settings.version)
    install(app, log)

    @app.get("/stations")
    def list_stations() -> list[dict[str, Any]]:
        return repo.list_stations()

    @app.get("/stations/{station_id}")
    def get_station(station_id: str) -> dict[str, Any]:
        return repo.get_station(station_id)

    @app.post("/reservations", status_code=201)
    def reserve(body: ReserveRequest) -> dict[str, Any]:
        connector = repo.get_connector(body.station_id, body.connector_id)
        if connector["type"] == ULTRA and not flags.enabled(FAST_CHARGE_BOOST):
            raise FeatureDisabled(
                "La ultra-carga (350 kW) requiere ENABLE_FAST_CHARGE_BOOST",
                code="FAST_CHARGE_DISABLED",
            )
        reservation = repo.reserve(body.saga_id, body.station_id, body.connector_id, body.fleet_id)
        log_event(
            log,
            "connector_reserved",
            station_id=body.station_id,
            connector_id=body.connector_id,
            fleet_id=body.fleet_id,
        )
        return reservation

    @app.get("/reservations/{saga_id}")
    def get_reservation(saga_id: str) -> dict[str, Any]:
        reservation = repo.get_reservation(saga_id)
        if reservation is None:
            raise NotFound(f"Reserva de la saga {saga_id} no existe")
        return reservation

    @app.post("/reservations/{saga_id}/occupy")
    def occupy(saga_id: str) -> dict[str, Any]:
        reservation = repo.occupy(saga_id)
        log_event(
            log,
            "connector_charging",
            station_id=reservation["station_id"],
            connector_id=reservation["connector_id"],
        )
        return reservation

    @app.post("/reservations/{saga_id}/release")
    def release(saga_id: str, body: ReleaseRequest) -> dict[str, Any]:
        """Compensación del paso 1. Idempotente."""
        result = repo.release(saga_id, body.reason)
        log_event(
            log,
            "connector_released",
            released=result["released"],
            detail=result.get("detail"),
            reason=body.reason,
        )
        return result

    @app.post("/reservations/{saga_id}/complete")
    def complete(saga_id: str) -> dict[str, Any]:
        result = repo.release(saga_id, "session_completed", completed=True)
        log_event(log, "connector_released", released=result["released"], reason="completed")
        return result

    app.state.repo = repo
    return app
