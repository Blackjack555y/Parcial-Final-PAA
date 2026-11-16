"""Orquestador de la SAGA "Reservar y activar carga".

    Paso 1  station-reservation-service  reservar conector (bloqueo temporal)  ⟲ liberar conector
    Paso 2  billing-wallet-service       RETENER el saldo estimado             ⟲ liberar retención
    Paso 3  energy-dispatch-service      activar el cargador                   (último paso)
    Cierre  dispatch → billing → reservations: detener, LIQUIDAR la energía real, liberar bahía

Si el paso N falla, se ejecutan las compensaciones de los pasos 1..N-1 en orden INVERSO:
  - falla el paso 3 → liberar retención (2) → liberar conector (1)
  - falla el paso 2 → liberar conector (1)
  - falla el paso 1 → nada que compensar (FAILED)

SAGA de LARGA DURACIÓN (reserva anticipada): con arrival_minutes > 0 se ejecutan los pasos 1 y 2
y la saga queda en AWAITING_ARRIVAL con un plazo. Luego:
  - confirm_arrival → paso 3 (si falla, compensa 2 y 1)
  - cancel          → compensa 2 y 1 (motivo arrival:CANCELLED)
  - expire          → vence el plazo: compensa 2 y 1 (motivo arrival:EXPIRED); lo dispara el
                      supervisor (supervisor.py). Compensación por TIMEOUT.

Idempotencia (pregunta 6.3):
  - El saga_id viaja en cada llamada y cada servicio lo usa como llave con escrituras
    condicionales: repetir una acción o una compensación NO repite su efecto.
  - Por eso las compensaciones se reintentan con backoff, retry_compensation() puede volver a
    ejecutarlas TODAS, y varias réplicas del supervisor pueden actuar sobre la misma saga.
  - Con idempotency_key, un reintento del cliente reutiliza la misma saga en vez de crear otra.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aegis_common.context import correlation_id_var, saga_id_var
from aegis_common.logging import log_event
from graphql_gateway.clients import ServiceClients, ServiceError
from graphql_gateway.saga.models import SagaRequest, SagaStatus, saga_id_for
from graphql_gateway.saga.repository import SagaRepository

Action = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

RESERVATION_TTL = int(os.environ.get("RESERVATION_TTL_SECONDS", "120"))
ARRIVAL_GRACE = int(os.environ.get("ARRIVAL_GRACE_SECONDS", "30"))
# El servicio de reservas libera por su cuenta un poco DESPUÉS que el orquestador: es la red de
# seguridad si el orquestador está caído, no compite con él.
RESERVATION_SAFETY_MARGIN = 60


@dataclass
class Step:
    name: str
    status_after: SagaStatus
    action: Action
    compensation: Action | None = None
    summary: Callable[[dict[str, Any]], dict[str, Any]] = field(default=lambda r: {})


def _now() -> datetime:
    return datetime.now(UTC)


class SagaOrchestrator:
    def __init__(
        self,
        repo: SagaRepository,
        clients: ServiceClients,
        logger: logging.Logger,
        *,
        compensation_attempts: int = 3,
        backoff_seconds: float = 0.2,
    ) -> None:
        self.repo = repo
        self.clients = clients
        self.log = logger
        self.compensation_attempts = compensation_attempts
        self.backoff = backoff_seconds

    # --- Definición de los pasos --------------------------------------------------------
    def _steps(self, saga_id: str, req: SagaRequest) -> list[Step]:
        res, bil, dis = self.clients.reservations, self.clients.billing, self.clients.dispatch
        if req.arrival_minutes > 0:
            hold_seconds = req.arrival_minutes * 60 + ARRIVAL_GRACE + RESERVATION_SAFETY_MARGIN
        else:
            hold_seconds = RESERVATION_TTL

        async def reserve(ctx: dict[str, Any]) -> dict[str, Any]:
            return await res.call(
                "POST",
                "/reservations",
                json={
                    "saga_id": saga_id,
                    "station_id": req.station_id,
                    "connector_id": req.connector_id,
                    "fleet_id": req.fleet_id,
                    "hold_seconds": hold_seconds,
                },
            )

        async def release_connector(ctx: dict[str, Any]) -> dict[str, Any]:
            return await res.call(
                "POST",
                f"/reservations/{saga_id}/release",
                json={"reason": ctx.get("failure", "compensation")},
            )

        async def hold_funds(ctx: dict[str, Any]) -> dict[str, Any]:
            return await bil.call(
                "POST",
                "/holds",
                json={
                    "saga_id": saga_id,
                    "fleet_id": req.fleet_id,
                    "connector_type": ctx["reserve"]["connector_type"],
                    "kwh": req.kwh,
                },
            )

        async def release_funds(ctx: dict[str, Any]) -> dict[str, Any]:
            return await bil.call(
                "POST",
                f"/holds/{saga_id}/release",
                json={"fleet_id": req.fleet_id, "reason": ctx.get("failure", "compensation")},
            )

        async def start_charging(ctx: dict[str, Any]) -> dict[str, Any]:
            session = await dis.call(
                "POST",
                "/sessions",
                json={
                    "saga_id": saga_id,
                    "station_id": req.station_id,
                    "connector_id": req.connector_id,
                    "power_kw": ctx["reserve"]["power_kw"],
                    "target_kwh": req.kwh,
                },
            )
            # El cargador YA entrega energía: a partir de aquí no se compensa. Marcar el conector
            # como CHARGING es de mejor esfuerzo; si falla, el conector queda RESERVED con una
            # sesión ACTIVE y la auditoría de Prefect lo reporta como inconsistencia.
            try:
                await res.call("POST", f"/reservations/{saga_id}/occupy")
            except ServiceError as err:
                log_event(
                    self.log,
                    "connector_occupy_failed",
                    level=logging.WARNING,
                    code=err.code,
                    message=err.message,
                )
            return session

        return [
            Step(
                "reserve",
                SagaStatus.RESERVED,
                reserve,
                release_connector,
                lambda r: {"connector_type": r["connector_type"], "power_kw": r["power_kw"]},
            ),
            Step(
                "charge",
                SagaStatus.CHARGED,
                hold_funds,
                release_funds,
                lambda r: {
                    "amount_cents": r["amount_cents"],
                    "surge_active": r.get("surge_active", False),
                },
            ),
            Step("dispatch", SagaStatus.COMPLETED, start_charging, None, lambda r: {}),
        ]

    # --- Ejecución ------------------------------------------------------------------------
    async def start(self, req: SagaRequest) -> dict[str, Any]:
        saga_id = saga_id_for(req.idempotency_key)
        token = saga_id_var.set(saga_id)
        try:
            _, created = await asyncio.to_thread(
                self.repo.create, saga_id, req, correlation_id_var.get()
            )
            if not created:
                log_event(self.log, "saga_replayed", "Llave de idempotencia repetida")
                return await self.get(saga_id)  # type: ignore[return-value]

            log_event(
                self.log,
                "saga_started",
                station_id=req.station_id,
                connector_id=req.connector_id,
                fleet_id=req.fleet_id,
                kwh=req.kwh,
                arrival_minutes=req.arrival_minutes,
            )
            steps = self._steps(saga_id, req)
            ctx: dict[str, Any] = {}
            # Reserva anticipada: solo pasos 1 y 2; el 3 espera la llegada del vehículo.
            first = steps[:2] if req.arrival_minutes > 0 else steps
            done = await self._run(saga_id, first, ctx, done=[])
            if done is None:
                return await self.get(saga_id)  # type: ignore[return-value]

            if req.arrival_minutes > 0:
                deadline = _now() + timedelta(minutes=req.arrival_minutes, seconds=ARRIVAL_GRACE)
                deadline_iso = deadline.isoformat(timespec="seconds")
                await self._event(saga_id, "arrival", "wait", "OK", f"hasta {deadline_iso}")
                await asyncio.to_thread(
                    self.repo.set_status,
                    saga_id,
                    SagaStatus.AWAITING_ARRIVAL,
                    arrival_deadline=deadline_iso,
                )
                log_event(self.log, "saga_awaiting_arrival", deadline=deadline_iso)
            else:
                log_event(self.log, "saga_completed", "Carga activa")
            return await self.get(saga_id)  # type: ignore[return-value]
        finally:
            saga_id_var.reset(token)

    async def _run(
        self, saga_id: str, steps: list[Step], ctx: dict[str, Any], done: list[Step]
    ) -> list[Step] | None:
        """Ejecuta los pasos en orden. Si uno falla, compensa los ya hechos y devuelve None."""
        for step in steps:
            try:
                result = await step.action(ctx)
            except ServiceError as err:
                await self._event(saga_id, step.name, "action", "FAILED", err.message)
                log_event(
                    self.log,
                    "saga_step_failed",
                    f"Paso {step.name} falló: {err.code}",
                    level=logging.WARNING,
                    step=step.name,
                    failed_service=err.service,
                    code=err.code,
                    http_status=err.status,
                )
                ctx["failure"] = f"{step.name}:{err.code}"
                if not done:
                    await asyncio.to_thread(
                        self.repo.set_status,
                        saga_id,
                        SagaStatus.FAILED,
                        failure_reason=ctx["failure"],
                        failure_message=err.message,
                    )
                else:
                    await self._compensate(saga_id, done, ctx, err.message)
                return None

            ctx[step.name] = result
            done.append(step)
            summary = step.summary(result)
            await self._event(saga_id, step.name, "action", "OK", _fmt(summary))
            await asyncio.to_thread(self.repo.set_status, saga_id, step.status_after, **summary)
            log_event(self.log, "saga_step_completed", step=step.name, **summary)
        return done

    # --- SAGA de larga duración: llegada, cancelación y vencimiento -------------------------
    async def _awaiting(self, saga_id: str) -> tuple[dict[str, Any], list[Step]]:
        saga = await self.get(saga_id)
        if saga is None:
            raise LookupError(f"Saga {saga_id} no existe")
        if saga["status"] != SagaStatus.AWAITING_ARRIVAL:
            raise ValueError(f"La saga está en {saga['status']}: no espera la llegada")
        req = SagaRequest(
            saga["station_id"], saga["connector_id"], saga["fleet_id"], saga["kwh"], None, 1
        )
        return saga, self._steps(saga_id, req)

    async def confirm_arrival(self, saga_id: str) -> dict[str, Any]:
        """El vehículo llegó: paso 3. Si el plazo ya venció, no se activa nada."""
        token = saga_id_var.set(saga_id)
        try:
            saga, steps = await self._awaiting(saga_id)
            if datetime.fromisoformat(saga["arrival_deadline"]) < _now():
                raise ValueError("La reserva venció: el vehículo llegó fuera del plazo")
            ctx = {
                "reserve": {
                    "connector_type": saga["connector_type"],
                    "power_kw": saga["power_kw"],
                }
            }
            await self._event(saga_id, "arrival", "confirm", "OK", "vehículo en la bahía")
            if await self._run(saga_id, steps[2:], ctx, done=steps[:2]) is not None:
                log_event(self.log, "saga_completed", "Carga activa (llegada confirmada)")
            return await self.get(saga_id)  # type: ignore[return-value]
        finally:
            saga_id_var.reset(token)

    async def cancel(self, saga_id: str, reason: str = "arrival:CANCELLED") -> dict[str, Any]:
        """Cancela una reserva anticipada: libera saldo retenido y bahía (orden inverso)."""
        token = saga_id_var.set(saga_id)
        try:
            saga, steps = await self._awaiting(saga_id)
            message = "reserva vencida sin llegada" if "EXPIRED" in reason else "cancelada"
            await self._event(saga_id, "arrival", "wait", "FAILED", message)
            await self._compensate(saga_id, steps[:2], {"failure": reason}, message)
            if "EXPIRED" in reason:
                log_event(self.log, "saga_expired", deadline=saga.get("arrival_deadline"))
            return await self.get(saga_id)  # type: ignore[return-value]
        finally:
            saga_id_var.reset(token)

    async def expire(self, saga_id: str) -> dict[str, Any]:
        """Compensación por TIMEOUT: el vehículo no llegó dentro del plazo."""
        return await self.cancel(saga_id, reason="arrival:EXPIRED")

    # --- Compensaciones -----------------------------------------------------------------------
    async def _compensate(
        self, saga_id: str, done: list[Step], ctx: dict[str, Any], message: str
    ) -> SagaStatus:
        await asyncio.to_thread(
            self.repo.set_status,
            saga_id,
            SagaStatus.COMPENSATING,
            failure_reason=ctx.get("failure"),
            failure_message=message,
        )
        log_event(self.log, "saga_compensating", reason=ctx.get("failure"))
        all_ok = True
        compensated: list[str] = []
        for step in reversed(done):  # orden inverso: lo último que se hizo se deshace primero
            if step.compensation is None:
                continue
            ok, detail = await self._run_compensation(step, ctx)
            await self._event(saga_id, step.name, "compensation", "OK" if ok else "FAILED", detail)
            if ok:
                compensated.append(step.name)
            all_ok = all_ok and ok

        final = SagaStatus.COMPENSATED if all_ok else SagaStatus.COMPENSATION_FAILED
        await asyncio.to_thread(self.repo.set_status, saga_id, final)
        log_event(
            self.log,
            "saga_compensated" if all_ok else "saga_compensation_failed",
            f"Rollback distribuido: {', '.join(compensated) or 'ninguno'}",
            level=logging.WARNING if all_ok else logging.ERROR,
            compensated_steps=compensated,
            reason=ctx.get("failure"),
        )
        return final

    async def _run_compensation(self, step: Step, ctx: dict[str, Any]) -> tuple[bool, str]:
        """Reintenta con backoff exponencial: es seguro porque la compensación es idempotente."""
        if step.compensation is None:
            return True, "sin compensación"
        last = ""
        for attempt in range(1, self.compensation_attempts + 1):
            try:
                result = await step.compensation(ctx)
                return True, _fmt(
                    {
                        k: result.get(k)
                        for k in ("released", "detail", "amount_cents")
                        if k in result
                    }
                )
            except ServiceError as err:
                last = f"intento {attempt}: {err.code} {err.message}"
                log_event(
                    self.log,
                    "saga_compensation_retry",
                    level=logging.WARNING,
                    step=step.name,
                    attempt=attempt,
                    code=err.code,
                )
                if attempt < self.compensation_attempts:
                    await asyncio.sleep(self.backoff * 2 ** (attempt - 1))
        return False, last

    async def retry_compensation(self, saga_id: str) -> dict[str, Any]:
        """Re-ejecuta TODAS las compensaciones de una saga fallida.

        Seguro gracias a la idempotencia: una retención ya liberada responde "ya RELEASED" y
        una bahía ya liberada responde "ya liberada", sin mover dinero ni conectores.
        """
        saga = await self.get(saga_id)
        if saga is None:
            raise LookupError(f"Saga {saga_id} no existe")
        if saga["status"] not in (
            SagaStatus.COMPENSATED,
            SagaStatus.COMPENSATION_FAILED,
            SagaStatus.COMPENSATING,
        ):
            raise ValueError(f"La saga está en {saga['status']}: solo se reintentan rollbacks")
        req = SagaRequest(saga["station_id"], saga["connector_id"], saga["fleet_id"], saga["kwh"])
        token = saga_id_var.set(saga_id)
        try:
            steps = [s for s in self._steps(saga_id, req) if s.compensation]
            ctx = {"failure": saga.get("failure_reason") or "manual_retry"}
            await self._compensate(saga_id, steps, ctx, "reintento manual de compensación")
        finally:
            saga_id_var.reset(token)
        return await self.get(saga_id)  # type: ignore[return-value]

    # --- Cierre: detener y liquidar -------------------------------------------------------------
    async def stop(self, saga_id: str, reason: str = "manual") -> dict[str, Any]:
        """Termina la carga: detiene el cargador, liquida la energía REAL y libera el conector.

        Idempotente: cada llamada a los servicios lo es, y una saga FINISHED se devuelve tal cual
        (el supervisor y el operador pueden intentarlo a la vez sin cobrar dos veces).
        """
        saga = await self.get(saga_id)
        if saga is None:
            raise LookupError(f"Saga {saga_id} no existe")
        if saga["status"] == SagaStatus.FINISHED:
            return saga
        if saga["status"] != SagaStatus.COMPLETED:
            raise ValueError(f"La saga está en {saga['status']}: no hay carga activa")
        token = saga_id_var.set(saga_id)
        try:
            session = await self.clients.dispatch.call("POST", f"/sessions/{saga_id}/stop")
            energy = float(session.get("energy_kwh", 0.0))
            settled = await self.clients.billing.call(
                "POST",
                f"/holds/{saga_id}/capture",
                json={"fleet_id": saga["fleet_id"], "energy_kwh": energy},
            )
            await self.clients.reservations.call("POST", f"/reservations/{saga_id}/complete")
            captured = settled.get("captured_cents", 0)
            released = settled.get("released_cents", 0)
            await self._event(saga_id, "dispatch", "stop", "OK", f"{reason}, energy_kwh={energy}")
            await self._event(
                saga_id,
                "charge",
                "capture",
                "OK",
                f"captured_cents={captured}, released_cents={released}",
            )
            await asyncio.to_thread(
                self.repo.set_status,
                saga_id,
                SagaStatus.FINISHED,
                energy_kwh=energy,
                captured_cents=captured,
                released_cents=released,
                finish_reason=reason,
            )
            log_event(
                self.log,
                "saga_finished",
                energy_kwh=energy,
                captured_cents=captured,
                released_cents=released,
                reason=reason,
            )
        finally:
            saga_id_var.reset(token)
        return await self.get(saga_id)  # type: ignore[return-value]

    async def get(self, saga_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.repo.get, saga_id)

    async def _event(self, saga_id: str, step: str, action: str, outcome: str, detail: str):
        await asyncio.to_thread(self.repo.add_event, saga_id, step, action, outcome, detail)


def _fmt(data: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in data.items())
