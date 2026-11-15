"""Logs estructurados en JSON, una línea por evento, hacia stdout y (opcional) CloudWatch.

Campos fijos de cada línea:
    timestamp, level, env, service, version, correlation_id, saga_id, event, message
más los campos propios del evento (status, duration_ms, step, ...).

Los metric filters de Terraform dependen de este formato:
    { $.event = "http_request" }                       → HttpRequests (por Service, Version)
    { $.event = "http_request" && $.status >= 500 }    → HttpErrors   (por Service, Version)
    { $.event = "saga_compensated" }                   → SagaCompensations
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from typing import Any

from aegis_common.context import correlation_id_var, saga_id_var

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "event", "fields"}


class JsonFormatter(logging.Formatter):
    def __init__(self, env: str, service: str, version: str) -> None:
        super().__init__()
        self.static = {"env": env, "service": service, "version": version}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            **self.static,
            "correlation_id": correlation_id_var.get(),
            "saga_id": saga_id_var.get(),
            "event": getattr(record, "event", "log"),
            "message": record.getMessage(),
        }
        for key, value in (getattr(record, "fields", None) or {}).items():
            # Los campos fijos (service, env, correlation_id...) nunca se sobrescriben: los
            # metric filters y la trazabilidad dependen de ellos.
            payload[f"{key}_field" if key in payload else key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class CloudWatchHandler(logging.Handler):
    """Envía los logs a CloudWatch Logs en lotes, desde un hilo en segundo plano.

    Nunca bloquea la petición: si CloudWatch no responde, los eventos se descartan tras el
    reintento (los logs siguen en stdout, que en Kubernetes recoge el agente del nodo).
    """

    MAX_BATCH = 200
    FLUSH_SECONDS = 2.0

    def __init__(self, client: Any, group: str, stream: str | None = None) -> None:
        super().__init__()
        self.client = client
        self.group = group
        self.stream = stream or f"{socket.gethostname()}-{os.getpid()}"
        self._queue: queue.Queue[tuple[int, str]] = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._ready = False
        self._thread = threading.Thread(target=self._run, name="cloudwatch-logs", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def emit(self, record: logging.LogRecord) -> None:
        # Cola llena: se descarta el evento para proteger la memoria del servicio.
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait((int(record.created * 1000), self.format(record)))

    def _ensure_stream(self) -> None:
        if self._ready:
            return
        for create in (
            lambda: self.client.create_log_group(logGroupName=self.group),
            lambda: self.client.create_log_stream(
                logGroupName=self.group, logStreamName=self.stream
            ),
        ):
            with contextlib.suppress(self.client.exceptions.ResourceAlreadyExistsException):
                create()
        self._ready = True

    def _drain(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while len(events) < self.MAX_BATCH:
            try:
                ts, message = self._queue.get_nowait()
            except queue.Empty:
                break
            events.append({"timestamp": ts, "message": message})
        return events

    def flush(self) -> None:
        events = self._drain()
        if not events:
            return
        events.sort(key=lambda e: e["timestamp"])  # PutLogEvents exige orden cronológico
        for attempt in (1, 2):
            try:
                self._ensure_stream()
                self.client.put_log_events(
                    logGroupName=self.group, logStreamName=self.stream, logEvents=events
                )
                return
            except Exception as exc:  # noqa: BLE001 — el logging nunca debe tumbar el servicio
                self._ready = False
                if attempt == 2:
                    print(f"cloudwatch-logs: lote descartado ({exc})", file=sys.stderr)

    def _run(self) -> None:
        while not self._stop.wait(self.FLUSH_SECONDS):
            self.flush()

    def close(self) -> None:
        if not self._stop.is_set():
            self._stop.set()
            self.flush()
        super().close()


def configure_logging(settings: Any, cloudwatch_client: Any | None = None) -> logging.Logger:
    """Configura el logger raíz del servicio. Idempotente."""
    formatter = JsonFormatter(settings.env.value, settings.service, settings.version)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(formatter)
    root.addHandler(stdout)
    if settings.cloudwatch_logs:
        if cloudwatch_client is None:
            from aegis_common.aws import client

            cloudwatch_client = client("logs", settings)
        cw = CloudWatchHandler(cloudwatch_client, settings.log_group)
        cw.setFormatter(formatter)
        root.addHandler(cw)
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    for noisy in ("botocore", "boto3", "urllib3", "httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(settings.service)


def log_event(
    logger: logging.Logger, event: str, message: str = "", level: int = logging.INFO, **fields: Any
) -> None:
    """Registra un evento de negocio: log_event(log, "saga_step", step="charge", ok=True)."""
    logger.log(level, message or event, extra={"event": event, "fields": fields})


def monotonic_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)
