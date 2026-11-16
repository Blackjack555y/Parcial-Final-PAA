"""SAGA de punta a punta por GraphQL contra DynamoDB (Floci).

Verifica en las TABLAS de cada servicio el efecto de cada paso y de cada compensación
(lo mismo que se muestra en la sustentación, pregunta 6.3).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

START = """
mutation Start($input: ReserveChargeInput!) {
  reserveAndStartCharging(input: $input) {
    success message
    saga { sagaId status failureReason amountCents correlationId
           events { step action outcome detail } }
  }
}"""
WALLET = "query($f: String!) { wallet(fleetId: $f) { balanceCents transactions { status } } }"
SET_FLAG = """
mutation($n: String!, $e: Boolean!) { setFeatureFlag(name: $n, enabled: $e) { name enabled } }
"""
RETRY = "mutation($s: String!) { retryCompensation(sagaId: $s) { success saga { status } } }"
STOP = """mutation($s: String!) { stopCharging(sagaId: $s) {
  success saga { status energyKwh capturedCents releasedCents } } }"""
CONFIRM = "mutation($s: String!) { confirmArrival(sagaId: $s) { success saga { status } } }"
CANCEL = "mutation($s: String!) { cancelReservation(sagaId: $s) { saga { status failureReason } } }"
SAGA = """query($s: String!) { saga(sagaId: $s) {
  status failureReason finishReason energyKwh capturedCents releasedCents } }"""


def _input(fleet="fleet-rapidoexpress", connector="c1", station="bog-norte", **extra):
    return {
        "input": {
            "stationId": station,
            "connectorId": connector,
            "fleetId": fleet,
            "kwh": 40,
            **extra,
        }
    }


async def _balance(p, fleet="fleet-rapidoexpress") -> int:
    return (await p.gql(WALLET, {"f": fleet}))["data"]["wallet"]["balanceCents"]


def _steps(saga):
    return [(e["step"], e["action"], e["outcome"]) for e in saga["events"]]


async def test_happy_path_holds_then_settles_real_energy(platform):
    p = platform
    before = await _balance(p)
    result = (await p.gql(START, _input()))["data"]["reserveAndStartCharging"]
    saga = result["saga"]

    assert result["success"] is True
    assert saga["status"] == "COMPLETED"
    assert saga["amountCents"] == 2720  # RETENIDO: 40 kWh × 68 ¢ (tarifa pico activa en dev)
    assert saga["correlationId"].startswith("corr-")
    assert await _balance(p) == before - 2720  # el saldo disponible baja por la retención

    sid = saga["sagaId"]
    assert p.item("billing", "WALLET#fleet-rapidoexpress", f"TXN#{sid}")["status"] == "HELD"
    assert p.item("reservations", "STATION#bog-norte", "CONNECTOR#c1")["status"] == "CHARGING"
    assert p.item("dispatch", f"SESSION#{sid}")["status"] == "ACTIVE"

    stopped = (await p.gql(STOP, {"s": sid}))["data"]["stopCharging"]["saga"]
    assert stopped["status"] == "FINISHED"
    captured, released = stopped["capturedCents"], stopped["releasedCents"]
    assert captured + released == 2720  # se cobra lo entregado y se libera el resto
    assert captured == round(stopped["energyKwh"] * 68)
    txn = p.item("billing", "WALLET#fleet-rapidoexpress", f"TXN#{sid}")
    assert txn["status"] == "CAPTURED" and txn["captured_cents"] == captured
    assert await _balance(p) == before - captured
    assert p.item("reservations", "STATION#bog-norte", "CONNECTOR#c1")["status"] == "AVAILABLE"
    assert p.item("reservations", f"RESERVATION#{sid}")["status"] == "COMPLETED"


async def test_supervisor_finishes_the_charge_when_the_target_is_reached(platform):
    p = platform
    before = await _balance(p, "fleet-ultimamilla")
    saga = (await p.gql(START, _input(fleet="fleet-ultimamilla", station="cali-sur")))["data"][
        "reserveAndStartCharging"
    ]["saga"]
    assert saga["status"] == "COMPLETED"

    import asyncio

    for _ in range(20):  # simulación ×2000: 40 kWh se entregan en ~1 s
        if (await p.supervisor.tick())["finished"]:
            break
        await asyncio.sleep(0.3)

    done = (await p.gql(SAGA, {"s": saga["sagaId"]}))["data"]["saga"]
    assert done["status"] == "FINISHED"
    assert done["finishReason"] == "target_reached"
    assert done["energyKwh"] == 40  # se entregó TODO lo pedido…
    assert done["capturedCents"] == 2720 and done["releasedCents"] == 0  # …y se cobra todo
    assert await _balance(p, "fleet-ultimamilla") == before - 2720
    assert p.item("reservations", "STATION#cali-sur", "CONNECTOR#c1")["status"] == "AVAILABLE"


async def test_reservation_ahead_then_arrival(platform):
    p = platform
    ahead = (await p.gql(START, _input(station="med-poblado", arrivalMinutes=5)))["data"][
        "reserveAndStartCharging"
    ]
    saga = ahead["saga"]
    sid = saga["sagaId"]
    assert ahead["success"] is True and saga["status"] == "AWAITING_ARRIVAL"
    connector = p.item("reservations", "STATION#med-poblado", "CONNECTOR#c1")
    assert connector["status"] == "RESERVED" and connector["reserved_until"]
    assert p.item("billing", "WALLET#fleet-rapidoexpress", f"TXN#{sid}")["status"] == "HELD"
    assert p.item("dispatch", f"SESSION#{sid}") is None  # el cargador aún no se activa

    arrived = (await p.gql(CONFIRM, {"s": sid}))["data"]["confirmArrival"]
    assert arrived["saga"]["status"] == "COMPLETED"
    assert p.item("reservations", "STATION#med-poblado", "CONNECTOR#c1")["status"] == "CHARGING"


async def test_reservation_ahead_can_be_cancelled(platform):
    p = platform
    before = await _balance(p)
    saga = (await p.gql(START, _input(station="cali-sur", connector="c2", arrivalMinutes=10)))[
        "data"
    ]["reserveAndStartCharging"]["saga"]
    cancelled = (await p.gql(CANCEL, {"s": saga["sagaId"]}))["data"]["cancelReservation"]["saga"]

    assert cancelled["status"] == "COMPENSATED"
    assert cancelled["failureReason"] == "arrival:CANCELLED"
    assert await _balance(p) == before  # retención liberada
    assert p.item("reservations", "STATION#cali-sur", "CONNECTOR#c2")["status"] == "AVAILABLE"


async def test_reservation_service_expires_bays_on_its_own(platform):
    """Defensa en profundidad: aunque el orquestador no actúe, la bahía se libera sola."""
    import time

    p = platform
    saga = (await p.gql(START, _input(connector="c2", arrivalMinutes=1)))["data"][
        "reserveAndStartCharging"
    ]["saga"]
    assert p.item("reservations", "STATION#bog-norte", "CONNECTOR#c2")["status"] == "RESERVED"

    expired = p.reservations_repo.expire_overdue(now=time.time() + 3600)
    assert saga["sagaId"] in expired
    assert p.item("reservations", "STATION#bog-norte", "CONNECTOR#c2")["status"] == "AVAILABLE"
    assert p.item("reservations", f"RESERVATION#{saga['sagaId']}")["reason"] == "expired"


async def test_charger_fault_compensates_billing_then_reservation(platform):
    p = platform
    before = await _balance(p)
    flag = await p.gql(SET_FLAG, {"n": "SIMULATE_CHARGER_HARDWARE_FAULT", "e": True}, admin=True)
    assert flag["data"]["setFeatureFlag"]["enabled"] is True

    result = (await p.gql(START, _input(connector="c2")))["data"]["reserveAndStartCharging"]
    saga = result["saga"]
    sid = saga["sagaId"]

    assert result["success"] is False
    assert saga["status"] == "COMPENSATED"
    assert saga["failureReason"] == "dispatch:CHARGER_FAULT"
    assert _steps(saga) == [
        ("reserve", "action", "OK"),
        ("charge", "action", "OK"),
        ("dispatch", "action", "FAILED"),
        ("charge", "compensation", "OK"),  # 1.º se reembolsa…
        ("reserve", "compensation", "OK"),  # …2.º se libera el conector
    ]
    # Efecto en las tablas de cada servicio:
    txn = p.item("billing", "WALLET#fleet-rapidoexpress", f"TXN#{sid}")
    assert txn["status"] == "RELEASED" and txn["refund_reason"] == "dispatch:CHARGER_FAULT"
    assert await _balance(p) == before  # retención liberada
    assert p.item("reservations", f"RESERVATION#{sid}")["status"] == "RELEASED"
    assert p.item("reservations", "STATION#bog-norte", "CONNECTOR#c2")["status"] == "AVAILABLE"
    assert p.item("dispatch", f"SESSION#{sid}")["status"] == "FAILED"

    # Idempotencia: re-ejecutar las compensaciones NO reembolsa dos veces.
    retry = (await p.gql(RETRY, {"s": sid}, admin=True))["data"]["retryCompensation"]
    assert retry["saga"]["status"] == "COMPENSATED"
    assert await _balance(p) == before


async def test_insufficient_funds_releases_connector_only(platform):
    p = platform
    result = (await p.gql(START, _input(fleet="fleet-sinfondos", station="cali-sur")))["data"][
        "reserveAndStartCharging"
    ]
    saga = result["saga"]

    assert saga["status"] == "COMPENSATED"
    assert saga["failureReason"] == "charge:INSUFFICIENT_FUNDS"
    assert _steps(saga) == [
        ("reserve", "action", "OK"),
        ("charge", "action", "FAILED"),
        ("reserve", "compensation", "OK"),
    ]
    assert await _balance(p, "fleet-sinfondos") == 500
    assert p.item("reservations", "STATION#cali-sur", "CONNECTOR#c1")["status"] == "AVAILABLE"


async def test_fast_charge_flag_controls_ultra_connectors(platform):
    p = platform
    await p.gql(SET_FLAG, {"n": "ENABLE_FAST_CHARGE_BOOST", "e": False}, admin=True)
    blocked = (await p.gql(START, _input(connector="c3")))["data"]["reserveAndStartCharging"]
    assert blocked["saga"]["status"] == "FAILED"  # falla el paso 1: nada que compensar
    assert "FAST_CHARGE_DISABLED" in blocked["saga"]["failureReason"]

    await p.gql(SET_FLAG, {"n": "ENABLE_FAST_CHARGE_BOOST", "e": True}, admin=True)
    allowed = (await p.gql(START, _input(connector="c3")))["data"]["reserveAndStartCharging"]
    assert allowed["saga"]["status"] == "COMPLETED"


async def test_idempotency_key_charges_only_once(platform):
    p = platform
    before = await _balance(p)
    first = await p.gql(START, _input(station="med-poblado", idempotencyKey="orden-777"))
    second = await p.gql(START, _input(station="med-poblado", idempotencyKey="orden-777"))
    s1 = first["data"]["reserveAndStartCharging"]["saga"]
    s2 = second["data"]["reserveAndStartCharging"]["saga"]

    assert s1["sagaId"] == s2["sagaId"]
    assert await _balance(p) == before - s1["amountCents"]  # una sola retención


async def test_admin_mutations_require_token(platform):
    result = await platform.gql(SET_FLAG, {"n": "SIMULATE_CHARGER_HARDWARE_FAULT", "e": True})
    assert result["data"] is None
    assert "X-Admin-Token" in result["errors"][0]["message"]


async def test_occupied_connector_cannot_be_double_booked(platform):
    p = platform
    first = (await p.gql(START, _input(station="cali-sur", connector="c2")))["data"]
    assert first["reserveAndStartCharging"]["saga"]["status"] == "COMPLETED"
    second = (await p.gql(START, _input(station="cali-sur", connector="c2")))["data"]
    saga = second["reserveAndStartCharging"]["saga"]
    assert saga["status"] == "FAILED"
    assert "CONNECTOR_UNAVAILABLE" in saga["failureReason"]
