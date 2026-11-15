"""Condición de promoción del canary (scripts/canary.py evaluate) con CloudWatch simulado."""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "canary", Path(__file__).parents[2] / "scripts" / "canary.py"
)
canary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(canary)


class FakeCloudWatch:
    def __init__(self, data: dict[tuple[str, str], float]) -> None:
        self.data = data  # (métrica, servicio) → suma

    def get_metric_statistics(self, **kwargs):
        dims = {d["Name"]: d["Value"] for d in kwargs["Dimensions"]}
        assert kwargs["Namespace"] == "AegisVolt/dev"
        assert dims["Version"] == "abc123"  # solo se mide la versión canary
        value = self.data.get((kwargs["MetricName"], dims["Service"]), 0)
        return {"Datapoints": [{"Sum": value}] if value else []}


@pytest.fixture
def run(monkeypatch):
    def _run(data, **kw):
        import boto3

        monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeCloudWatch(data))
        params = dict(max_error_rate=0.05, min_requests=20)
        params.update(kw)
        return canary.evaluate("dev", "abc123", time.time(), **params)

    return _run


def test_promotes_with_enough_traffic_and_low_errors(run, capsys):
    data = {("HttpRequests", "graphql-gateway"): 100, ("HttpErrors", "graphql-gateway"): 2}
    assert run(data) == 0
    assert "PROMOTE" in capsys.readouterr().out


def test_rolls_back_when_error_rate_exceeds_threshold(run, capsys):
    data = {("HttpRequests", "graphql-gateway"): 40, ("HttpErrors", "graphql-gateway"): 20}
    assert run(data) == 1
    assert "tasa de error" in capsys.readouterr().out


def test_errors_in_any_service_of_the_version_count(run):
    data = {
        ("HttpRequests", "graphql-gateway"): 50,
        ("HttpRequests", "billing-wallet-service"): 50,
        ("HttpErrors", "billing-wallet-service"): 10,  # 10 % del total
    }
    assert run(data) == 1


def test_rolls_back_without_enough_traffic(run, capsys):
    assert run({("HttpRequests", "graphql-gateway"): 5}) == 1
    assert "tráfico insuficiente" in capsys.readouterr().out


def test_no_metrics_at_all_is_a_rollback(run):
    assert run({}) == 1
