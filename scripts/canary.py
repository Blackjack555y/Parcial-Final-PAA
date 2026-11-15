"""Análisis del despliegue canary.

    python scripts/canary.py traffic  --url URL --host HOST --seconds 60 --rps 5
    python scripts/canary.py evaluate --env dev --version TAG --since EPOCH

CONDICIÓN DE PROMOCIÓN del 20 % al 100 % (pregunta 6.2). Se evalúa sobre las métricas de
CloudWatch que generan los metric filters de Terraform a partir de los logs JSON:

    namespace  AegisVolt/<env>
    métricas   HttpRequests, HttpErrors (status >= 500)
    dimensión  Version = <etiqueta de la imagen canary>, Service = cada uno de los 4 servicios

    1. tráfico suficiente:  HttpRequests(gateway, canary) >= --min-requests     (por defecto 20)
    2. tasa de error:       Σ HttpErrors(canary) / Σ HttpRequests(canary) <= --max-error-rate (5 %)

Si ambas se cumplen → PROMOVER (exit 0). Si no → ROLLBACK (exit 1). Sin tráfico suficiente el
resultado es inconcluso y también se revierte: no se promueve lo que no se pudo medir.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime, timedelta

SERVICES = (
    "graphql-gateway",
    "station-reservation-service",
    "billing-wallet-service",
    "energy-dispatch-service",
)
QUERIES = (
    "{ environment { name version } }",
    "{ stations { stationId availableConnectors connectors { status } } }",
    "{ featureFlags { name enabled } }",
    '{ quote(connectorType: "CCS2", kwh: 30) { amountCents surgeActive } }',
)


# --- Tráfico sintético -------------------------------------------------------------------
def traffic(url: str, host: str, seconds: float, rps: float) -> int:
    """Envía consultas GraphQL de solo lectura por el Ingress y cuenta respuestas por versión."""
    versions: Counter[str] = Counter()
    statuses: Counter[int] = Counter()
    deadline = time.monotonic() + seconds
    i = 0
    while time.monotonic() < deadline:
        body = json.dumps({"query": QUERIES[i % len(QUERIES)]}).encode()
        request = urllib.request.Request(  # noqa: S310 — URL interna del port-forward
            f"{url}/graphql",
            data=body,
            headers={"content-type": "application/json", "host": host},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                statuses[response.status] += 1
                versions[response.headers.get("x-aegis-version", "?")] += 1
        except urllib.error.HTTPError as exc:
            statuses[exc.code] += 1
            versions[exc.headers.get("x-aegis-version", "?")] += 1
        except (urllib.error.URLError, TimeoutError):
            statuses[0] += 1
        i += 1
        time.sleep(1 / rps)
    total = sum(versions.values()) or 1
    print(f"Tráfico sintético: {i} peticiones a {host} en {seconds:.0f} s")
    for version, count in versions.most_common():
        print(f"  versión {version:<16} {count:5d}  ({count / total:.0%})")
    print(f"  códigos HTTP: {dict(statuses)}")
    return 0


# --- Evaluación --------------------------------------------------------------------------
def _sum(cw, env: str, metric: str, service: str, version: str, start, end) -> float:
    response = cw.get_metric_statistics(
        Namespace=f"AegisVolt/{env}",
        MetricName=metric,
        Dimensions=[
            {"Name": "Service", "Value": service},
            {"Name": "Version", "Value": version},
        ],
        StartTime=start,
        EndTime=end,
        Period=60,
        Statistics=["Sum"],
    )
    return sum(dp["Sum"] for dp in response.get("Datapoints", []))


def evaluate(env: str, version: str, since: float, max_error_rate: float, min_requests: int) -> int:
    import boto3

    cw = boto3.client(
        "cloudwatch",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )
    start = datetime.fromtimestamp(since, UTC) - timedelta(minutes=1)
    end = datetime.now(UTC) + timedelta(minutes=1)

    rows = []
    for svc in SERVICES:
        requests = _sum(cw, env, "HttpRequests", svc, version, start, end)
        errors = _sum(cw, env, "HttpErrors", svc, version, start, end)
        rows.append((svc, int(requests), int(errors)))
    gateway_requests = rows[0][1]
    total_requests = sum(r[1] for r in rows)
    total_errors = sum(r[2] for r in rows)
    error_rate = total_errors / total_requests if total_requests else 1.0

    enough = gateway_requests >= min_requests
    healthy = error_rate <= max_error_rate
    decision = "PROMOTE" if enough and healthy else "ROLLBACK"
    reason = (
        "condiciones cumplidas"
        if decision == "PROMOTE"
        else (
            f"tráfico insuficiente ({gateway_requests} < {min_requests})"
            if not enough
            else f"tasa de error {error_rate:.1%} > {max_error_rate:.0%}"
        )
    )

    lines = [
        f"### Análisis canary · `{env}` · versión `{version}`",
        "",
        "| Servicio | Peticiones | Errores 5xx |",
        "| --- | ---: | ---: |",
        *[f"| {svc} | {req} | {err} |" for svc, req, err in rows],
        f"| **Total** | **{total_requests}** | **{total_errors}** |",
        "",
        f"- Tráfico mínimo (gateway): {gateway_requests} ≥ {min_requests} → "
        f"{'✅' if enough else '❌'}",
        f"- Tasa de error: {error_rate:.2%} ≤ {max_error_rate:.0%} → {'✅' if healthy else '❌'}",
        f"- **Decisión: {decision}** ({reason})",
    ]
    report = "\n".join(lines)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    return 0 if decision == "PROMOTE" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Análisis del despliegue canary")
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("traffic")
    t.add_argument("--url", required=True)
    t.add_argument("--host", required=True)
    t.add_argument("--seconds", type=float, default=60)
    t.add_argument("--rps", type=float, default=5)
    e = sub.add_parser("evaluate")
    e.add_argument("--env", required=True, choices=["dev", "test", "prod"])
    e.add_argument("--version", required=True)
    e.add_argument("--since", type=float, required=True, help="epoch del inicio del canary")
    e.add_argument("--max-error-rate", type=float, default=0.05)
    e.add_argument("--min-requests", type=int, default=20)
    args = parser.parse_args()
    if args.cmd == "traffic":
        return traffic(args.url, args.host, args.seconds, args.rps)
    return evaluate(args.env, args.version, args.since, args.max_error_rate, args.min_requests)


if __name__ == "__main__":
    sys.exit(main())
