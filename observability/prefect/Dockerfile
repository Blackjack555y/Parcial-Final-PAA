# syntax=docker/dockerfile:1
# Flujos de observabilidad (Prefect). Contexto de build: raíz del repo.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS deps
WORKDIR /build
COPY observability/prefect/requirements.txt ./requirements.txt
COPY libs/aegis_common ./libs/aegis_common
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt ./libs/aegis_common

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PREFECT_HOME=/tmp/prefect
COPY --from=deps /install /usr/local
WORKDIR /app
COPY --chown=root:root observability/prefect/aegis_flows ./aegis_flows
COPY --chown=root:root observability/prefect/serve.py ./serve.py
USER 10001:10001
CMD ["python", "serve.py"]
