"""Clientes HTTP hacia los microservicios.

Cada petición lleva X-Correlation-Id y X-Saga-Id (hook de httpx): así los logs de los cuatro
servicios comparten el mismo correlation_id y se pueden rastrear juntos en CloudWatch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from aegis_common.context import outgoing_headers


class ServiceError(Exception):
    """Fallo de un microservicio (respuesta 4xx/5xx o red)."""

    def __init__(self, service: str, status: int, code: str, message: str) -> None:
        super().__init__(f"{service}: {code} ({status}) {message}")
        self.service = service
        self.status = status
        self.code = code
        self.message = message

    @property
    def transient(self) -> bool:
        """Errores en los que reintentar tiene sentido (red caída, 5xx genérico)."""
        return self.status == 0 or (self.status >= 500 and self.code != "CHARGER_FAULT")


async def _propagate(request: httpx.Request) -> None:
    request.headers.update(outgoing_headers())


class ServiceClient:
    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.name = name
        self.http = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            event_hooks={"request": [_propagate]},
        )

    async def call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self.http.request(method, path, json=json, params=params)
        except httpx.TransportError as exc:
            raise ServiceError(self.name, 0, "UNAVAILABLE", str(exc) or type(exc).__name__) from exc
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            detail = body.get("message") or body.get("detail") or response.text[:200]
            raise ServiceError(
                self.name, response.status_code, body.get("error", "HTTP_ERROR"), str(detail)
            )
        return response.json()

    async def healthy(self) -> bool:
        try:
            return (await self.http.get("/healthz")).status_code == 200
        except httpx.TransportError:
            return False

    async def aclose(self) -> None:
        await self.http.aclose()


@dataclass
class ServiceClients:
    reservations: ServiceClient
    billing: ServiceClient
    dispatch: ServiceClient

    @classmethod
    def from_env(cls, transports: dict[str, httpx.AsyncBaseTransport] | None = None):
        transports = transports or {}
        return cls(
            reservations=ServiceClient(
                "station-reservation-service",
                os.environ.get("RESERVATION_URL", "http://localhost:8001"),
                transport=transports.get("reservations"),
            ),
            billing=ServiceClient(
                "billing-wallet-service",
                os.environ.get("BILLING_URL", "http://localhost:8002"),
                transport=transports.get("billing"),
            ),
            dispatch=ServiceClient(
                "energy-dispatch-service",
                os.environ.get("DISPATCH_URL", "http://localhost:8003"),
                transport=transports.get("dispatch"),
            ),
        )

    def all(self) -> list[ServiceClient]:
        return [self.reservations, self.billing, self.dispatch]

    async def aclose(self) -> None:
        for client in self.all():
            await client.aclose()
