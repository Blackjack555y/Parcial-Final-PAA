"""Fixtures compartidas."""

import os
import uuid

import pytest


@pytest.fixture(scope="session")
def aws_endpoint() -> str:
    # Sin endpoint, las pruebas de integración/e2e FALLAN (no se saltan):
    # un check en verde sin haber probado nada sería un falso positivo.
    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    if not endpoint:
        pytest.fail("AWS_ENDPOINT_URL no está definido; levanta Floci con ./scripts/dev.sh up")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    return endpoint


@pytest.fixture
def unique_suffix() -> str:
    return uuid.uuid4().hex[:8]
