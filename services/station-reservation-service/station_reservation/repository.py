"""Acceso a la tabla aegisvolt-reservations-<env> (dueño exclusivo: este servicio).

Ítems:
  pk=STATION#<id>      sk=META                 nombre, ciudad, dirección
  pk=STATION#<id>      sk=CONNECTOR#<id>       tipo, potencia, status, reserved_by (saga_id)
  pk=RESERVATION#<sid> sk=META                 status RESERVED|CHARGING|RELEASED|COMPLETED

Estados del conector: AVAILABLE → RESERVED → CHARGING → AVAILABLE   (o FAULTED)

Idempotencia: el saga_id es la llave. Reservar dos veces con el mismo saga_id devuelve la
misma reserva; liberar dos veces no hace nada la segunda vez. Las escrituras son condicionales
y van en TransactWriteItems (conector + registro de reserva cambian juntos o no cambian).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

from aegis_common.aws import cancellation_codes, from_item, to_item
from aegis_common.errors import Conflict, NotFound

AVAILABLE, RESERVED, CHARGING, FAULTED = "AVAILABLE", "RESERVED", "CHARGING", "FAULTED"
CLOSED = ("RELEASED", "COMPLETED")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _connector_key(station_id: str, connector_id: str) -> dict[str, Any]:
    return to_item({"pk": f"STATION#{station_id}", "sk": f"CONNECTOR#{connector_id}"})


def _reservation_key(saga_id: str) -> dict[str, Any]:
    return to_item({"pk": f"RESERVATION#{saga_id}", "sk": "META"})


class ReservationRepository:
    def __init__(self, ddb: Any, table: str) -> None:
        self.ddb = ddb
        self.table = table

    # --- Lecturas ----------------------------------------------------------------------
    def list_stations(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "TableName": self.table,
            "FilterExpression": "begins_with(pk, :p)",
            "ExpressionAttributeValues": to_item({":p": "STATION#"}),
        }
        while True:
            page = self.ddb.scan(**kwargs)
            items.extend(from_item(i) for i in page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        stations: dict[str, dict[str, Any]] = {}
        for item in items:
            sid = item["pk"].removeprefix("STATION#")
            station = stations.setdefault(sid, {"station_id": sid, "connectors": []})
            if item["sk"] == "META":
                station.update({k: v for k, v in item.items() if k not in ("pk", "sk")})
            else:
                station["connectors"].append(_connector(item))
        for station in stations.values():
            station["connectors"].sort(key=lambda c: c["connector_id"])
        return sorted(stations.values(), key=lambda s: s["station_id"])

    def get_station(self, station_id: str) -> dict[str, Any]:
        for station in self.list_stations():
            if station["station_id"] == station_id:
                return station
        raise NotFound(f"Estación {station_id} no existe")

    def get_connector(self, station_id: str, connector_id: str) -> dict[str, Any]:
        raw = self.ddb.get_item(
            TableName=self.table, Key=_connector_key(station_id, connector_id), ConsistentRead=True
        ).get("Item")
        if not raw:
            raise NotFound(f"Conector {station_id}/{connector_id} no existe")
        return _connector(from_item(raw))

    def get_reservation(self, saga_id: str) -> dict[str, Any] | None:
        raw = self.ddb.get_item(
            TableName=self.table, Key=_reservation_key(saga_id), ConsistentRead=True
        ).get("Item")
        return _reservation(from_item(raw)) if raw else None

    # --- Paso 1 de la SAGA: reservar ----------------------------------------------------
    def reserve(
        self, saga_id: str, station_id: str, connector_id: str, fleet_id: str
    ) -> dict[str, Any]:
        existing = self.get_reservation(saga_id)
        if existing:  # reintento de la misma saga → misma respuesta (idempotencia)
            return existing
        connector = self.get_connector(station_id, connector_id)
        now = _now()
        try:
            self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.table,
                            "Key": _connector_key(station_id, connector_id),
                            "ConditionExpression": "#s = :available",
                            "UpdateExpression": (
                                "SET #s = :reserved, reserved_by = :saga, fleet_id = :fleet, "
                                "updated_at = :now"
                            ),
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": to_item(
                                {
                                    ":available": AVAILABLE,
                                    ":reserved": RESERVED,
                                    ":saga": saga_id,
                                    ":fleet": fleet_id,
                                    ":now": now,
                                }
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table,
                            "Item": to_item(
                                {
                                    "pk": f"RESERVATION#{saga_id}",
                                    "sk": "META",
                                    "saga_id": saga_id,
                                    "station_id": station_id,
                                    "connector_id": connector_id,
                                    "connector_type": connector["type"],
                                    "power_kw": connector["power_kw"],
                                    "fleet_id": fleet_id,
                                    "status": RESERVED,
                                    "created_at": now,
                                    "updated_at": now,
                                }
                            ),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
        except self.ddb.exceptions.TransactionCanceledException as exc:
            codes = cancellation_codes(exc)
            if len(codes) > 1 and codes[1] == "ConditionalCheckFailed":
                # Otra petición con el mismo saga_id ganó la carrera: devolver su reserva.
                return self.get_reservation(saga_id)  # type: ignore[return-value]
            current = self.get_connector(station_id, connector_id)
            raise Conflict(
                f"Conector {station_id}/{connector_id} no disponible (estado {current['status']})",
                code="CONNECTOR_UNAVAILABLE",
            ) from exc
        return self.get_reservation(saga_id)  # type: ignore[return-value]

    # --- El cargador arrancó: el conector pasa a CHARGING --------------------------------
    def occupy(self, saga_id: str) -> dict[str, Any]:
        reservation = self._require(saga_id)
        if reservation["status"] == CHARGING:
            return reservation  # idempotente
        return self._transition(reservation, to_status=CHARGING, connector_status=CHARGING)

    # --- Compensación del paso 1 / fin de la sesión -----------------------------------
    def release(self, saga_id: str, reason: str, *, completed: bool = False) -> dict[str, Any]:
        """Libera el conector. Idempotente: si ya se liberó (o nunca se reservó), no hace nada."""
        reservation = self.get_reservation(saga_id)
        if reservation is None:
            return {"saga_id": saga_id, "released": False, "detail": "sin reserva previa"}
        if reservation["status"] in CLOSED:
            return {**reservation, "released": False, "detail": "ya liberada (idempotente)"}
        updated = self._transition(
            reservation,
            to_status="COMPLETED" if completed else "RELEASED",
            connector_status=AVAILABLE,
            reason=reason,
        )
        return {**updated, "released": True, "detail": reason}

    def _require(self, saga_id: str) -> dict[str, Any]:
        reservation = self.get_reservation(saga_id)
        if reservation is None:
            raise NotFound(f"Reserva de la saga {saga_id} no existe")
        return reservation

    def _transition(
        self,
        reservation: dict[str, Any],
        *,
        to_status: str,
        connector_status: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        saga_id = reservation["saga_id"]
        now = _now()
        if connector_status == AVAILABLE:
            connector_expr = "SET #s = :cs, updated_at = :now REMOVE reserved_by, fleet_id"
        else:
            connector_expr = "SET #s = :cs, updated_at = :now"
        connector_update = {
            "TableName": self.table,
            "Key": _connector_key(reservation["station_id"], reservation["connector_id"]),
            "ConditionExpression": "reserved_by = :saga",
            "UpdateExpression": connector_expr,
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": to_item(
                {":saga": saga_id, ":cs": connector_status, ":now": now}
            ),
        }
        values: dict[str, Any] = {":rs": to_status, ":now": now, ":from": reservation["status"]}
        update_expr = "SET #s = :rs, updated_at = :now"
        if reason:
            update_expr += ", reason = :reason"
            values[":reason"] = reason
        reservation_update = {
            "TableName": self.table,
            "Key": _reservation_key(saga_id),
            "ConditionExpression": "#s = :from",
            "UpdateExpression": update_expr,
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": to_item(values),
        }
        try:
            self.ddb.transact_write_items(
                TransactItems=[{"Update": connector_update}, {"Update": reservation_update}]
            )
        except self.ddb.exceptions.TransactionCanceledException as exc:
            codes = cancellation_codes(exc)
            if codes[:1] == ["ConditionalCheckFailed"] and codes[1:2] != ["ConditionalCheckFailed"]:
                # El conector ya no está a nombre de esta saga: solo se cierra la reserva.
                self.ddb.update_item(**reservation_update)
            else:
                raise Conflict(
                    f"La reserva {saga_id} cambió de estado concurrentemente",
                    code="RESERVATION_STATE_CHANGED",
                ) from exc
        return self._require(saga_id)

    # --- Carga inicial (scripts/seed.py) -----------------------------------------------
    def put_station(self, station: dict[str, Any], *, reset: bool = False) -> None:
        sid = station["station_id"]
        meta = {k: v for k, v in station.items() if k not in ("station_id", "connectors")}
        self.ddb.put_item(
            TableName=self.table,
            Item=to_item({"pk": f"STATION#{sid}", "sk": "META", **meta}),
        )
        for connector in station["connectors"]:
            item = to_item(
                {
                    "pk": f"STATION#{sid}",
                    "sk": f"CONNECTOR#{connector['connector_id']}",
                    "type": connector["type"],
                    "power_kw": connector["power_kw"],
                    "status": connector.get("status", AVAILABLE),
                    "updated_at": _now(),
                }
            )
            kwargs: dict[str, Any] = {"TableName": self.table, "Item": item}
            if not reset:
                kwargs["ConditionExpression"] = "attribute_not_exists(pk)"
            # Sin --reset no se pisa el estado actual del conector.
            with contextlib.suppress(self.ddb.exceptions.ConditionalCheckFailedException):
                self.ddb.put_item(**kwargs)


def _connector(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "station_id": item["pk"].removeprefix("STATION#"),
        "connector_id": item["sk"].removeprefix("CONNECTOR#"),
        "type": item.get("type"),
        "power_kw": item.get("power_kw"),
        "status": item.get("status"),
        "reserved_by": item.get("reserved_by"),
        "fleet_id": item.get("fleet_id"),
        "updated_at": item.get("updated_at"),
    }


def _reservation(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k not in ("pk", "sk")}
