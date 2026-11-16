# syntax=docker/dockerfile:1
# Imagen de graphql-gateway. Contexto de build: la RAÍZ del repo (necesita libs/aegis_common).
#   docker build -f services/graphql-gateway/Dockerfile --build-arg VERSION=$(git rev-parse --short HEAD) .

# --- Etapa 1: dependencias (pip y sus cachés no llegan a la imagen final) -------------
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS deps
WORKDIR /build
COPY services/graphql-gateway/requirements.txt ./requirements.txt
COPY libs/aegis_common ./libs/aegis_common
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt "./libs/aegis_common[web]"

# --- Etapa 2: runtime mínimo ---------------------------------------------------------
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ARG VERSION=dev-local
LABEL org.opencontainers.image.title="aegisvolt-graphql-gateway" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/Blackjack555y/Parcial-Final-PAA"

# Sin .pyc (el sistema de archivos es de solo lectura) y logs sin búfer (JSON a stdout).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AEGIS_SERVICE=graphql-gateway \
    AEGIS_VERSION=${VERSION}

# Parches de seguridad del sistema base publicados después de la imagen (Trivy los exige en CI).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Usuario sin privilegios con UID fijo (Kubernetes: runAsNonRoot + runAsUser 10001).
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=deps /install /usr/local
WORKDIR /app
COPY --chown=root:root services/graphql-gateway/graphql_gateway ./graphql_gateway

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
CMD ["uvicorn", "--factory", "graphql_gateway.main:create_app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]
