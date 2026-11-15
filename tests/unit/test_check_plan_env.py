import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "check_plan_env", Path(__file__).parents[2] / "scripts" / "check_plan_env.py"
)
check_plan_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_plan_env)


def _plan(*changes):
    return {
        "resource_changes": [
            {"address": addr, "change": {"actions": actions, "before": None, "after": after}}
            for addr, actions, after in changes
        ]
    }


def test_same_environment_is_allowed():
    plan = _plan(
        ("module.dynamodb.aws_dynamodb_table.this", ["create"], {"name": "aegisvolt-billing-test"}),
        (
            "module.observability.aws_cloudwatch_log_group.x",
            ["create"],
            {"name": "/aegisvolt/test/gw"},
        ),
        ("module.network.aws_vpc.this", ["update"], {"tags": {"Name": "aegisvolt-test-vpc"}}),
    )
    assert check_plan_env.find_violations(plan, "test") == []


@pytest.mark.parametrize(
    "value",
    [
        "aegisvolt-billing-prod",
        "aegisvolt-prod-vpc",
        "/aegisvolt/prod/billing-wallet-service",
        "env:/prod/terraform.tfstate",
        "arn:aws:dynamodb:us-east-1:000:table/aegisvolt-reservations-prod",
    ],
)
def test_other_environment_is_rejected(value):
    plan = _plan(("aws_x.y", ["delete"], {"name": value}))
    violations = check_plan_env.find_violations(plan, "test")
    assert len(violations) == 1
    assert "prod" in violations[0]


def test_no_op_changes_are_ignored():
    plan = _plan(("aws_x.y", ["no-op"], {"name": "aegisvolt-billing-prod"}))
    assert check_plan_env.find_violations(plan, "dev") == []


def test_unrelated_names_are_ignored():
    plan = _plan(("aws_x.y", ["create"], {"description": "product dev tools", "x": "testing"}))
    assert check_plan_env.find_violations(plan, "prod") == []


def test_invalid_environment_raises():
    with pytest.raises(ValueError):
        check_plan_env.find_violations({}, "default")
