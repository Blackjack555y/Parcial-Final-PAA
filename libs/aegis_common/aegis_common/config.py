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
_TRUE = {"1", "true", "yes", "on"}


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


def resource_name(resource: str, env: Environment, prefix: str = PROJECT) -> str:
    """Nombre físico: resource_name("reservations", DEV) -> "aegisvolt-reservations-dev"."""
    if not _RESOURCE_RE.match(resource):
        raise ValueError(f"Nombre de recurso inválido: {resource!r}")
    if not _RESOURCE_RE.match(prefix):
        raise ValueError(f"Prefijo inválido: {prefix!r}")
    return f"{prefix}-{resource}-{env.value}"


def log_group(service: str, env: Environment) -> str:
    """Log group de CloudWatch: /aegisvolt/<env>/<servicio>."""
    if not _RESOURCE_RE.match(service):
        raise ValueError(f"Nombre de servicio inválido: {service!r}")
    return f"/{PROJECT}/{env.value}/{service}"


@dataclass(frozen=True, slots=True)
class Settings:
    env: Environment
    region: str = "us-east-1"
    aws_endpoint_url: str | None = None
    service: str = "unknown"
    version: str = "dev-local"
    # Solo las pruebas cambian el prefijo, para crear tablas aisladas y desechables.
    table_prefix: str = PROJECT
    cloudwatch_logs: bool = False

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None, **overrides: object) -> Settings:
        source = os.environ if environ is None else environ
        values: dict[str, object] = {
            "env": Environment.parse(source.get("AEGIS_ENV")),
            "region": source.get("AWS_DEFAULT_REGION", "us-east-1"),
            "aws_endpoint_url": source.get("AWS_ENDPOINT_URL") or None,
            "service": source.get("AEGIS_SERVICE", "unknown"),
            "version": source.get("AEGIS_VERSION", "dev-local"),
            "table_prefix": source.get("AEGIS_TABLE_PREFIX", PROJECT),
            "cloudwatch_logs": source.get("AEGIS_CLOUDWATCH_LOGS", "false").lower() in _TRUE,
        }
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]

    def table(self, resource: str) -> str:
        return resource_name(resource, self.env, self.table_prefix)

    @property
    def log_group(self) -> str:
        return log_group(self.service, self.env)
