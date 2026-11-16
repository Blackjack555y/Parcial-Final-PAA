"""Supervisor de sagas de larga duración (tarea en segundo plano del gateway).

Cada SUPERVISOR_INTERVAL_SECONDS:
  1. AWAITING_ARRIVAL con el plazo vencido → orchestrator.expire()   (compensación por timeout)
  2. COMPLETED cuya telemetría alcanzó la energía pedida → orchestrator.stop("target_reached")
     (fin automático: liquida la energía real y libera el conector)

Seguro con varias réplicas del gateway: si dos réplicas actúan sobre la misma saga, la segunda
encuentra la saga ya cerrada (o las operaciones de los servicios responden "ya hecho") gracias a
la idempotencia. Las sagas se buscan por el GSI by-status de la tabla, sin escanear.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from aegis_common.logging import log_event
from graphql_gateway.clients import ServiceError
from graphql_gateway.saga.models import SagaStatus
from graphql_gateway.saga.orchestrator import SagaOrchestrator


class SagaSupervisor:
    def __init__(
        self, orchestrator: SagaOrchestrator, logger: logging.Logger, interval: float = 5.0
    ) -> None:
        self.orch = orchestrator
        self.log = logger
        self.interval = interval

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 — el supervisor nunca debe morir
                log_event(self.log, "supervisor_error", level=logging.ERROR, error=str(exc)[:300])
            await asyncio.sleep(self.interval)

    async def tick(self, now: datetime | None = None) -> dict[str, list[str]]:
        now = now or datetime.now(UTC)
        result: dict[str, list[str]] = {"expired": [], "finished": []}
        repo = self.orch.repo

        for saga in await asyncio.to_thread(repo.list_by_status, SagaStatus.AWAITING_ARRIVAL):
            deadline = saga.get("arrival_deadline")
            if deadline and datetime.fromisoformat(deadline) < now:
                try:
                    await self.orch.expire(saga["saga_id"])
                    result["expired"].append(saga["saga_id"])
                except ValueError:
                    pass  # otra réplica (o el operador) ya la resolvió

        for saga in await asyncio.to_thread(repo.list_by_status, SagaStatus.COMPLETED):
            if await self._target_reached(saga):
                try:
                    await self.orch.stop(saga["saga_id"], reason="target_reached")
                    result["finished"].append(saga["saga_id"])
                except (ValueError, ServiceError) as exc:
                    log_event(
                        self.log,
                        "supervisor_stop_failed",
                        level=logging.WARNING,
                        saga_id_field=saga["saga_id"],
                        error=str(exc)[:200],
                    )
        if result["expired"] or result["finished"]:
            log_event(self.log, "supervisor_tick", **result)
        return result

    async def _target_reached(self, saga: dict[str, Any]) -> bool:
        try:
            telemetry = await self.orch.clients.dispatch.call(
                "GET", f"/sessions/{saga['saga_id']}/telemetry"
            )
        except ServiceError:
            return False
        return bool(telemetry.get("target_reached"))
