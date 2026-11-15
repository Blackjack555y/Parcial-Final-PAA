"""Modelo de la SAGA "Reservar y activar carga"."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

# Espacio de nombres fijo: la misma llave de idempotencia produce SIEMPRE el mismo saga_id.
_SAGA_NAMESPACE = uuid.UUID("6f1d3a52-6c1e-4a7b-9f40-2a9e5c0b7d11")


class SagaStatus(StrEnum):
    STARTED = "STARTED"  # registrada, ningún paso ejecutado
    RESERVED = "RESERVED"  # paso 1 OK: conector bloqueado
    CHARGED = "CHARGED"  # paso 2 OK: saldo retenido
    COMPLETED = "COMPLETED"  # paso 3 OK: el cargador entrega energía
    FINISHED = "FINISHED"  # la sesión terminó y el conector se liberó
    FAILED = "FAILED"  # falló el paso 1: no había nada que compensar
    COMPENSATING = "COMPENSATING"  # ejecutando compensaciones
    COMPENSATED = "COMPENSATED"  # rollback distribuido completo
    COMPENSATION_FAILED = "COMPENSATION_FAILED"  # requiere reintento (Prefect lo detecta)


TERMINAL = {
    SagaStatus.FINISHED,
    SagaStatus.FAILED,
    SagaStatus.COMPENSATED,
    SagaStatus.COMPENSATION_FAILED,
}


@dataclass(frozen=True, slots=True)
class SagaRequest:
    station_id: str
    connector_id: str
    fleet_id: str
    kwh: float
    idempotency_key: str | None = None


def saga_id_for(idempotency_key: str | None) -> str:
    """Con llave de idempotencia: id determinista (reintentos del cliente = misma saga)."""
    if idempotency_key:
        return f"saga-{uuid.uuid5(_SAGA_NAMESPACE, idempotency_key)}"
    return f"saga-{uuid.uuid4()}"
