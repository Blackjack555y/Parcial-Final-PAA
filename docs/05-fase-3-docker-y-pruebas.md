# 05 — Fase 3: contenedores Docker y pruebas en CI

## Imágenes

Hay un Dockerfile por microservicio (`services/<svc>/Dockerfile`). El contexto de build es la raíz
del repo, porque cada imagen incluye `libs/aegis_common`.

| Práctica | Cómo se aplica |
| --- | --- |
| Base fijada por digest | `python:3.12-slim@sha256:5e2b…`: el build no cambia si alguien re-publica el tag |
| Multi-etapa | `pip` y sus cachés quedan en la etapa `deps`; la final solo copia `/install` |
| Parches del SO | `apt-get upgrade` en la etapa final (Trivy encontró 2 CVE HIGH de `zlib1g` con parche) |
| Sin root | Usuario `app` con UID/GID **10001**, shell `nologin`; el código pertenece a root (no modificable) |
| Solo lectura | `PYTHONDONTWRITEBYTECODE=1`; en compose `read_only: true` + `tmpfs /tmp` |
| Mínimos privilegios | Compose: `cap_drop: [ALL]`, `no-new-privileges` |
| Health check | `HEALTHCHECK` contra `/healthz` (compose espera a que todos estén `healthy`) |
| Trazabilidad de versión | `--build-arg VERSION=<sha>` → `AEGIS_VERSION` → campo `version` de cada log (el canary distingue versiones por él) |
| Contexto mínimo | `.dockerignore` excluye todo salvo lo necesario (`**` + excepciones) |

**Comandos:**

```bash
./scripts/dev.sh up-apps dev     # build + 4 contenedores contra las tablas de dev (gateway :8000)
./scripts/dev.sh down-apps       # los detiene (Floci sigue)
```

Solo el gateway publica un puerto (`127.0.0.1:8000`). Los microservicios son internos a la red
`aegisvolt`, igual que los Services `ClusterIP` en Kubernetes.

---

## Pirámide de pruebas

| Suite | Cantidad | Qué cubre | Dónde corre |
| --- | --- | --- | --- |
| `tests/unit` | 54 | Lógica de la SAGA (orden inverso, reintentos, idempotencia), tarifas, telemetría, logs JSON, protecciones GraphQL, guardia de Terraform | Runner de GitHub |
| `tests/integration` | 10 | SAGA completa por GraphQL con los 4 servicios en proceso contra **DynamoDB real** (Floci): tablas, doble reembolso, doble reserva; S3 versionado; logs | Runner de GitHub + Floci efímero |
| `tests/e2e` | 8 | Stack en **contenedores** por su API pública: 3 escenarios de la SAGA, telemetría, WSS en vivo, `correlation_id` en CloudWatch | Runner de GitHub + ambiente efímero completo |

### Cómo arma su ambiente `e2e-tests`

1. Levanta Floci vacío.
2. Corre **el mismo Terraform** del proyecto (workspace `dev`, backend local desechable): VPC,
   VPCE, tablas, log groups y metric filters, igual que en los ambientes reales.
3. Carga los datos de demostración (`scripts/seed.py dev`).
4. Construye y levanta los 4 contenedores (`--wait` hasta que estén `healthy`).
5. Ejecuta `pytest tests/e2e` contra `http://127.0.0.1:8000` (HTTP y WSS).

Así, cada PR valida que **infraestructura + imágenes + código** funcionan juntos antes del merge.

### `lint-build`

Ejecuta, en orden:

1. ruff;
2. shellcheck;
3. actionlint;
4. esquema GraphQL al día;
5. `docker compose config`;
6. **build de las 4 imágenes**;
7. **Trivy**: falla si alguna imagen tiene vulnerabilidades HIGH/CRITICAL con parche disponible.

---

## Pregunta 6.2: los checks bloquean el merge

**Evidencia generada en esta fase:** al PR de la Fase 3 se le subió **a propósito** un commit con
una prueba rota.

- El check `unit-tests` quedó en rojo y GitHub rechazó el merge (`gh pr merge` falló por el
  ruleset).
- Después se revirtió el commit, los 6 checks volvieron a verde y recién entonces se pudo fusionar.

Los detalles están en el historial del PR.
