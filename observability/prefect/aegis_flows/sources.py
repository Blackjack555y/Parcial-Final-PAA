"""Lectura del estado de un ambiente: DynamoDB, CloudWatch y el gateway (vía Ingress).

Los flujos de Prefect SOLO leen (salvo la remediación opcional, que usa la API pública del
gateway): la auditoría no escribe directamente en las tablas de los servicios.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import httpx

from aegis_common.aws import from_item
from aegis_flows.rules import Snapshot

TABLES = ("reservations", "billing", "dispatch", "sagas", "flags")
SERVICES = (
    "graphql-gateway",
    "station-reservation-service",
    "billing-wallet-service",
    "energy-dispatch-service",
)


def _client(service: str):
    return boto3.client(
        service,
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )


def table_name(resource: str, env: str) -> str:
    return f"{os.environ.get('AEGIS_TABLE_PREFIX', 'aegisvolt')}-{resource}-{env}"


def _scan(table: str) -> list[dict[str, Any]]:
    ddb = _client("dynamodb")
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"TableName": table}
    while True:
        page = ddb.scan(**kwargs)
        items.extend(from_item(i) for i in page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def snapshot(env: str) -> Snapshot:
    """Foto de las 4 tablas de un ambiente (la saga y lo que cada servicio dice de ella)."""
    sagas = {i["saga_id"]: i for i in _scan(table_name("sagas", env)) if i.get("sk") == "META"}
    reservations: dict[str, dict[str, Any]] = {}
    connectors: list[dict[str, Any]] = []
    for item in _scan(table_name("reservations", env)):
        if item["pk"].startswith("RESERVATION#"):
            reservations[item["saga_id"]] = item
        elif item["sk"].startswith("CONNECTOR#"):
            connectors.append(
                {
                    "station_id": item["pk"].removeprefix("STATION#"),
                    "connector_id": item["sk"].removeprefix("CONNECTOR#"),
                    "status": item.get("status"),
                    "reserved_by": item.get("reserved_by"),
                }
            )
    charges = {
        i["saga_id"]: i
        for i in _scan(table_name("billing", env))
        if i.get("sk", "").startswith("TXN#")
    }
    sessions = {
        i["saga_id"]: i for i in _scan(table_name("dispatch", env)) if i.get("sk") == "META"
    }
    return Snapshot(sagas, reservations, connectors, charges, sessions)


def table_status(env: str) -> dict[str, str]:
    ddb = _client("dynamodb")
    status = {}
    for resource in TABLES:
        name = table_name(resource, env)
        try:
            status[name] = ddb.describe_table(TableName=name)["Table"]["TableStatus"]
        except ddb.exceptions.ResourceNotFoundException:
            status[name] = "MISSING"
    return status


def flags(env: str) -> dict[str, bool]:
    return {i["pk"]: bool(i.get("enabled")) for i in _scan(table_name("flags", env))}


def recent_5xx(env: str, minutes: int = 15) -> int:
    """Suma de HttpErrors (metric filter de Terraform) de todas las versiones y servicios."""
    cw = _client("cloudwatch")
    end = datetime.now(UTC)
    start = end - timedelta(minutes=minutes)
    total = 0.0
    metrics = cw.list_metrics(Namespace=f"AegisVolt/{env}", MetricName="HttpErrors")
    for metric in metrics.get("Metrics", []):
        stats = cw.get_metric_statistics(
            Namespace=f"AegisVolt/{env}",
            MetricName="HttpErrors",
            Dimensions=metric["Dimensions"],
            StartTime=start,
            EndTime=end,
            Period=60,
            Statistics=["Sum"],
        )
        total += sum(dp["Sum"] for dp in stats.get("Datapoints", []))
    return int(total)


def gateway(env: str) -> dict[str, Any]:
    """Salud del ambiente por su puerta de entrada real: Ingress → gateway → microservicios."""
    base = os.environ.get("INGRESS_URL", "http://minikube")
    headers = {"host": f"api.{env}.aegisvolt.local"}
    start = time.perf_counter()
    try:
        with httpx.Client(base_url=base, headers=headers, timeout=10) as http:
            deep = http.get("/readyz/deep").json()
            info = http.post("/graphql", json={"query": "{ environment { name version } }"}).json()
        return {
            "gateway_ready": bool(deep.get("ready")),
            "services": deep.get("services", {}),
            "version": (info.get("data") or {}).get("environment", {}).get("version"),
            "latency_ms": round((time.perf_counter() - start) * 1000),
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {"gateway_ready": False, "services": {}, "error": str(exc)[:200]}


def put_metric(env: str, name: str, value: float) -> None:
    """Métrica propia de la observabilidad (namespace AegisVolt/<env>, sin dimensiones)."""
    _client("cloudwatch").put_metric_data(
        Namespace=f"AegisVolt/{env}",
        MetricData=[{"MetricName": name, "Value": value, "Unit": "Count"}],
    )


def retry_compensation(env: str, saga_id: str) -> str:
    """Remediación: re-ejecuta las compensaciones (idempotentes) por la API del gateway."""
    token = os.environ.get(f"ADMIN_TOKEN_{env.upper()}", "")
    if not token:
        return "sin token admin para este ambiente"
    base = os.environ.get("INGRESS_URL", "http://minikube")
    query = "mutation($s: String!) { retryCompensation(sagaId: $s) { saga { status } } }"
    response = httpx.post(
        f"{base}/graphql",
        headers={"host": f"api.{env}.aegisvolt.local", "x-admin-token": token},
        json={"query": query, "variables": {"s": saga_id}},
        timeout=30,
    ).json()
    if response.get("errors"):
        return f"error: {response['errors'][0]['message']}"
    return response["data"]["retryCompensation"]["saga"]["status"]
