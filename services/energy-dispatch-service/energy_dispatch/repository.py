"""Acceso a la tabla aegisvolt-dispatch-<env> (dueño exclusivo: este servicio).

Ítems:
  pk=SESSION#<saga>  sk=META              status ACTIVE|FAILED|COMPLETED, potencia, inicio
  pk=SESSION#<saga>  sk=TELEMETRY#<ts>    muestra de telemetría (expira con TTL expires_at)

Idempotencia: la sesión se crea con attribute_not_exists(pk). Si la orden de activación
llega dos veces para la misma saga, la segunda devuelve la sesión ya registrada (o la misma
falla), nunca energiza el cargador dos veces.
"""

from __future__ import annotations

import contextlib
import time
from datetime import UTC, datetime
from typing import Any

from aegis_common.aws import from_item, to_item
from aegis_common.errors import NotFound

ACTIVE, FAILED, COMPLETED = "ACTIVE", "FAILED", "COMPLETED"
TELEMETRY_TTL_SECONDS = 24 * 3600


def _now() -> datetime:
    return datetime.now(UTC)


def _session_key(saga_id: str) -> dict[str, Any]:
    return to_item({"pk": f"SESSION#{saga_id}", "sk": "META"})


class DispatchRepository:
    def __init__(self, ddb: Any, table: str) -> None:
        self.ddb = ddb
        self.table = table

    def get_session(self, saga_id: str) -> dict[str, Any] | None:
        raw = self.ddb.get_item(
            TableName=self.table, Key=_session_key(saga_id), ConsistentRead=True
        ).get("Item")
        return _session(from_item(raw)) if raw else None

    def require_session(self, saga_id: str) -> dict[str, Any]:
        session = self.get_session(saga_id)
        if session is None:
            raise NotFound(f"Sesión de la saga {saga_id} no existe")
        return session

    def list_sessions(self, status: str | None = None) -> list[dict[str, Any]]:
        values = {":sk": "META", ":p": "SESSION#"}
        expr = "sk = :sk AND begins_with(pk, :p)"
        names: dict[str, str] = {}
        if status:
            expr += " AND #s = :status"
            values[":status"] = status
            names["#s"] = "status"
        kwargs: dict[str, Any] = {
            "TableName": self.table,
            "FilterExpression": expr,
            "ExpressionAttributeValues": to_item(values),
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names
        sessions: list[dict[str, Any]] = []
        while True:
            page = self.ddb.scan(**kwargs)
            sessions.extend(_session(from_item(i)) for i in page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return sorted(sessions, key=lambda s: s["started_at"], reverse=True)

    def create_session(
        self, saga_id: str, *, status: str, fields: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Crea la sesión. Devuelve (sesión, creada). Si ya existía, creada = False."""
        now = _now().isoformat(timespec="seconds")
        try:
            self.ddb.put_item(
                TableName=self.table,
                Item=to_item(
                    {
                        "pk": f"SESSION#{saga_id}",
                        "sk": "META",
                        "saga_id": saga_id,
                        "status": status,
                        "started_at": now,
                        "updated_at": now,
                        **fields,
                    }
                ),
                ConditionExpression="attribute_not_exists(pk)",
            )
        except self.ddb.exceptions.ConditionalCheckFailedException:
            return self.require_session(saga_id), False
        return self.require_session(saga_id), True

    def complete(self, saga_id: str, energy_kwh: float) -> dict[str, Any]:
        session = self.require_session(saga_id)
        if session["status"] != ACTIVE:
            return session  # idempotente: ya terminó (o nunca arrancó)
        now = _now().isoformat(timespec="seconds")
        with contextlib.suppress(self.ddb.exceptions.ConditionalCheckFailedException):
            self.ddb.update_item(
                TableName=self.table,
                Key=_session_key(saga_id),
                ConditionExpression="#s = :active",
                UpdateExpression=(
                    "SET #s = :done, ended_at = :now, updated_at = :now, energy_kwh = :e"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=to_item(
                    {":active": ACTIVE, ":done": COMPLETED, ":now": now, ":e": str(energy_kwh)}
                ),
            )
        return self.require_session(saga_id)

    def put_telemetry(self, saga_id: str, data: dict[str, Any]) -> None:
        ts = _now()
        self.ddb.put_item(
            TableName=self.table,
            Item=to_item(
                {
                    "pk": f"SESSION#{saga_id}",
                    "sk": f"TELEMETRY#{ts.isoformat(timespec='milliseconds')}",
                    "expires_at": int(time.time()) + TELEMETRY_TTL_SECONDS,
                    **{k: str(v) if isinstance(v, float) else v for k, v in data.items()},
                }
            ),
        )


def _session(item: dict[str, Any]) -> dict[str, Any]:
    data = {k: v for k, v in item.items() if k not in ("pk", "sk")}
    if "energy_kwh" in data:
        data["energy_kwh"] = float(data["energy_kwh"])
    return data
