# 01 — Teoría y registro de decisiones (ADR)

Este documento registra cada decisión de arquitectura con su contexto, la opción elegida, las
alternativas descartadas y su evidencia. Se amplía en cada fase. Es la base para responder el
**por qué** en la sustentación.

---

## Resultados de las pruebas de Floci (Fase 0)

Las pruebas se ejecutaron el 2026-11-14 con `./scripts/floci_smoke.sh` sobre Floci 2.4.0:

| Prueba | Resultado | Decisión que confirma |
| --- | --- | --- |
| F0-1 Escritura condicional en S3 (`If-None-Match`) | ✅ La 2.ª escritura se rechaza | **T5:** bloqueo del estado con `use_lockfile = true` |
| F0-2 Versionamiento + `list-object-versions` | ✅ 3 versiones; la más antigua se recupera por `VersionId` | Restauración del tfstate (pregunta 6.1) |
| F0-3 VPC, VPCE Gateway (DynamoDB) e Interface (`execute-api`) | ✅ | Módulos `network` y `vpc_endpoints` reales en Floci |
| F0-4 NLB interno + VPC Link de API Gateway | ✅ | Módulo `api_gateway` real en Floci |
| F0-5 EKS `create-cluster` / `describe-cluster` | ✅ (plano de control) | Módulo `eks` real; los pods corren en minikube |
| F0-6 Metric filter → métrica en CloudWatch | ✅ | **T4:** el canary usa el metric filter con dimensión `version` |
| F0-7 `filter-log-events` con `{ $.correlation_id = "…" }` | ✅ Devuelve exactamente 1 evento | Trazabilidad por `correlation_id` (pregunta 6.4) |
| F0-8 minikube → Floci | ⏳ Pendiente | Se ejecuta al instalar minikube (Fase 4) |

**Conclusión:** no hace falta ningún plan B. Todas las piezas de AWS del proyecto existen en Floci,
así que el mismo Terraform sirve para Floci y para AWS real.

---

## Decisiones de entorno

### E1. Linux nativo y scripts Bash

- **Contexto:** el equipo de desarrollo es CachyOS (Arch).
- **Decisión:** todos los scripts son Bash con `set -euo pipefail` y se validan con `shellcheck`, en
  local y en el job `lint-build`.

### E2. Herramientas

- **Instalación:** kubectl, minikube, Helm, uv y AWS CLI v2 se instalan con `pacman`.
- **Docker:** el `credsStore` de Docker Desktop usa gpg y a veces se bloquea mientras gpg-agent espera
  la contraseña. Si `docker pull` se queda colgado, desbloquea gpg (por ejemplo con
  `echo test | gpg --clearsign`) y reintenta.

### E3. Python 3.12

- **Contexto:** el sistema trae Python 3.14, y parte del ecosistema (Prefect, algunas ruedas
  binarias) aún no lo soporta bien.
- **Decisión:**
  - las imágenes usan `python:3.12-slim`;
  - en el host se usa `uv venv --python 3.12`;
  - `pyproject.toml` fija `>=3.12,<3.13`.
- **Respaldo:** `scripts/dev.sh` ejecuta las pruebas en un contenedor de 3.12 si no existe `.venv`.

---

## Decisiones de gobernanza Git (Fase 0)

### G1. Solo se permite el merge commit

- **Contexto:** la promoción es `dev → test → prod`. Con *squash* o *rebase*, GitHub crea commits
  nuevos en la rama destino que no existen en la origen. Entonces las ramas divergen y la siguiente
  promoción trae conflictos.
- **Decisión:** el ruleset y la configuración del repo solo permiten *merge commit*. `test` siempre
  contiene la historia exacta de `dev`, y `prod` la de `test`.

### G2. Sin "rama al día con la base" (`strict_required_status_checks_policy: false`)

- **Contexto:** cada merge a `test` crea un merge commit que `dev` no tiene. Con la política
  estricta, el siguiente PR `dev → test` exigiría primero traer `test` a `dev`. Ese flujo inverso
  está prohibido por `promote-guard`.
- **Decisión:** desactivar la política estricta. Los checks igual corren sobre el resultado del merge
  (`refs/pull/N/merge`), así que no se pierde la validación.

### G3. Cero aprobaciones requeridas

- **Contexto:** el proyecto es individual, y GitHub no permite aprobar un PR propio.
- **Decisión:** `required_approving_review_count: 0`. El control de calidad lo dan los 6 checks
  obligatorios y el PR sigue siendo obligatorio.
- **En equipo:** se subiría a 1 o más, con CODEOWNERS.

### G4. Sin actores con bypass

- **Decisión:** `bypass_actors: []`. Ni siquiera el administrador puede hacer push directo o force
  push. Eso es lo que se demuestra en vivo en la pregunta 6.2.

### G5. Checks atados a GitHub Actions

- **Decisión:** cada check requerido lleva `integration_id: 15368` (la app de GitHub Actions). Así,
  nadie puede "aprobar" un check publicando un estado falso con el mismo nombre desde otra
  integración.

### G6. `promote-guard` resistente a inyección

- **Contexto:** el nombre de la rama lo controla quien abre el PR.
- **Decisión:** el workflow recibe `head_ref` y `base_ref` por variables de entorno, nunca
  interpolados con `${{ }}` dentro del script.

### G7. Acciones fijadas por SHA

- **Decisión:** todas las `uses:` apuntan al SHA del commit, con la versión en un comentario. Un tag
  puede moverse a otro commit; un SHA no.

---

## Decisiones técnicas (T1–T7)

Están descritas en [FASES-DESARROLLO.md](../FASES-DESARROLLO.md#decisiones-técnicas-alineadas-con-la-rúbrica).
Estado actual:

| # | Decisión | Estado |
| --- | --- | --- |
| T1 | `terraform plan` en runner self-hosted | Fase 0: `fmt` en un runner de GitHub. Pasa a self-hosted en la Fase 1 |
| T2 | Repo público | `scripts/setup_rulesets.sh` lo verifica antes de aplicar las reglas |
| T3 | Endurecimiento frente a forks | `setup_rulesets.sh` exige aprobación para PR externos y token de solo lectura |
| T4 | Canary con Ingress y metric filter | Confirmado por F0-6 |
| T5 | `use_lockfile` | Confirmado por F0-1 |
| T6 | Nombres de los checks idénticos | `pr-checks.yml` / `promote-guard.yml` ↔ `protected-branches.json` |
| T7 | Suscripciones GraphQL sobre WSS | Fase 2 |
