import pytest

from aegis_common import Environment, Settings, resource_name
from aegis_common.config import log_group


@pytest.mark.parametrize("env", list(Environment))
def test_resource_name_includes_environment(env):
    assert resource_name("reservations", env) == f"aegisvolt-reservations-{env}"


def test_environments_never_collide():
    names = {resource_name("billing", env) for env in Environment}
    assert len(names) == 3


@pytest.mark.parametrize("value", [None, "", "default", "staging", "PRODUCTION"])
def test_invalid_environment_is_rejected(value):
    with pytest.raises(ValueError):
        Environment.parse(value)


def test_environment_parse_normalizes_case():
    assert Environment.parse(" Prod ") is Environment.PROD


@pytest.mark.parametrize("bad", ["Reservations", "x", "res_ervations", "../etc"])
def test_invalid_resource_name_is_rejected(bad):
    with pytest.raises(ValueError):
        resource_name(bad, Environment.DEV)


def test_log_group_follows_convention():
    assert log_group("billing-wallet-service", Environment.TEST) == (
        "/aegisvolt/test/billing-wallet-service"
    )


def test_settings_from_env():
    settings = Settings.from_env({"AEGIS_ENV": "test", "AWS_ENDPOINT_URL": "http://floci:4566"})
    assert settings.env is Environment.TEST
    assert settings.region == "us-east-1"
    assert settings.table("dispatch") == "aegisvolt-dispatch-test"


def test_settings_without_env_fails_fast():
    with pytest.raises(ValueError, match="AEGIS_ENV"):
        Settings.from_env({})
