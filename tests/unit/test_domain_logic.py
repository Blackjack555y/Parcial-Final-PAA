"""Lógica pura de los servicios: tarifas, telemetría, logs JSON y esquema GraphQL."""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime

import pytest

from aegis_common import Environment, Settings
from aegis_common.context import correlation_id_var, saga_id_var
from aegis_common.logging import JsonFormatter, log_event
from billing_wallet.pricing import is_peak, parse_peak_hours, quote
from energy_dispatch.telemetry import sample
from graphql_gateway.main import build_schema

# --- Tarifas -----------------------------------------------------------------------------
PEAK = (17, 22)
AT_PEAK = datetime(2026, 11, 15, 18, 0)
OFF_PEAK = datetime(2026, 11, 15, 10, 0)


def _q(**kw):
    defaults = dict(surge_flag=True, now=AT_PEAK, peak_hours=PEAK, surge_multiplier=1.5)
    return quote("CCS2", 40, **{**defaults, **kw})


def test_surge_applies_only_with_flag_and_peak_hour():
    assert _q().surge_active is True
    assert _q(surge_flag=False).surge_active is False
    assert _q(now=OFF_PEAK).surge_active is False


def test_surge_multiplies_price():
    base = _q(surge_flag=False)
    surge = _q()
    assert base.unit_price_cents == 45 and base.amount_cents == 1800
    assert surge.unit_price_cents == 68 and surge.amount_cents == 2720  # 45 × 1,5 = 67,5 → 68


def test_ultra_is_more_expensive():
    assert (
        quote(
            "ULTRA",
            40,
            **dict(surge_flag=False, now=OFF_PEAK, peak_hours=PEAK, surge_multiplier=1.5),
        ).amount_cents
        == 2600
    )


@pytest.mark.parametrize("bad", ["", "17", "22-17", "0-25", "a-b"])
def test_invalid_peak_hours(bad):
    with pytest.raises(ValueError):
        parse_peak_hours(bad)


def test_full_day_peak_window():
    assert all(is_peak(datetime(2026, 1, 1, h), parse_peak_hours("0-24")) for h in range(24))


@pytest.mark.parametrize("kwh", [0, -1, 501])
def test_invalid_kwh(kwh):
    with pytest.raises(ValueError):
        quote("CCS2", kwh, surge_flag=False, now=OFF_PEAK, peak_hours=PEAK, surge_multiplier=1)


# --- Telemetría --------------------------------------------------------------------------
def test_telemetry_ramps_up_then_delivers_energy():
    start = sample(0, 150, 40)
    ramping = sample(15, 150, 40)
    full = sample(120, 150, 40)
    assert start.energy_kwh == 0
    assert 0 < ramping.power_kw < 150
    assert full.power_kw == pytest.approx(150, rel=0.01)
    assert full.energy_kwh > ramping.energy_kwh


def test_telemetry_stops_at_target_energy():
    done = sample(3600, 150, 10)
    assert done.energy_kwh == 10
    assert done.power_kw == 0


# --- Logs JSON ---------------------------------------------------------------------------
def test_json_log_contains_correlation_and_saga_ids():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("test", "billing-wallet-service", "v1.2.3"))
    logger = logging.getLogger("test-json")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    c, s = correlation_id_var.set("corr-1"), saga_id_var.set("saga-1")
    try:
        log_event(logger, "wallet_refunded", amount_cents=1800, service="intruso")
    finally:
        correlation_id_var.reset(c)
        saga_id_var.reset(s)
    line = json.loads(stream.getvalue())
    assert line["event"] == "wallet_refunded"
    assert line["correlation_id"] == "corr-1"
    assert line["saga_id"] == "saga-1"
    assert line["env"] == "test"
    assert line["service"] == "billing-wallet-service"
    assert line["version"] == "v1.2.3"
    assert line["amount_cents"] == 1800
    assert line["service_field"] == "intruso"  # un campo del evento no pisa el campo fijo


def test_settings_log_group_and_table_prefix():
    s = Settings(env=Environment.PROD, service="graphql-gateway", table_prefix="it1234")
    assert s.log_group == "/aegisvolt/prod/graphql-gateway"
    assert s.table("sagas") == "it1234-sagas-prod"


# --- Esquema GraphQL ---------------------------------------------------------------------
INTROSPECTION = "{ __schema { queryType { name } } }"


def test_introspection_disabled_in_prod_only():
    assert build_schema(Environment.DEV).execute_sync(INTROSPECTION).errors is None
    assert build_schema(Environment.PROD).execute_sync(INTROSPECTION).errors


async def test_alias_flooding_is_rejected():
    flood = "{ " + " ".join(f"a{i}: environment {{ name }}" for i in range(20)) + " }"
    result = await build_schema(Environment.DEV).execute(flood, context_value={})
    assert result.errors and "aliases found" in str(result.errors[0])


async def test_internal_errors_are_masked():
    # Sin contexto, el resolver falla con un TypeError interno: el cliente no debe verlo.
    result = await build_schema(Environment.PROD).execute("{ sagas { sagaId } }")
    assert result.errors[0].message == "Unexpected error."


# --- Inyección de fallos (demo del rollback del canary) ----------------------------------
def test_chaos_injection_returns_500_and_spares_health(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis_common.web import install

    monkeypatch.setenv("CHAOS_ERROR_RATE", "1")
    app = FastAPI()
    install(app, logging.getLogger("chaos"))

    @app.get("/ping")
    def ping():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/ping").status_code == 500
    assert client.get("/healthz").status_code == 200  # el canary no muere por health checks


def test_chaos_is_off_by_default(monkeypatch):
    from aegis_common.web import _chaos_rate

    monkeypatch.delenv("CHAOS_ERROR_RATE", raising=False)
    assert _chaos_rate() == 0.0
    monkeypatch.setenv("CHAOS_ERROR_RATE", "basura")
    assert _chaos_rate() == 0.0
