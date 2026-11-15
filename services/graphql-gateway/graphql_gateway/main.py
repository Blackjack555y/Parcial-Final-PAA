"""graphql-gateway: capa de agregación GraphQL + orquestador de la SAGA.

Endpoints:
  POST /graphql         queries y mutations (HTTPS a través de API Gateway)
  WS   /graphql         subscriptions (graphql-transport-ws)
  GET  /healthz /readyz health checks (el NLB y Kubernetes los usan)

Protecciones del esquema:
  - Profundidad máxima, máximo de alias y de tokens por consulta (consultas abusivas).
  - Introspección desactivada en prod.
  - MaskErrors: los errores internos llegan al cliente como "Unexpected error."; solo los
    UserError (validación y reglas de negocio) muestran su mensaje.
  - Mutaciones administrativas protegidas con X-Admin-Token (comparación en tiempo constante).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

import strawberry
from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from graphql import GraphQLError
from strawberry.extensions import (
    DisableIntrospection,
    MaskErrors,
    MaxAliasesLimiter,
    MaxTokensLimiter,
    QueryDepthLimiter,
)
from strawberry.fastapi import GraphQLRouter
from strawberry.subscriptions import GRAPHQL_TRANSPORT_WS_PROTOCOL

from aegis_common import Environment, Settings
from aegis_common.aws import client
from aegis_common.flags import FlagStore
from aegis_common.logging import configure_logging
from aegis_common.web import install
from graphql_gateway.clients import ServiceClients
from graphql_gateway.saga.orchestrator import SagaOrchestrator
from graphql_gateway.saga.repository import SagaRepository
from graphql_gateway.schema import Mutation, Query, Subscription, UserError

SERVICE = "graphql-gateway"


def _is_user_error(error: GraphQLError) -> bool:
    return isinstance(error.original_error, UserError) or error.original_error is None


def build_schema(env: Environment) -> strawberry.Schema:
    # Fábricas (no instancias): Strawberry crea una extensión nueva por petición.
    extensions: list[Any] = [
        lambda: QueryDepthLimiter(max_depth=8),
        lambda: MaxAliasesLimiter(max_alias_count=15),
        lambda: MaxTokensLimiter(max_token_count=2000),
        lambda: MaskErrors(should_mask_error=lambda e: not _is_user_error(e)),
    ]
    if env is Environment.PROD:
        extensions.append(DisableIntrospection)
    return strawberry.Schema(
        query=Query, mutation=Mutation, subscription=Subscription, extensions=extensions
    )


def create_app(
    settings: Settings | None = None,
    clients: ServiceClients | None = None,
    flags: FlagStore | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env(service=SERVICE)
    log = configure_logging(settings)
    clients = clients or ServiceClients.from_env()
    flags = flags or FlagStore(settings)
    repo = SagaRepository(client("dynamodb", settings), settings.table("sagas"))
    orchestrator = SagaOrchestrator(repo, clients, log)

    async def context_getter(request: Request = None, websocket: WebSocket = None):  # type: ignore[assignment]
        return {
            "request": request or websocket,
            "settings": settings,
            "clients": clients,
            "flags": flags,
            "orchestrator": orchestrator,
            "log": log,
        }

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await clients.aclose()

    app = FastAPI(title=SERVICE, version=settings.version, lifespan=lifespan)
    origins = [o for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",") if o]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type", "x-admin-token", "x-correlation-id"],
        expose_headers=["x-correlation-id"],
    )

    async def ready() -> bool:
        return all([await c.healthy() for c in clients.all()])

    install(app, log)

    @app.get("/readyz/deep", include_in_schema=False)
    async def deep_ready() -> dict[str, Any]:
        return {
            "ready": await ready(),
            "services": {c.name: await c.healthy() for c in clients.all()},
        }

    router = GraphQLRouter(
        build_schema(settings.env),
        context_getter=context_getter,
        graphql_ide=None if settings.env is Environment.PROD else "graphiql",
        subscription_protocols=[GRAPHQL_TRANSPORT_WS_PROTOCOL],
    )
    app.include_router(router, prefix="/graphql")
    app.state.orchestrator = orchestrator
    app.state.clients = clients
    return app
