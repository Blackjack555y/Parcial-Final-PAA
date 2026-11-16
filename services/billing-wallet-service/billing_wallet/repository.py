"""Acceso a la tabla aegisvolt-billing-<env> (dueño exclusivo: este servicio).

Ítems:
  pk=WALLET#<fleet>  sk=META          balance_cents (saldo DISPONIBLE), currency, name
  pk=WALLET#<fleet>  sk=TXN#<saga>    retención de la saga y su liquidación

Ciclo de una transacción (como la pre-autorización de una tarjeta en una gasolinera):

  HELD ──capture(energía real)──► CAPTURED   cobra lo entregado y devuelve la diferencia
   │
   └──release (compensación)────► RELEASED   devuelve todo lo retenido

  (Transacciones antiguas: CHARGED ≈ HELD y REFUNDED ≈ RELEASED; se siguen aceptando.)

Cada transición es UN TransactWriteItems condicional: la transacción cambia de estado y el saldo
se ajusta juntos, o no cambia nada. La condición sobre el estado hace todo idempotente: liberar o
liquidar dos veces no devuelve ni cobra dos veces.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from aegis_common.aws import cancellation_codes, from_item, to_item
from aegis_common.errors import Conflict, InsufficientFunds, NotFound
from billing_wallet.pricing import Quote

HELD, CAPTURED, RELEASED = "HELD", "CAPTURED", "RELEASED"
LEGACY_HELD, LEGACY_RELEASED = "CHARGED", "REFUNDED"
OPEN = (HELD, LEGACY_HELD)  # estados con dinero retenido de la flota
CLOSED = (CAPTURED, RELEASED, LEGACY_RELEASED)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _wallet_key(fleet_id: str) -> dict[str, Any]:
    return to_item({"pk": f"WALLET#{fleet_id}", "sk": "META"})


def _txn_key(fleet_id: str, saga_id: str) -> dict[str, Any]:
    return to_item({"pk": f"WALLET#{fleet_id}", "sk": f"TXN#{saga_id}"})


def captured_amount(held_cents: int, unit_price_cents: int, energy_kwh: float) -> int:
    """Cobro final = energía real × precio unitario, nunca más de lo retenido."""
    real = (Decimal(str(max(0.0, energy_kwh))) * unit_price_cents).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    return min(held_cents, int(real))


class BillingRepository:
    def __init__(self, ddb: Any, table: str) -> None:
        self.ddb = ddb
        self.table = table

    # --- Lecturas ----------------------------------------------------------------------
    def get_wallet(self, fleet_id: str) -> dict[str, Any]:
        raw = self.ddb.get_item(
            TableName=self.table, Key=_wallet_key(fleet_id), ConsistentRead=True
        ).get("Item")
        if not raw:
            raise NotFound(f"Billetera de la flota {fleet_id} no existe")
        item = from_item(raw)
        held = sum(t["amount_cents"] for t in self.list_transactions(fleet_id) if t["open"])
        return {
            "fleet_id": fleet_id,
            "name": item.get("name", fleet_id),
            "balance_cents": item["balance_cents"],
            "held_cents": held,
            "currency": item.get("currency", "USD"),
        }

    def list_transactions(self, fleet_id: str) -> list[dict[str, Any]]:
        page = self.ddb.query(
            TableName=self.table,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :txn)",
            ExpressionAttributeValues=to_item({":pk": f"WALLET#{fleet_id}", ":txn": "TXN#"}),
        )
        txns = [_txn(from_item(i)) for i in page.get("Items", [])]
        return sorted(txns, key=lambda t: t["created_at"], reverse=True)

    def get_transaction(self, fleet_id: str, saga_id: str) -> dict[str, Any] | None:
        raw = self.ddb.get_item(
            TableName=self.table, Key=_txn_key(fleet_id, saga_id), ConsistentRead=True
        ).get("Item")
        return _txn(from_item(raw)) if raw else None

    # --- Paso 2 de la SAGA: retener el monto estimado ----------------------------------
    def hold(self, saga_id: str, fleet_id: str, q: Quote) -> dict[str, Any]:
        self.get_wallet(fleet_id)  # 404 si la flota no existe
        existing = self.get_transaction(fleet_id, saga_id)
        if existing:  # misma saga → misma retención, sin descontar dos veces
            return existing
        now = _now()
        try:
            self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table,
                            "Item": to_item(
                                {
                                    "pk": f"WALLET#{fleet_id}",
                                    "sk": f"TXN#{saga_id}",
                                    "saga_id": saga_id,
                                    "type": "CHARGE",
                                    "status": HELD,
                                    "amount_cents": q.amount_cents,
                                    "kwh": str(q.kwh),
                                    "unit_price_cents": q.unit_price_cents,
                                    "surge_active": q.surge_active,
                                    "surge_multiplier": str(q.surge_multiplier),
                                    "connector_type": q.connector_type,
                                    "created_at": now,
                                    "updated_at": now,
                                }
                            ),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table,
                            "Key": _wallet_key(fleet_id),
                            "ConditionExpression": "balance_cents >= :amount",
                            "UpdateExpression": "SET balance_cents = balance_cents - :amount",
                            "ExpressionAttributeValues": to_item({":amount": q.amount_cents}),
                        }
                    },
                ]
            )
        except self.ddb.exceptions.TransactionCanceledException as exc:
            codes = cancellation_codes(exc)
            if codes[:1] == ["ConditionalCheckFailed"]:
                return self.get_transaction(fleet_id, saga_id)  # carrera con la misma saga
            if codes[1:2] == ["ConditionalCheckFailed"]:
                wallet = self.get_wallet(fleet_id)
                raise InsufficientFunds(
                    f"Saldo insuficiente: disponible {wallet['balance_cents']} ¢, "
                    f"requerido {q.amount_cents} ¢"
                ) from exc
            raise
        return self.get_transaction(fleet_id, saga_id)  # type: ignore[return-value]

    # --- Compensación del paso 2: liberar la retención ---------------------------------
    def release(self, saga_id: str, fleet_id: str, reason: str) -> dict[str, Any]:
        txn = self.get_transaction(fleet_id, saga_id)
        if txn is None:  # la retención nunca ocurrió: nada que liberar
            return {"saga_id": saga_id, "released": False, "detail": "sin retención previa"}
        if not txn["open"]:
            return {**txn, "released": False, "detail": f"ya {txn['status']} (idempotente)"}
        updated = self._close(
            txn,
            status=RELEASED,
            returned_cents=txn["amount_cents"],
            extra={":reason": reason, ":rel": txn["amount_cents"]},
            extra_expr="refund_reason = :reason, released_cents = :rel",
        )
        if updated is None:  # otra ejecución la cerró entre la lectura y la escritura
            current = self.get_transaction(fleet_id, saga_id)
            return {**current, "released": False, "detail": "ya liberada (idempotente)"}
        return {**updated, "released": True, "detail": reason}

    # --- Fin de la carga: liquidar lo realmente entregado -------------------------------
    def capture(self, saga_id: str, fleet_id: str, energy_kwh: float) -> dict[str, Any]:
        txn = self.get_transaction(fleet_id, saga_id)
        if txn is None:
            raise NotFound(f"La saga {saga_id} no tiene retención en la flota {fleet_id}")
        if not txn["open"]:
            if txn["status"] == CAPTURED:
                return {**txn, "captured": False, "detail": "ya liquidada (idempotente)"}
            raise Conflict(f"La retención está {txn['status']}", code="HOLD_NOT_OPEN")
        captured = captured_amount(txn["amount_cents"], txn["unit_price_cents"], energy_kwh)
        returned = txn["amount_cents"] - captured
        updated = self._close(
            txn,
            status=CAPTURED,
            returned_cents=returned,
            extra={":cap": captured, ":rel": returned, ":e": str(round(energy_kwh, 3))},
            extra_expr="captured_cents = :cap, released_cents = :rel, energy_kwh = :e",
        )
        if updated is None:
            current = self.get_transaction(fleet_id, saga_id)
            return {**current, "captured": False, "detail": "ya liquidada (idempotente)"}
        detail = f"cobrado {captured} ¢, liberado {returned} ¢"
        return {**updated, "captured": True, "detail": detail}

    def _close(
        self,
        txn: dict[str, Any],
        *,
        status: str,
        returned_cents: int,
        extra: dict[str, Any],
        extra_expr: str,
    ) -> dict[str, Any] | None:
        """HELD → status y saldo += returned_cents, atómico. None si ya estaba cerrada."""
        fleet_id, saga_id = txn["fleet_id"], txn["saga_id"]
        now = _now()
        items: list[dict[str, Any]] = [
            {
                "Update": {
                    "TableName": self.table,
                    "Key": _txn_key(fleet_id, saga_id),
                    "ConditionExpression": "#s IN (:held, :legacy)",
                    "UpdateExpression": (
                        f"SET #s = :new, closed_at = :now, updated_at = :now, {extra_expr}"
                    ),
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": to_item(
                        {
                            ":held": HELD,
                            ":legacy": LEGACY_HELD,
                            ":new": status,
                            ":now": now,
                            **extra,
                        }
                    ),
                }
            }
        ]
        if returned_cents:
            items.append(
                {
                    "Update": {
                        "TableName": self.table,
                        "Key": _wallet_key(fleet_id),
                        "UpdateExpression": "SET balance_cents = balance_cents + :r",
                        "ExpressionAttributeValues": to_item({":r": returned_cents}),
                    }
                }
            )
        try:
            self.ddb.transact_write_items(TransactItems=items)
        except self.ddb.exceptions.TransactionCanceledException as exc:
            if cancellation_codes(exc)[:1] == ["ConditionalCheckFailed"]:
                return None
            raise
        return self.get_transaction(fleet_id, saga_id)

    # --- Administración (demo / seed) --------------------------------------------------
    def put_wallet(self, fleet_id: str, name: str, balance_cents: int, *, reset: bool) -> None:
        kwargs: dict[str, Any] = {
            "TableName": self.table,
            "Item": to_item(
                {
                    "pk": f"WALLET#{fleet_id}",
                    "sk": "META",
                    "name": name,
                    "balance_cents": balance_cents,
                    "currency": "USD",
                }
            ),
        }
        if not reset:
            kwargs["ConditionExpression"] = "attribute_not_exists(pk)"
        with contextlib.suppress(self.ddb.exceptions.ConditionalCheckFailedException):
            self.ddb.put_item(**kwargs)

    def top_up(self, fleet_id: str, amount_cents: int) -> dict[str, Any]:
        if amount_cents <= 0:
            raise Conflict("El monto debe ser positivo", code="INVALID_AMOUNT")
        try:
            self.ddb.update_item(
                TableName=self.table,
                Key=_wallet_key(fleet_id),
                ConditionExpression="attribute_exists(pk)",
                UpdateExpression="SET balance_cents = balance_cents + :a",
                ExpressionAttributeValues=to_item({":a": amount_cents}),
            )
        except self.ddb.exceptions.ConditionalCheckFailedException as exc:
            raise NotFound(f"Billetera de la flota {fleet_id} no existe") from exc
        return self.get_wallet(fleet_id)


def _txn(item: dict[str, Any]) -> dict[str, Any]:
    data = {k: v for k, v in item.items() if k not in ("pk", "sk")}
    data["fleet_id"] = item["pk"].removeprefix("WALLET#")
    for key in ("kwh", "surge_multiplier", "energy_kwh"):
        if key in data:
            data[key] = float(data[key])
    data["open"] = data.get("status") in OPEN
    return data
