"""billing-wallet-service: retención, liquidación y liberación de fondos de la flota.

POST /holds                     paso 2 de la SAGA: retiene el monto estimado
POST /holds/{saga}/release      compensación: libera la retención (idempotente)
POST /holds/{saga}/capture      fin de la carga: cobra la energía real y libera el resto
(alias anteriores: POST /charges = /holds · POST /charges/{saga}/refund = /release)
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

from aegis_common import Settings
from aegis_common.aws import client
from aegis_common.flags import SURGE_PRICING, FlagStore
from aegis_common.logging import configure_logging, log_event
from aegis_common.web import install
from billing_wallet.pricing import local_now, parse_peak_hours, quote
from billing_wallet.repository import BillingRepository

SERVICE = "billing-wallet-service"
ID = r"^[a-z0-9][a-z0-9-]{0,63}$"
SAGA_ID = r"^saga-[a-f0-9-]{8,64}$"
CONNECTOR_TYPE = r"^(CCS2|ULTRA)$"


class ChargeRequest(BaseModel):
    saga_id: str = Field(pattern=SAGA_ID)
    fleet_id: str = Field(pattern=ID)
    connector_type: str = Field(pattern=CONNECTOR_TYPE)
    kwh: float = Field(gt=0, le=500)


class ReleaseRequest(BaseModel):
    fleet_id: str = Field(pattern=ID)
    reason: str = Field(default="compensation", max_length=200)


class CaptureRequest(BaseModel):
    fleet_id: str = Field(pattern=ID)
    energy_kwh: float = Field(ge=0, le=500)


class TopUpRequest(BaseModel):
    amount_cents: int = Field(gt=0, le=10_000_000)


def create_app(settings: Settings | None = None, flags: FlagStore | None = None) -> FastAPI:
    settings = settings or Settings.from_env(service=SERVICE)
    log = configure_logging(settings)
    repo = BillingRepository(client("dynamodb", settings), settings.table("billing"))
    flags = flags or FlagStore(settings)
    peak_hours = parse_peak_hours(os.environ.get("PEAK_HOURS", "17-22"))
    surge_multiplier = float(os.environ.get("SURGE_MULTIPLIER", "1.5"))
    pricing_tz = os.environ.get("PRICING_TZ", "America/Bogota")

    def current_quote(connector_type: str, kwh: float):
        return quote(
            connector_type,
            kwh,
            surge_flag=flags.enabled(SURGE_PRICING),
            now=local_now(pricing_tz),
            peak_hours=peak_hours,
            surge_multiplier=surge_multiplier,
        )

    app = FastAPI(title=SERVICE, version=settings.version)
    install(app, log)

    @app.get("/quotes")
    def get_quote(
        connector_type: str = Query(pattern=CONNECTOR_TYPE),
        kwh: float = Query(gt=0, le=500),
    ) -> dict[str, Any]:
        return current_quote(connector_type, kwh).as_dict()

    @app.get("/wallets/{fleet_id}")
    def get_wallet(fleet_id: str) -> dict[str, Any]:
        return repo.get_wallet(fleet_id)

    @app.get("/wallets/{fleet_id}/transactions")
    def list_transactions(fleet_id: str) -> list[dict[str, Any]]:
        return repo.list_transactions(fleet_id)

    @app.post("/wallets/{fleet_id}/topup")
    def top_up(fleet_id: str, body: TopUpRequest) -> dict[str, Any]:
        wallet = repo.top_up(fleet_id, body.amount_cents)
        log_event(log, "wallet_topped_up", fleet_id=fleet_id, amount_cents=body.amount_cents)
        return wallet

    @app.post("/holds", status_code=201)
    @app.post("/charges", status_code=201, include_in_schema=False)
    def hold(body: ChargeRequest) -> dict[str, Any]:
        q = current_quote(body.connector_type, body.kwh)
        txn = repo.hold(body.saga_id, body.fleet_id, q)
        log_event(
            log,
            "funds_held",
            fleet_id=body.fleet_id,
            amount_cents=txn["amount_cents"],
            surge_active=txn.get("surge_active"),
        )
        return txn

    @app.post("/holds/{saga_id}/release")
    @app.post("/charges/{saga_id}/refund", include_in_schema=False)
    def release(saga_id: str, body: ReleaseRequest) -> dict[str, Any]:
        """Compensación del paso 2. Idempotente."""
        result = repo.release(saga_id, body.fleet_id, body.reason)
        log_event(
            log,
            "funds_released",
            fleet_id=body.fleet_id,
            released=result["released"],
            detail=result.get("detail"),
            amount_cents=result.get("amount_cents"),
        )
        return result

    @app.post("/holds/{saga_id}/capture")
    def capture(saga_id: str, body: CaptureRequest) -> dict[str, Any]:
        """Liquidación al terminar la carga: cobra lo entregado, devuelve la diferencia."""
        result = repo.capture(saga_id, body.fleet_id, body.energy_kwh)
        log_event(
            log,
            "funds_captured",
            fleet_id=body.fleet_id,
            energy_kwh=body.energy_kwh,
            captured_cents=result.get("captured_cents"),
            released_cents=result.get("released_cents"),
            detail=result.get("detail"),
        )
        return result

    app.state.repo = repo
    return app
