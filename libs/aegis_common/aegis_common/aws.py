"""Clientes de AWS configurados para el ambiente (Floci o AWS real)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config

_BOTO_CONFIG = Config(retries={"max_attempts": 4, "mode": "standard"}, connect_timeout=3)
_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


@lru_cache(maxsize=32)
def _client(service: str, region: str, endpoint: str | None) -> Any:
    return boto3.client(service, region_name=region, endpoint_url=endpoint, config=_BOTO_CONFIG)


def client(service: str, settings: Any) -> Any:
    return _client(service, settings.region, settings.aws_endpoint_url)


def to_item(data: dict[str, Any]) -> dict[str, Any]:
    """dict de Python → formato tipado de DynamoDB ({"S": ...}, {"N": ...})."""
    return {k: _serializer.serialize(v) for k, v in data.items() if v is not None}


def from_item(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {k: _plain(_deserializer.deserialize(v)) for k, v in item.items()}


def _plain(value: Any) -> Any:
    # DynamoDB devuelve Decimal; para la API usamos int/float.
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | set):
        return [_plain(v) for v in value]
    return value


def cancellation_codes(exc: Exception) -> list[str]:
    """Códigos por operación de un TransactionCanceledException (en el orden de la transacción)."""
    reasons = getattr(exc, "response", {}).get("CancellationReasons", [])
    return [r.get("Code", "None") for r in reasons]
