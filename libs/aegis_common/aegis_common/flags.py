"""Feature flags del ambiente, guardadas en la tabla aegisvolt-flags-<env>.

Cada ambiente tiene SU tabla, así que cambiar una flag en dev no puede afectar a prod.
Lectura con caché de TTL corto (5 s): un cambio se refleja en todos los servicios en
segundos, sin redesplegar ni reiniciar nada.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aegis_common.aws import client, from_item, to_item

SURGE_PRICING = "ENABLE_DYNAMIC_SURGE_PRICING"
FAST_CHARGE_BOOST = "ENABLE_FAST_CHARGE_BOOST"
SIMULATE_CHARGER_FAULT = "SIMULATE_CHARGER_HARDWARE_FAULT"
KNOWN_FLAGS = (SURGE_PRICING, FAST_CHARGE_BOOST, SIMULATE_CHARGER_FAULT)


@dataclass(frozen=True, slots=True)
class Flag:
    name: str
    enabled: bool
    description: str = ""
    updated_by: str = ""
    updated_at: str = ""


class FlagStore:
    def __init__(self, settings: Any, ttl_seconds: float = 5.0, ddb: Any | None = None) -> None:
        self.table = settings.table("flags")
        self.ttl = ttl_seconds
        self._ddb = ddb or client("dynamodb", settings)
        self._lock = threading.Lock()
        self._cache: dict[str, Flag] = {}
        self._loaded_at = 0.0

    def _refresh(self) -> None:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"TableName": self.table}
        while True:
            page = self._ddb.scan(**kwargs)
            items.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        flags = {}
        for raw in items:
            data = from_item(raw) or {}
            flags[data["pk"]] = Flag(
                name=data["pk"],
                enabled=bool(data.get("enabled", False)),
                description=data.get("description", ""),
                updated_by=data.get("updated_by", ""),
                updated_at=data.get("updated_at", ""),
            )
        self._cache = flags
        self._loaded_at = time.monotonic()

    def all(self, *, fresh: bool = False) -> dict[str, Flag]:
        with self._lock:
            if fresh or time.monotonic() - self._loaded_at > self.ttl:
                self._refresh()
            return dict(self._cache)

    def enabled(self, name: str) -> bool:
        flag = self.all().get(name)
        return bool(flag and flag.enabled)  # flag inexistente = apagada (falla segura)

    def set(self, name: str, enabled: bool, updated_by: str) -> Flag:
        if name not in KNOWN_FLAGS:
            raise ValueError(f"Flag desconocida: {name}")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self._ddb.update_item(
            TableName=self.table,
            Key=to_item({"pk": name}),
            UpdateExpression="SET enabled = :e, updated_by = :u, updated_at = :t",
            ExpressionAttributeValues=to_item({":e": enabled, ":u": updated_by, ":t": now}),
        )
        with self._lock:
            self._loaded_at = 0.0  # invalidar: este proceso ve el cambio de inmediato
        return self.all()[name]
