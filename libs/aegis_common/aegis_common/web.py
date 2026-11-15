"""Piezas comunes de FastAPI: correlación, log de cada petición, errores y health checks."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from aegis_common.context import (
    CORRELATION_HEADER,
    SAGA_HEADER,
    correlation_id_var,
    new_correlation_id,
    saga_id_var,
)
from aegis_common.errors import DomainError
from aegis_common.logging import log_event, monotonic_ms

_SKIP_LOG = {"/healthz", "/readyz"}  # los health checks no cuentan como tráfico (canary)


def install(app: FastAPI, logger: logging.Logger, ready_check: Callable[[], bool] | None = None):
    @app.middleware("http")
    async def correlation_and_access_log(request: Request, call_next):
        cid = request.headers.get(CORRELATION_HEADER) or new_correlation_id()
        c_token = correlation_id_var.set(cid)
        s_token = saga_id_var.set(request.headers.get(SAGA_HEADER))
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers[CORRELATION_HEADER] = cid
            return response
        finally:
            if request.url.path not in _SKIP_LOG:
                route = request.scope.get("route")
                log_event(
                    logger,
                    "http_request",
                    f"{request.method} {request.url.path} → {status}",
                    level=logging.ERROR if status >= 500 else logging.INFO,
                    method=request.method,
                    path=getattr(route, "path", request.url.path),
                    status=status,
                    duration_ms=monotonic_ms(start),
                )
            correlation_id_var.reset(c_token)
            saga_id_var.reset(s_token)

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"error": exc.code, "message": exc.message}
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        ok = ready_check() if ready_check else True
        return JSONResponse(status_code=200 if ok else 503, content={"ready": ok})
