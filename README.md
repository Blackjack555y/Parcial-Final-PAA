# AegisVolt

Plataforma cloud-native para gestionar la carga de flotas eléctricas comerciales en electrolineras B2B.
Permite reservar bahías de carga, retener saldo de la billetera de la flota, activar la transferencia de
energía y auditar la telemetría, con aislamiento estricto entre los ambientes `dev`, `test` y `prod`.

Proyecto final individual de **Patrones Arquitectónicos Avanzados**.

> El plan completo de construcción, con el orden de las fases y las decisiones técnicas, está en
> [FASES-DESARROLLO.md](FASES-DESARROLLO.md).

## Estado

| Fase | Contenido | Estado |
| --- | --- | --- |
| 0 | Entorno, pruebas de Floci y gobernanza Git | ✅ Completada |
| 1 | Terraform: S3 versionado, workspaces, VPC/VPCE, DynamoDB, EKS, API Gateway | ✅ Completada |
| 2 | Microservicios, GraphQL, SAGA y feature flags | ✅ Completada |
| 3 | Docker y pruebas reales en CI | ✅ Completada |
| 4 | Kubernetes, canary y deploy por rama | ✅ Completada |
| 5 | Prefect | ✅ Completada |
| 6 | Frontend | ✅ Completada |
| 7 | Release final, QA y ensayo | ✅ Completada |

## Arquitectura

```text
                 [ Frontend React ]
                   │ HTTPS     │ WSS (suscripciones)
                   ▼           │
           [ API Gateway ]     │
                   │ VPC Link + NLB interno (subred privada)
                   ▼           ▼
   ┌───────────── Kubernetes (namespace por ambiente) ─────────────┐
   │                  graphql-gateway                              │
   │          (esquema GraphQL + orquestador SAGA)                 │
   │        ┌──────────────┬───────────────┬──────────────┐        │
   │        ▼              ▼               ▼              │        │
   │  station-reservation  billing-wallet  energy-dispatch │       │
   └────────┼──────────────┼───────────────┼──────────────────────┘
            │   VPC Endpoint Gateway (DynamoDB)            │
            ▼              ▼               ▼
      reservations-<env>  billing-<env>   dispatch-<env>     + sagas-<env>, flags-<env>
```

- **Database-per-Service:** cada microservicio es dueño de su tabla DynamoDB. El orquestador es dueño
  de la bitácora `sagas` y de la tabla `flags`.
- **Logs:** todos los servicios escriben logs JSON con `correlation_id`, `saga_id`, `env`, `service` y
  `version` en `/aegisvolt/<env>/<servicio>` (CloudWatch).

### SAGA "Reservar y activar carga"

La SAGA es por orquestación: la dirige la capa GraphQL.

| Paso | Servicio | Acción | Compensación |
| --- | --- | --- | --- |
| 1 | `station-reservation-service` | Conector → `RESERVED` (bloqueo **temporal**: vence solo) | Conector → `AVAILABLE` |
| 2 | `billing-wallet-service` | **Retiene** el saldo estimado (`HELD`) | Libera la retención (`RELEASED`) |
| 3 | `energy-dispatch-service` | Inicia el flujo eléctrico | — (último paso) |
| Cierre | dispatch → billing → reservations | Detiene, **liquida la energía real** (`CAPTURED`) y libera la bahía | — |

**Reserva anticipada (SAGA de larga duración):** con `arrivalMinutes > 0` se ejecutan los pasos 1
y 2 y la saga espera la llegada del vehículo (`AWAITING_ARRIVAL`). Luego hay tres salidas:
`confirmArrival` ejecuta el paso 3; `cancelReservation` compensa; y si vence el plazo, el
supervisor compensa por **timeout**. Además, el supervisor cierra y liquida solas las cargas que
alcanzan la energía pedida.

**Qué se compensa según el paso que falla:**

- **Falla el paso 3** (avería del cargador o `SIMULATE_CHARGER_HARDWARE_FAULT`): se libera la
  retención (paso 2) y luego se libera el conector (paso 1).
- **Falla el paso 2** (fondos insuficientes): se libera el conector (paso 1).

**Idempotencia:** cada paso usa `saga_id` como llave junto con escrituras condicionales en DynamoDB.
Si una compensación llega dos veces, la segunda no tiene efecto.

## Stack

| Capa | Tecnología |
| --- | --- |
| Nube local | [Floci](https://github.com/floci-io/floci) 2.4 (emulador de AWS); el Terraform es portable a AWS real |
| IaC | Terraform ≥ 1.10, backend S3 versionado, workspaces `dev` / `test` / `prod` |
| Backend | Python 3.12, FastAPI, Strawberry GraphQL, boto3 |
| Datos | Amazon DynamoDB |
| Contenedores | Docker, Kubernetes (minikube localmente, EKS declarado en Terraform), Ingress canary |
| CI/CD | GitHub Actions (runners de GitHub + runner self-hosted) |
| Observabilidad | Amazon CloudWatch Logs y Metrics, Prefect 3 |
| Frontend | React 19, Vite, TypeScript, `graphql-ws`, nginx sin root. Loader: [Juanes200122 en Uiverse.io](https://uiverse.io/Juanes200122) (MIT) |

## Ambientes

Cada ambiente está aislado en todas las capas:

| | `dev` | `test` | `prod` |
| --- | --- | --- | --- |
| Rama Git | `dev` | `test` | `prod` |
| Workspace Terraform | `dev` | `test` | `prod` |
| Estado en S3 | `env:/dev/terraform.tfstate` | `env:/test/terraform.tfstate` | `env:/prod/terraform.tfstate` |
| Tablas DynamoDB | `aegisvolt-*-dev` | `aegisvolt-*-test` | `aegisvolt-*-prod` |
| Namespace Kubernetes | `aegisvolt-dev` | `aegisvolt-test` | `aegisvolt-prod` |
| Log groups | `/aegisvolt/dev/*` | `/aegisvolt/test/*` | `/aegisvolt/prod/*` |

### Feature flags

Se guardan en la tabla `aegisvolt-flags-<env>` y se cambian sin redesplegar (caché de 5 s).

| Flag | `dev` | `test` | `prod` | Efecto |
| --- | --- | --- | --- | --- |
| `ENABLE_DYNAMIC_SURGE_PRICING` | ✅ | ✅ | ❌ | Tarifa dinámica en horas pico |
| `ENABLE_FAST_CHARGE_BOOST` | ✅ | ✅ | ❌ | Reserva prioritaria de conectores de ultra-carga |
| `SIMULATE_CHARGER_HARDWARE_FAULT` | ❌ | ❌ | ❌ | Fuerza la falla del paso 3 para demostrar la compensación |

## Flujo de ramas

```text
feature/* ──PR──► dev ──PR──► test ──PR──► prod
                   │           │            │
             deploy dev   deploy test  deploy prod
```

**Reglas del repositorio:**

- `dev`, `test` y `prod` están protegidas: sin force push, sin borrado y sin push directo.
- Todo cambio entra por PR.
- **Checks obligatorios para habilitar el merge:**
  - `lint-build`
  - `unit-tests`
  - `integration-tests`
  - `e2e-tests`
  - `terraform-plan`
  - `promote-guard`
- `promote-guard` solo acepta `feature/* → dev`, `dev → test` y `test → prod`.
- **Al fusionar, el pipeline:**
  1. detecta la rama destino;
  2. selecciona el workspace con el mismo nombre;
  3. ejecuta `terraform apply`;
  4. despliega con canary (20 % del tráfico → evaluación → 100 % o rollback).

## Estructura del repositorio

Este es el árbol objetivo; se completa fase por fase.

```text
.
├── .github/
│   ├── workflows/            # pr-checks.yml, promote-guard.yml, deploy.yml
│   ├── rulesets/             # protected-branches.json
│   ├── pull_request_template.md
│   └── CODEOWNERS
├── infra/
│   ├── bootstrap/            # bucket S3 versionado del estado + tabla de lock
│   ├── envs/                 # raíz con backend S3 y workspaces; floci.tfvars, aws.tfvars
│   └── modules/              # network, vpc_endpoints, dynamodb, observability, eks, api_gateway, k8s_namespace
├── libs/aegis_common/        # configuración, logging JSON, correlación, cliente DynamoDB
├── services/
│   ├── graphql-gateway/      # esquema GraphQL, orquestador SAGA, feature flags
│   ├── station-reservation-service/
│   ├── billing-wallet-service/
│   └── energy-dispatch-service/
├── frontend/                 # React + Vite
├── observability/prefect/    # flujos de salud y auditoría de sagas
├── k8s/
│   ├── base/                 # Deployments, Services e Ingress stable/canary, NetworkPolicy
│   └── overlays/             # dev, test, prod
├── scripts/                  # dev.sh, floci_smoke.sh, setup_rulesets.sh, deploy.sh, canary.py, seed.py
├── tests/                    # unit, integration, e2e
├── docs/                     # teoría, decisiones (ADR) y guías por fase
├── compose.yaml              # Floci
├── compose.apps.yaml         # stack completo local
├── pyproject.toml
└── FASES-DESARROLLO.md
```

## Prerrequisitos

Pensados para Linux (Arch / CachyOS):

```bash
sudo pacman -S --needed docker kubectl minikube helm terraform shellcheck uv github-cli
sudo pacman -S aws-cli-v2
```

| Herramienta | Versión mínima |
| --- | --- |
| Docker | 24 |
| Terraform | 1.10 (bloqueo nativo con `use_lockfile`) |
| Python | 3.12 (vía `uv`; no usar 3.14) |
| Node.js | 20 |
| kubectl, minikube, Helm | Actuales |
| AWS CLI | v2 |
| GitHub CLI (`gh`) | Actual |

**Si `docker pull` falla con errores de `gpg` o `pass`:** quita la línea `"credsStore": "pass"` de
`~/.docker/config.json`.

## Puesta en marcha

```bash
cp .env.example .env

# Python 3.12 para pruebas, lint y Prefect
uv venv --python 3.12
source .venv/bin/activate          # fish: source .venv/bin/activate.fish
uv pip install -r requirements-dev.txt
pre-commit install

# Emulador de AWS (Floci persistente en localhost:4566)
./scripts/dev.sh up
./scripts/dev.sh smoke             # pruebas F0-1 a F0-8

# Infraestructura (Terraform sobre Floci)
./scripts/tf.sh bootstrap apply    # bucket S3 versionado del estado (una sola vez)
./scripts/tf.sh dev init           # backend S3 + workspaces dev/test/prod
./scripts/tf.sh dev plan
./scripts/tf.sh dev apply
./scripts/tf.sh dev versions       # historial del tfstate de dev en S3

# Servicios (procesos locales contra las tablas de un ambiente)
python scripts/seed.py dev         # datos de demostración
./scripts/run_local.sh start dev   # gateway :8000 (GraphiQL en /graphql) + 3 servicios
./scripts/run_local.sh stop

# Stack en contenedores (alternativa a run_local.sh)
./scripts/dev.sh up-apps dev       # build + 4 contenedores sin root, solo lectura
./scripts/dev.sh down-apps

# Kubernetes (minikube, un namespace por ambiente) con despliegue canary
minikube start --driver=docker --cpus=4 --memory=5g --cni=calico --addons=ingress --addons=metrics-server
TAG=$(./scripts/deploy.sh image-tag)
./scripts/deploy.sh build "$TAG"
./scripts/deploy.sh dev "$TAG"     # canary 20 % → análisis en CloudWatch → 100 % o rollback
./scripts/deploy.sh status dev

# Observabilidad (Prefect UI en http://127.0.0.1:4200)
./scripts/dev.sh up-observability  # flujos de salud (5 min) y auditoría de la SAGA (10 min)

# Consola web de cada ambiente (port-forward)
./scripts/ui.sh start              # dev :3001 · test :3002 · prod :3003
./scripts/ui.sh token dev          # X-Admin-Token para cambiar flags desde la consola
cd frontend && npm install && npm run dev   # desarrollo (Vite :5173 → gateway :8000)

# Día de la demo
./scripts/demo.sh up               # levanta todo y muestra el semáforo
./scripts/demo.sh reset-flags      # flags a su valor inicial
./scripts/demo.sh check            # semáforo de cada punto a demostrar
./scripts/rubrica.sh               # revisión EN VIVO de los 50 criterios de la rúbrica (5.2)
./scripts/api.sh dev '{ environment { name version } }' -v   # entra por API GATEWAY

# Calidad
./scripts/dev.sh lint
./scripts/dev.sh test all          # unit + integration + e2e
```

| Comando | Qué hace |
| --- | --- |
| `./scripts/dev.sh up` / `down` | Levanta o detiene Floci (los datos se conservan) |
| `./scripts/dev.sh reset` | Detiene Floci y **borra** su estado, incluido el tfstate (pide confirmación) |
| `./scripts/dev.sh status` | Estado de los contenedores y de Floci |
| `./scripts/dev.sh test unit\|integration\|e2e\|all` | Pruebas; sin `.venv`, corren en un contenedor de Python 3.12 |
| `./scripts/tf.sh <env> plan\|apply\|output\|versions\|restore` | Terraform con el workspace del ambiente seleccionado explícitamente |
| `./scripts/deploy.sh <env> <tag> [--chaos 0.5]` | Despliegue canary en Kubernetes (`build`, `image-tag`, `status`) |
| `./scripts/api.sh <env> '<query>' [--admin] [-v]` | GraphQL de un ambiente **entrando por API Gateway** |
| `./scripts/ui.sh start\|stop\|token <env>` | Consolas web (dev :3001 · test :3002 · prod :3003) |
| `./scripts/demo.sh up\|check\|reset-flags` | Kit del día de la demo |
| `./scripts/rubrica.sh` | Verifica en vivo cada criterio de la rúbrica por capa (50 criterios; los efectos de prueba se revierten) |
| `./scripts/setup_runner.sh` | Instala el runner self-hosted de GitHub Actions (servicio `systemd --user`) |
| `./scripts/setup_rulesets.sh` | Aplica en GitHub los rulesets de `dev`/`test`/`prod` y la configuración de Actions |

## Documentación

| Documento | Contenido |
| --- | --- |
| [FASES-DESARROLLO.md](FASES-DESARROLLO.md) | Orden de las fases, decisiones (E1–E3, T1–T7) y cuándo queda lista cada pregunta de la sustentación |
| [docs/01-teoria-y-decisiones.md](docs/01-teoria-y-decisiones.md) | Registro de decisiones (ADR) y resultados de las pruebas de Floci |
| [docs/03-fase-1-terraform.md](docs/03-fase-1-terraform.md) | Terraform: aislamiento por workspace, restauración del tfstate, recorrido del paquete |
| [docs/04-fase-2-backend.md](docs/04-fase-2-backend.md) | GraphQL, SAGA (retención, reserva anticipada, fin automático), idempotencia, flags y trazabilidad |
| [docs/05-fase-3-docker-y-pruebas.md](docs/05-fase-3-docker-y-pruebas.md) | Imágenes endurecidas, Trivy y pruebas en CI |
| [docs/06-fase-4-kubernetes-canary.md](docs/06-fase-4-kubernetes-canary.md) | Kubernetes endurecido, NetworkPolicy, canary 20 % → 100 % y condición de promoción |
| [docs/07-fase-5-prefect.md](docs/07-fase-5-prefect.md) | Prefect: salud de ambientes, auditoría de la SAGA, detección y remediación |
| [docs/08-fase-6-frontend.md](docs/08-fase-6-frontend.md) | Consola web: paneles, despliegue same-origin, seguridad y demo de flags por ambiente |
| [docs/10-qa.md](docs/10-qa.md) | Pirámide de pruebas, matriz manual por ambiente y checklist de la rúbrica |
| [docs/evidencias/](docs/evidencias/) | Capturas reales: flags dev vs prod, reserva con vencimiento, loader, Prefect |
