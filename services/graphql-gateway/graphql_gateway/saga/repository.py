"""Bitácora de la SAGA en aegisvolt-sagas-<env> (dueño exclusivo: el orquestador).

Ítems:
  pk=SAGA#<id>  sk=META                       estado actual + datos de la solicitud
  pk=SAGA#<id>  sk=EVENT#<timestamp>#<n>      cada acción/compensación con su resultado

GSI by-status (gsi1pk=STATUS#<estado>, gsi1sk=updated_at): Prefect consulta
"todas las sagas COMPENSATING/COMPENSATION_FAILED" sin escanear la tabla.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

from aegis_common.aws import from_item, to_item
from graphql_gateway.saga.models import SagaRequest, SagaStatus

_seq = itertools.count()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _key(saga_id: str) -> dict[str, Any]:
    return to_item({"pk": f"SAGA#{saga_id}", "sk": "META"})


class SagaRepository:
    def __init__(self, ddb: Any, table: str) -> None:
        self.ddb = ddb
        self.table = table

    def create(
        self, saga_id: str, req: SagaRequest, correlation_id: str | None
    ) -> tuple[dict[str, Any], bool]:
        now = _now()
        try:
            self.ddb.put_item(
                TableName=self.table,
                Item=to_item(
                    {
                        "pk": f"SAGA#{saga_id}",
                        "sk": "META",
                        "saga_id": saga_id,
                        "status": SagaStatus.STARTED.value,
                        "gsi1pk": f"STATUS#{SagaStatus.STARTED}",
                        "gsi1sk": now,
                        "station_id": req.station_id,
                        "connector_id": req.connector_id,
                        "fleet_id": req.fleet_id,
                        "kwh": str(req.kwh),
                        "idempotency_key": req.idempotency_key,
                        "correlation_id": correlation_id,
                        "created_at": now,
                        "updated_at": now,
                    }
                ),
                ConditionExpression="attribute_not_exists(pk)",
            )
        except self.ddb.exceptions.ConditionalCheckFailedException:
            return self.get(saga_id), False  # type: ignore[return-value]
        return self.get(saga_id), True  # type: ignore[return-value]

    def set_status(self, saga_id: str, status: SagaStatus, **fields: Any) -> None:
        now = _now()
        values: dict[str, Any] = {
            ":s": status.value,
            ":g": f"STATUS#{status}",
            ":now": now,
        }
        sets = ["#s = :s", "gsi1pk = :g", "gsi1sk = :now", "updated_at = :now"]
        names = {"#s": "status"}
        present = {k: v for k, v in fields.items() if v is not None}
        for i, (name, value) in enumerate(present.items()):
            sets.append(f"#f{i} = :f{i}")
            names[f"#f{i}"] = name
            values[f":f{i}"] = str(value) if isinstance(value, float) else value
        self.ddb.update_item(
            TableName=self.table,
            Key=_key(saga_id),
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=to_item(values),
        )

    def add_event(
        self, saga_id: str, step: str, action: str, outcome: str, detail: str = ""
    ) -> None:
        now = _now()
        self.ddb.put_item(
            TableName=self.table,
            Item=to_item(
                {
                    "pk": f"SAGA#{saga_id}",
                    "sk": f"EVENT#{now}#{next(_seq):06d}",
                    "at": now,
                    "step": step,
                    "action": action,  # "action" | "compensation"
                    "outcome": outcome,  # "OK" | "FAILED" | "SKIPPED"
                    "detail": detail[:500],
                }
            ),
        )

    def get(self, saga_id: str) -> dict[str, Any] | None:
        page = self.ddb.query(
            TableName=self.table,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues=to_item({":pk": f"SAGA#{saga_id}"}),
            ConsistentRead=True,
        )
        items = [from_item(i) for i in page.get("Items", [])]
        meta = next((i for i in items if i["sk"] == "META"), None)
        if meta is None:
            return None
        return _saga(meta, [i for i in items if i["sk"].startswith("EVENT#")])

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "TableName": self.table,
            "FilterExpression": "sk = :meta",
            "ExpressionAttributeValues": to_item({":meta": "META"}),
        }
        metas: list[dict[str, Any]] = []
        while True:
            page = self.ddb.scan(**kwargs)
            metas.extend(from_item(i) for i in page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        metas.sort(key=lambda m: m["created_at"], reverse=True)
        return [_saga(m, []) for m in metas[:limit]]

    def list_by_status(self, status: SagaStatus) -> list[dict[str, Any]]:
        page = self.ddb.query(
            TableName=self.table,
            IndexName="by-status",
            KeyConditionExpression="gsi1pk = :g",
            ExpressionAttributeValues=to_item({":g": f"STATUS#{status}"}),
        )
        return [_saga(from_item(i), []) for i in page.get("Items", [])]


def _saga(meta: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    data = {k: v for k, v in meta.items() if k not in ("pk", "sk", "gsi1pk", "gsi1sk")}
    data["kwh"] = float(data.get("kwh", 0))
    data["events"] = sorted(
        ({k: v for k, v in e.items() if k not in ("pk", "sk")} for e in events),
        key=lambda e: e["at"],
    )
    return data
