"""E2E de la Fase 0: el stack local levanta y expone los servicios AWS del proyecto.

En la Fase 3 este archivo se amplía con los tres escenarios de la SAGA por GraphQL.
"""

import json
import urllib.request

import pytest

pytestmark = pytest.mark.e2e

REQUIRED_SERVICES = {"s3", "dynamodb", "logs", "monitoring", "ec2", "apigateway", "eks"}


def test_floci_exposes_required_services(aws_endpoint):
    with urllib.request.urlopen(f"{aws_endpoint}/_floci/health", timeout=10) as resp:  # noqa: S310
        health = json.load(resp)
    running = {name for name, state in health["services"].items() if state == "running"}
    missing = REQUIRED_SERVICES - running
    assert not missing, f"Servicios no disponibles en Floci: {sorted(missing)}"
