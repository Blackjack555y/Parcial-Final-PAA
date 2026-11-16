"""Reglas de auditoría de la SAGA (lógica pura: sin Prefect ni AWS, probada de forma unitaria).

Cruza la bitácora del orquestador (tabla sagas) con el estado REAL de cada servicio
(reservations, billing, dispatch) y reporta lo que no cuadra. Es la verificación de la
consistencia eventual: después de una saga, las tres tablas deben contar la misma historia.

Severidad:
  CRITICAL  dinero o energía comprometidos (cobro sin reembolso, cargador activo sin saga)
  WARNING   recursos retenidos o sagas detenidas (conector bloqueado, saga atascada)
  INFO      hechos normales que conviene ver (sagas compensadas)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

IN_FLIGHT = {"STARTED", "RESERVED", "CHARGED", "COMPENSATING"}
ROLLED_BACK = {"COMPENSATED", "FAILED"}
OPEN_RESERVATION = {"RESERVED", "CHARGING"}
MONEY_HELD = {"HELD", "CHARGED"}  # retención abierta (CHARGED = formato anterior)


@dataclass(frozen=True, slots=True)
class Finding:
    severity: str
    rule: str
    saga_id: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class Snapshot:
    """Estado de un ambiente leído de las 4 tablas (ver sources.py)."""

    sagas: dict[str, dict[str, Any]]  # saga_id → META de la saga
    reservations: dict[str, dict[str, Any]]  # saga_id → RESERVATION#
    connectors: list[dict[str, Any]]  # conectores (reserved_by = saga_id)
    charges: dict[str, dict[str, Any]]  # saga_id → TXN#
    sessions: dict[str, dict[str, Any]]  # saga_id → SESSION#


def _age(ts: str | None, now: datetime) -> timedelta:
    if not ts:
        return timedelta(0)
    return now - datetime.fromisoformat(ts)


def audit(snap: Snapshot, now: datetime | None = None, stuck_after_s: int = 120) -> list[Finding]:
    now = now or datetime.now(UTC)
    findings: list[Finding] = []

    def add(severity: str, rule: str, saga_id: str, detail: str) -> None:
        findings.append(Finding(severity, rule, saga_id, detail))

    for sid, saga in snap.sagas.items():
        status = saga.get("status", "?")
        charge = snap.charges.get(sid)
        reservation = snap.reservations.get(sid)
        session = snap.sessions.get(sid)

        # 1. Sagas que no terminaron (el orquestador murió a mitad de camino).
        age = _age(saga.get("updated_at"), now)
        if status in IN_FLIGHT and age.total_seconds() > stuck_after_s:
            add("WARNING", "saga_atascada", sid, f"{status} hace {int(age.total_seconds())} s")

        # 2. Compensación que agotó sus reintentos.
        if status == "COMPENSATION_FAILED":
            pending = []
            if charge and charge.get("status") in MONEY_HELD:
                pending.append(f"{charge.get('amount_cents')} ¢ retenidos sin liberar")
            if reservation and reservation.get("status") in OPEN_RESERVATION:
                pending.append("conector retenido")
            detail = f"{saga.get('failure_reason') or ''}: {', '.join(pending) or 'revisar'}"
            add("CRITICAL", "compensacion_fallida", sid, detail)

        # 3. Rollback "terminado" pero la retención no se liberó.
        if status in ROLLED_BACK and charge and charge.get("status") in MONEY_HELD:
            add(
                "CRITICAL",
                "cobro_sin_reembolso",
                sid,
                f"saga {status} y la retención sigue {charge.get('status')} "
                f"({charge.get('amount_cents')} ¢)",
            )

        # 3b. Carga terminada pero el cobro no se liquidó (la flota sigue con saldo retenido).
        if status == "FINISHED" and charge and charge.get("status") in MONEY_HELD:
            add(
                "WARNING",
                "retencion_sin_liquidar",
                sid,
                f"saga FINISHED y {charge.get('amount_cents')} ¢ siguen retenidos",
            )

        # 3c. Reserva anticipada vencida que nadie compensó (¿supervisor caído?).
        deadline = saga.get("arrival_deadline")
        if status == "AWAITING_ARRIVAL" and deadline:
            overdue = (now - datetime.fromisoformat(deadline)).total_seconds()
            if overdue > stuck_after_s:
                add(
                    "WARNING",
                    "reserva_vencida_sin_compensar",
                    sid,
                    f"plazo vencido hace {int(overdue)} s y la saga sigue esperando",
                )

        # 4. Rollback "terminado" pero el conector sigue bloqueado.
        held = reservation and reservation.get("status") in OPEN_RESERVATION
        if status in ROLLED_BACK | {"FINISHED"} and held:
            add(
                "WARNING",
                "conector_retenido",
                sid,
                f"saga {status} y la reserva sigue {reservation.get('status')}",
            )

        # 5. La saga dice "cargando" pero el cargador no.
        if status == "COMPLETED" and (session is None or session.get("status") != "ACTIVE"):
            current = session.get("status") if session else "ausente"
            add("CRITICAL", "carga_sin_sesion", sid, f"saga COMPLETED y la sesión está {current}")

        # 6. El cargador entrega energía para una saga que no está activa.
        if session and session.get("status") == "ACTIVE" and status != "COMPLETED":
            add("CRITICAL", "sesion_activa_huerfana", sid, f"sesión ACTIVE con saga {status}")

        # 7. Conector en CHARGING pero la reserva quedó en RESERVED (occupy de mejor esfuerzo).
        if status == "COMPLETED" and reservation and reservation.get("status") == "RESERVED":
            add("WARNING", "conector_no_marcado", sid, "carga activa con el conector en RESERVED")

        if status == "COMPENSATED":
            add("INFO", "saga_compensada", sid, saga.get("failure_reason") or "")

    # 8. Rastros en los servicios sin saga en la bitácora (huérfanos).
    for connector in snap.connectors:
        sid = connector.get("reserved_by")
        if sid and sid not in snap.sagas:
            add(
                "WARNING",
                "reserva_huerfana",
                sid,
                f"conector {connector.get('station_id')}/{connector.get('connector_id')} "
                "bloqueado por una saga inexistente",
            )
    for sid, charge in snap.charges.items():
        if sid not in snap.sagas and charge.get("status") in MONEY_HELD:
            add("CRITICAL", "cobro_huerfano", sid, f"{charge.get('amount_cents')} ¢ sin saga")
    for sid, session in snap.sessions.items():
        if sid not in snap.sagas and session.get("status") == "ACTIVE":
            add("CRITICAL", "sesion_activa_huerfana", sid, "sesión ACTIVE sin saga")

    order = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    return sorted(findings, key=lambda f: (order[f.severity], f.rule, f.saga_id))


def summarize(snap: Snapshot, findings: list[Finding]) -> dict[str, Any]:
    by_status = Counter(s.get("status", "?") for s in snap.sagas.values())
    by_severity = Counter(f.severity for f in findings)
    return {
        "sagas": len(snap.sagas),
        "por_estado": dict(sorted(by_status.items())),
        "critical": by_severity.get("CRITICAL", 0),
        "warning": by_severity.get("WARNING", 0),
        "compensadas": by_status.get("COMPENSATED", 0),
        "consistente": by_severity.get("CRITICAL", 0) == 0 and by_severity.get("WARNING", 0) == 0,
    }


def health_verdict(checks: dict[str, Any]) -> tuple[str, list[str]]:
    """Resume los chequeos de un ambiente en OK | DEGRADED | DOWN con sus motivos."""
    problems: list[str] = []
    if not checks.get("gateway_ready"):
        problems.append("gateway no responde /readyz")
    for svc, ok in (checks.get("services") or {}).items():
        if not ok:
            problems.append(f"{svc} caído")
    for table, status in (checks.get("tables") or {}).items():
        if status != "ACTIVE":
            problems.append(f"tabla {table} {status}")
    if checks.get("errors_5xx_15m", 0) > 0:
        problems.append(f"{checks['errors_5xx_15m']} errores 5xx en 15 min")
    if checks.get("env") == "prod" and checks.get("fault_flag"):
        problems.append("SIMULATE_CHARGER_HARDWARE_FAULT activa en PROD")
    if not checks.get("gateway_ready") or any(p.startswith("tabla") for p in problems):
        return "DOWN", problems
    return ("DEGRADED" if problems else "OK"), problems
