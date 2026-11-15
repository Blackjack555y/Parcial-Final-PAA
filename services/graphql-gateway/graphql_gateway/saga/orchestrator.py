"""Orquestador de la SAGA "Reservar y activar carga".

    Paso 1  station-reservation-service  reservar conector      ⟲ liberar conector
    Paso 2  billing-wallet-service       cobrar saldo estimado  ⟲ reembolsar
    Paso 3  energy-dispatch-service      activar el cargador    (último paso: sin compensación)

Si el paso N falla, se ejecutan las compensaciones de los pasos 1..N-1 en orden INVERSO:
  - falla el paso 3 → reembolso (2) → liberación (1)
  - falla el paso 2 → liberación (1)
  - falla el paso 1 → nada que compensar (FAILED)

Cada transición queda en la bitácora (tabla sagas) y en los logs con el mismo correlation_id.

Idempotencia (pregunta 6.3):
  - El saga_id viaja en cada llamada y cada servicio lo usa como llave con escrituras
    condicionales: repetir una acción o una compensación NO repite su efecto.
  - Por eso las compensaciones se pueden reintentar con seguridad (backoff exponencial) y
    retry_compensation() puede volver a ejecutarlas TODAS sin riesgo de doble reembolso.
  - Con idempotency_key, un reintento del cliente reutiliza la misma saga en vez de crear otra.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aegis_common.context import correlation_id_var, saga_id_var
from aegis_common.logging import log_event
from graphql_gateway.clients import ServiceClients, ServiceError
from graphql_gateway.saga.models import SagaRequest, SagaStatus, saga_id_for
from graphql_gateway.saga.repository import SagaRepository

Action = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class Step:
    name: str
    status_after: SagaStatus
    action: Action
    compensation: Action | None = None
    summary: Callable[[dict[str, Any]], dict[str, Any]] = field(default=lambda r: {})


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

        async def reserve(ctx: dict[str, Any]) -> dict[str, Any]:
            return await res.call(
                "POST",
                "/reservations",
                json={
                    "saga_id": saga_id,
                    "station_id": req.station_id,
                    "connector_id": req.connector_id,
                    "fleet_id": req.fleet_id,
                },
            )

        async def release(ctx: dict[str, Any]) -> dict[str, Any]:
            return await res.call(
                "POST",
                f"/reservations/{saga_id}/release",
                json={"reason": ctx.get("failure", "compensation")},
            )

        async def charge(ctx: dict[str, Any]) -> dict[str, Any]:
            return await bil.call(
                "POST",
                "/charges",
                json={
                    "saga_id": saga_id,
                    "fleet_id": req.fleet_id,
                    "connector_type": ctx["reserve"]["connector_type"],
                    "kwh": req.kwh,
                },
            )

        async def refund(ctx: dict[str, Any]) -> dict[str, Any]:
            return await bil.call(
                "POST",
                f"/charges/{saga_id}/refund",
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
                release,
                lambda r: {"connector_type": r["connector_type"], "power_kw": r["power_kw"]},
            ),
            Step(
                "charge",
                SagaStatus.CHARGED,
                charge,
                refund,
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
            )
            ctx: dict[str, Any] = {}
            done: list[Step] = []
            for step in self._steps(saga_id, req):
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
                    return await self.get(saga_id)  # type: ignore[return-value]

                ctx[step.name] = result
                done.append(step)
                summary = step.summary(result)
                await self._event(saga_id, step.name, "action", "OK", _fmt(summary))
                await asyncio.to_thread(self.repo.set_status, saga_id, step.status_after, **summary)
                log_event(self.log, "saga_step_completed", step=step.name, **summary)

            log_event(self.log, "saga_completed", "Carga activa")
            return await self.get(saga_id)  # type: ignore[return-value]
        finally:
            saga_id_var.reset(token)

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
                        for k in ("released", "refunded", "detail", "amount_cents")
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

        Seguro gracias a la idempotencia: un reembolso ya hecho responde "ya reembolsado" y
        una liberación ya hecha responde "ya liberada", sin mover dinero ni conectores.
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

    async def stop(self, saga_id: str) -> dict[str, Any]:
        """Termina la carga: detiene el cargador y libera el conector."""
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
            await self.clients.reservations.call("POST", f"/reservations/{saga_id}/complete")
            energy = session.get("energy_kwh", 0.0)
            await self._event(saga_id, "dispatch", "stop", "OK", f"energy_kwh={energy}")
            await asyncio.to_thread(
                self.repo.set_status, saga_id, SagaStatus.FINISHED, energy_kwh=energy
            )
            log_event(self.log, "saga_finished", energy_kwh=energy)
        finally:
            saga_id_var.reset(token)
        return await self.get(saga_id)  # type: ignore[return-value]

    async def get(self, saga_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.repo.get, saga_id)

    async def _event(self, saga_id: str, step: str, action: str, outcome: str, detail: str):
        await asyncio.to_thread(self.repo.add_event, saga_id, step, action, outcome, detail)


def _fmt(data: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in data.items())
