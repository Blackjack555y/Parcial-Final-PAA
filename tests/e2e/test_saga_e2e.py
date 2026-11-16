"""E2E: los tres escenarios de la SAGA, flags en caliente, WSS y trazabilidad en CloudWatch.

Todo pasa por la API pública del gateway (HTTP + WebSocket), como un cliente real.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import boto3
import pytest
import websockets

pytestmark = pytest.mark.e2e

START = """
mutation($i: ReserveChargeInput!) {
  reserveAndStartCharging(input: $i) {
    success
    saga { sagaId status failureReason amountCents correlationId
           events { step action outcome } }
  }
}"""
WALLET = """query($f: String!) {
  wallet(fleetId: $f) { balanceCents transactions { sagaId status refundReason } }
}"""
FAULT = "SIMULATE_CHARGER_HARDWARE_FAULT"


def _start(gw, key, fleet="fleet-rapidoexpress", connector_type="CCS2"):
    station, connector = gw.free_connector(connector_type)
    data = gw.gql(
        START,
        {
            "i": {
                "stationId": station,
                "connectorId": connector,
                "fleetId": fleet,
                "kwh": 20,
                "idempotencyKey": key,
            }
        },
    )
    return data["reserveAndStartCharging"]["saga"], station, connector


def _balance(gw, fleet="fleet-rapidoexpress") -> int:
    return gw.gql(WALLET, {"f": fleet})["wallet"]["balanceCents"]


def test_endpoint_reports_its_environment(gateway):
    env = gateway.gql("{ environment { name version } }")["environment"]
    assert env["name"] == gateway.env


def test_response_carries_correlation_id(gateway):
    response = gateway.http.post("/graphql", json={"query": "{ environment { name } }"})
    assert response.headers["x-correlation-id"].startswith("corr-")


def test_happy_path_charges_streams_telemetry_and_stops(gateway, unique_key):
    gateway.set_flag(FAULT, False)
    before = _balance(gateway)
    saga, station, connector = _start(gateway, unique_key)
    try:
        assert saga["status"] == "COMPLETED", saga
        assert _balance(gateway) == before - saga["amountCents"]
        telemetry = gateway.gql(
            "query($s: String!) { telemetry(sagaId: $s) { status energyKwh } }",
            {"s": saga["sagaId"]},
        )["telemetry"]
        assert telemetry["status"] == "ACTIVE"
    finally:
        stopped = gateway.gql(
            "mutation($s: String!) { stopCharging(sagaId: $s) { saga { status } } }",
            {"s": saga["sagaId"]},
        )
    assert stopped["stopCharging"]["saga"]["status"] == "FINISHED"
    station_data = gateway.gql(
        "query($s: String!) { station(stationId: $s) { connectors { connectorId status } } }",
        {"s": station},
    )["station"]
    status = {c["connectorId"]: c["status"] for c in station_data["connectors"]}
    assert status[connector] == "AVAILABLE"


def test_charger_fault_rolls_back_billing_and_reservation(gateway, unique_key):
    before = _balance(gateway)
    gateway.set_flag(FAULT, True)
    try:
        saga, _, _ = _start(gateway, unique_key)
    finally:
        gateway.set_flag(FAULT, False)

    assert saga["status"] == "COMPENSATED"
    assert saga["failureReason"] == "dispatch:CHARGER_FAULT"
    assert [(e["step"], e["action"]) for e in saga["events"] if e["action"] == "compensation"] == [
        ("charge", "compensation"),
        ("reserve", "compensation"),
    ]
    assert _balance(gateway) == before
    txns = gateway.gql(WALLET, {"f": "fleet-rapidoexpress"})["wallet"]["transactions"]
    txn = next(t for t in txns if t["sagaId"] == saga["sagaId"])
    assert txn["status"] == "RELEASED"  # la retención se liberó (compensación del paso 2)

    # Trazabilidad: el correlation_id de la saga fallida está en CloudWatch (pregunta 6.4).
    _assert_in_cloudwatch(gateway.env, saga["correlationId"])


def test_reservation_ahead_holds_the_bay_until_cancelled(gateway, unique_key):
    before = _balance(gateway)
    station, connector = gateway.free_connector()
    data = gateway.gql(
        START,
        {
            "i": {
                "stationId": station,
                "connectorId": connector,
                "fleetId": "fleet-rapidoexpress",
                "kwh": 20,
                "idempotencyKey": unique_key,
                "arrivalMinutes": 5,
            }
        },
    )
    saga = data["reserveAndStartCharging"]["saga"]
    assert saga["status"] == "AWAITING_ARRIVAL"
    assert _balance(gateway) == before - saga["amountCents"]  # saldo retenido

    cancelled = gateway.gql(
        "mutation($s: String!) { cancelReservation(sagaId: $s) { saga { status } } }",
        {"s": saga["sagaId"]},
    )
    assert cancelled["cancelReservation"]["saga"]["status"] == "COMPENSATED"
    assert _balance(gateway) == before  # retención liberada


def test_insufficient_funds_releases_connector(gateway, unique_key):
    saga, station, connector = _start(gateway, unique_key, fleet="fleet-sinfondos")
    assert saga["status"] == "COMPENSATED"
    assert saga["failureReason"] == "charge:INSUFFICIENT_FUNDS"
    assert gateway.free_connector() is not None


def test_feature_flag_changes_are_pushed_over_websocket(gateway):
    ws_url = gateway.url.replace("http", "ws", 1) + "/graphql"
    flag = "ENABLE_DYNAMIC_SURGE_PRICING"
    original = next(
        f["enabled"]
        for f in gateway.gql("{ featureFlags { name enabled } }")["featureFlags"]
        if f["name"] == flag
    )

    async def scenario() -> list[bool]:
        seen: list[bool] = []
        async with websockets.connect(ws_url, subprotocols=["graphql-transport-ws"]) as ws:
            await ws.send(json.dumps({"type": "connection_init"}))
            assert json.loads(await ws.recv())["type"] == "connection_ack"
            query = "subscription { featureFlags { name enabled } }"
            await ws.send(json.dumps({"id": "1", "type": "subscribe", "payload": {"query": query}}))

            async def next_value() -> bool:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
                flags = msg["payload"]["data"]["featureFlags"]
                return next(f["enabled"] for f in flags if f["name"] == flag)

            seen.append(await next_value())
            await asyncio.to_thread(
                gateway.gql,
                "mutation($n: String!, $e: Boolean!) "
                "{ setFeatureFlag(name: $n, enabled: $e) { name } }",
                {"n": flag, "e": not original},
                admin=True,
            )
            seen.append(await next_value())
        return seen

    try:
        assert asyncio.run(scenario()) == [original, not original]
    finally:
        gateway.set_flag(flag, original)


def _assert_in_cloudwatch(env: str, correlation_id: str) -> None:
    logs = boto3.client(
        "logs", endpoint_url=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
    )
    deadline = time.monotonic() + 20  # los servicios envían lotes cada 2 s
    found: set[str] = set()
    while time.monotonic() < deadline:
        for svc in ("graphql-gateway", "billing-wallet-service", "energy-dispatch-service"):
            events = logs.filter_log_events(
                logGroupName=f"/aegisvolt/{env}/{svc}",
                filterPattern=f'{{ $.correlation_id = "{correlation_id}" }}',
            )["events"]
            if events:
                found.add(svc)
        if len(found) == 3:
            return
        time.sleep(2)
    pytest.fail(f"correlation_id {correlation_id} solo apareció en {sorted(found)}")
