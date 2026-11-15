"""Acceso a la tabla aegisvolt-billing-<env> (dueño exclusivo: este servicio).

Ítems:
  pk=WALLET#<fleet>  sk=META          balance_cents, currency, name
  pk=WALLET#<fleet>  sk=TXN#<saga>    CHARGE: amount_cents, status CHARGED|REFUNDED

Cobro (paso 2) y reembolso (compensación) son TransactWriteItems atómicos:

  cobro:     Put TXN       si attribute_not_exists(pk)      ← idempotencia por saga_id
             Update saldo  si balance_cents >= :monto      ← fondos suficientes
  reembolso: Update TXN    si status = CHARGED  → REFUNDED  ← idempotencia: 2.º intento falla
             Update saldo  += monto                            la condición y no reembolsa doble
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

from aegis_common.aws import cancellation_codes, from_item, to_item
from aegis_common.errors import Conflict, InsufficientFunds, NotFound
from billing_wallet.pricing import Quote

CHARGED, REFUNDED = "CHARGED", "REFUNDED"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _wallet_key(fleet_id: str) -> dict[str, Any]:
    return to_item({"pk": f"WALLET#{fleet_id}", "sk": "META"})


def _txn_key(fleet_id: str, saga_id: str) -> dict[str, Any]:
    return to_item({"pk": f"WALLET#{fleet_id}", "sk": f"TXN#{saga_id}"})


class BillingRepository:
    def __init__(self, ddb: Any, table: str) -> None:
        self.ddb = ddb
        self.table = table

    def get_wallet(self, fleet_id: str) -> dict[str, Any]:
        raw = self.ddb.get_item(
            TableName=self.table, Key=_wallet_key(fleet_id), ConsistentRead=True
        ).get("Item")
        if not raw:
            raise NotFound(f"Billetera de la flota {fleet_id} no existe")
        item = from_item(raw)
        return {
            "fleet_id": fleet_id,
            "name": item.get("name", fleet_id),
            "balance_cents": item["balance_cents"],
            "currency": item.get("currency", "USD"),
        }

    def list_transactions(self, fleet_id: str) -> list[dict[str, Any]]:
        page = self.ddb.query(
            TableName=self.table,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :txn)",
            ExpressionAttributeValues=to_item({":pk": f"WALLET#{fleet_id}", ":txn": "TXN#"}),
            ScanIndexForward=False,
        )
        txns = [_txn(from_item(i)) for i in page.get("Items", [])]
        return sorted(txns, key=lambda t: t["created_at"], reverse=True)

    def get_transaction(self, fleet_id: str, saga_id: str) -> dict[str, Any] | None:
        raw = self.ddb.get_item(
            TableName=self.table, Key=_txn_key(fleet_id, saga_id), ConsistentRead=True
        ).get("Item")
        return _txn(from_item(raw)) if raw else None

    # --- Paso 2 de la SAGA: cobrar -----------------------------------------------------
    def charge(self, saga_id: str, fleet_id: str, q: Quote) -> dict[str, Any]:
        self.get_wallet(fleet_id)  # 404 si la flota no existe
        existing = self.get_transaction(fleet_id, saga_id)
        if existing:  # misma saga → mismo cobro, sin descontar dos veces
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
                                    "status": CHARGED,
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

    # --- Compensación del paso 2: reembolsar -------------------------------------------
    def refund(self, saga_id: str, fleet_id: str, reason: str) -> dict[str, Any]:
        txn = self.get_transaction(fleet_id, saga_id)
        if txn is None:  # el cobro nunca ocurrió: nada que reembolsar
            return {"saga_id": saga_id, "refunded": False, "detail": "sin cobro previo"}
        if txn["status"] == REFUNDED:
            return {**txn, "refunded": False, "detail": "ya reembolsado (idempotente)"}
        now = _now()
        try:
            self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.table,
                            "Key": _txn_key(fleet_id, saga_id),
                            "ConditionExpression": "#s = :charged",
                            "UpdateExpression": (
                                "SET #s = :refunded, refund_reason = :reason, "
                                "refunded_at = :now, updated_at = :now"
                            ),
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": to_item(
                                {
                                    ":charged": CHARGED,
                                    ":refunded": REFUNDED,
                                    ":reason": reason,
                                    ":now": now,
                                }
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table,
                            "Key": _wallet_key(fleet_id),
                            "UpdateExpression": "SET balance_cents = balance_cents + :amount",
                            "ExpressionAttributeValues": to_item({":amount": txn["amount_cents"]}),
                        }
                    },
                ]
            )
        except self.ddb.exceptions.TransactionCanceledException as exc:
            if cancellation_codes(exc)[:1] == ["ConditionalCheckFailed"]:
                # Otra ejecución reembolsó entre la lectura y la escritura: no se duplica.
                current = self.get_transaction(fleet_id, saga_id)
                return {**current, "refunded": False, "detail": "ya reembolsado (idempotente)"}
            raise
        return {**self.get_transaction(fleet_id, saga_id), "refunded": True, "detail": reason}

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
    for key in ("kwh", "surge_multiplier"):
        if key in data:
            data[key] = float(data[key])
    return data
