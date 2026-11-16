"""Reglas de auditoría de la SAGA (observability/prefect/aegis_flows/rules.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aegis_flows.rules import Snapshot, audit, health_verdict, summarize

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
OLD = (NOW - timedelta(minutes=10)).isoformat()
FRESH = (NOW - timedelta(seconds=5)).isoformat()


def _snap(**kw) -> Snapshot:
    base = dict(sagas={}, reservations={}, connectors=[], charges={}, sessions={})
    base.update(kw)
    return Snapshot(**base)


def _rules(snap: Snapshot) -> set[tuple[str, str]]:
    return {(f.severity, f.rule) for f in audit(snap, NOW)}


def test_consistent_compensated_saga_only_reports_info():
    snap = _snap(
        sagas={"s1": {"status": "COMPENSATED", "updated_at": OLD}},
        reservations={"s1": {"status": "RELEASED"}},
        charges={"s1": {"status": "REFUNDED"}},
        sessions={"s1": {"status": "FAILED"}},
    )
    assert _rules(snap) == {("INFO", "saga_compensada")}
    assert summarize(snap, audit(snap, NOW))["consistente"] is True


def test_refund_missing_is_critical():
    snap = _snap(
        sagas={"s1": {"status": "COMPENSATED"}},
        charges={"s1": {"status": "CHARGED", "amount_cents": 1800}},
    )
    assert ("CRITICAL", "cobro_sin_reembolso") in _rules(snap)


def test_connector_still_held_after_rollback():
    snap = _snap(
        sagas={"s1": {"status": "FAILED"}},
        reservations={"s1": {"status": "RESERVED"}},
    )
    assert ("WARNING", "conector_retenido") in _rules(snap)


def test_stuck_saga_depends_on_age():
    stuck = _snap(sagas={"s1": {"status": "CHARGED", "updated_at": OLD}})
    fresh = _snap(sagas={"s1": {"status": "CHARGED", "updated_at": FRESH}})
    assert ("WARNING", "saga_atascada") in _rules(stuck)
    assert ("WARNING", "saga_atascada") not in _rules(fresh)  # en curso, no atascada


def test_compensation_failed_is_critical():
    snap = _snap(sagas={"s1": {"status": "COMPENSATION_FAILED", "failure_reason": "x"}})
    assert ("CRITICAL", "compensacion_fallida") in _rules(snap)


def test_completed_saga_requires_active_session():
    snap = _snap(
        sagas={"s1": {"status": "COMPLETED"}},
        reservations={"s1": {"status": "CHARGING"}},
        sessions={"s1": {"status": "FAILED"}},
    )
    assert ("CRITICAL", "carga_sin_sesion") in _rules(snap)


def test_active_session_without_active_saga():
    snap = _snap(
        sagas={"s1": {"status": "COMPENSATED"}},
        sessions={"s1": {"status": "ACTIVE"}, "ghost": {"status": "ACTIVE"}},
    )
    rules = {(f.rule, f.saga_id) for f in audit(snap, NOW)}
    assert ("sesion_activa_huerfana", "s1") in rules
    assert ("sesion_activa_huerfana", "ghost") in rules


def test_orphans_in_services_without_saga():
    snap = _snap(
        connectors=[{"station_id": "bog", "connector_id": "c1", "reserved_by": "nadie"}],
        charges={"nadie": {"status": "CHARGED", "amount_cents": 99}},
    )
    assert {("WARNING", "reserva_huerfana"), ("CRITICAL", "cobro_huerfano")} <= _rules(snap)


def test_findings_are_sorted_by_severity():
    snap = _snap(
        sagas={"a": {"status": "COMPENSATED"}, "b": {"status": "COMPENSATION_FAILED"}},
    )
    assert [f.severity for f in audit(snap, NOW)] == ["CRITICAL", "INFO"]


def test_health_verdicts():
    ok = {"env": "dev", "gateway_ready": True, "services": {"a": True}, "tables": {"t": "ACTIVE"}}
    assert health_verdict(ok) == ("OK", [])
    degraded = {**ok, "services": {"billing": False}}
    assert health_verdict(degraded)[0] == "DEGRADED"
    down = {**ok, "gateway_ready": False}
    assert health_verdict(down)[0] == "DOWN"
    prod_fault = {**ok, "env": "prod", "fault_flag": True}
    verdict, reasons = health_verdict(prod_fault)
    assert verdict == "DEGRADED" and "PROD" in reasons[0]
