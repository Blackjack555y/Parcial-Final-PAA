"""Contexto de la petición: correlation_id y saga_id viajan entre servicios.

- correlation_id: identifica UNA operación de negocio de punta a punta (la mutación GraphQL
  y todas las llamadas que dispara). Viaja en el encabezado X-Correlation-Id.
- saga_id: identifica la SAGA. Viaja en X-Saga-Id.

Se guardan en ContextVar: cada petición (hilo o tarea asyncio) ve sus propios valores.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

CORRELATION_HEADER = "X-Correlation-Id"
SAGA_HEADER = "X-Saga-Id"

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
saga_id_var: ContextVar[str | None] = ContextVar("saga_id", default=None)


def new_correlation_id() -> str:
    return f"corr-{uuid.uuid4().hex}"


def outgoing_headers() -> dict[str, str]:
    """Encabezados que se propagan en cada llamada a otro servicio."""
    headers: dict[str, str] = {}
    if cid := correlation_id_var.get():
        headers[CORRELATION_HEADER] = cid
    if sid := saga_id_var.get():
        headers[SAGA_HEADER] = sid
    return headers
