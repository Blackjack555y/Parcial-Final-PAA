"""Configuración por ambiente.

Regla central del aislamiento: todo recurso lleva el ambiente en su nombre
(aegisvolt-<recurso>-<env>), igual que Terraform usa ${terraform.workspace}.
Un servicio que arranca sin ambiente válido falla de inmediato en vez de
escribir, por error, en las tablas de otro ambiente.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum

PROJECT = "aegisvolt"
_RESOURCE_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"

    @classmethod
    def parse(cls, value: str | None) -> Environment:
        if not value:
            raise ValueError("AEGIS_ENV no está definido (valores válidos: dev, test, prod)")
        try:
            return cls(value.strip().lower())
        except ValueError:
            raise ValueError(
                f"Ambiente inválido {value!r} (valores válidos: dev, test, prod)"
            ) from None


def resource_name(resource: str, env: Environment) -> str:
    """Nombre físico: resource_name("reservations", DEV) -> "aegisvolt-reservations-dev"."""
    if not _RESOURCE_RE.match(resource):
        raise ValueError(f"Nombre de recurso inválido: {resource!r}")
    return f"{PROJECT}-{resource}-{env.value}"


def log_group(service: str, env: Environment) -> str:
    """Log group de CloudWatch: /aegisvolt/<env>/<servicio>."""
    if not _RESOURCE_RE.match(service):
        raise ValueError(f"Nombre de servicio inválido: {service!r}")
    return f"/{PROJECT}/{env.value}/{service}"


@dataclass(frozen=True, slots=True)
class Settings:
    env: Environment
    region: str
    aws_endpoint_url: str | None

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        source = os.environ if environ is None else environ
        return cls(
            env=Environment.parse(source.get("AEGIS_ENV")),
            region=source.get("AWS_DEFAULT_REGION", "us-east-1"),
            aws_endpoint_url=source.get("AWS_ENDPOINT_URL") or None,
        )

    def table(self, resource: str) -> str:
        return resource_name(resource, self.env)
