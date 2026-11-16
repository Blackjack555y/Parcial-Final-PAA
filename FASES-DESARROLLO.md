# AegisVolt — Fases de desarrollo propuestas

Plan de construcción del proyecto final de *Patrones Arquitectónicos Avanzados*.

## Cómo está ordenado

El orden sigue tres principios:

1. **Esqueleto que atraviesa todas las capas primero.** Repo, ramas, rulesets y pipeline existen desde
   el día 1. Así cada fase posterior entra por PR (`feature/* → dev → test → prod`) y el historial de
   promociones se acumula como evidencia para la sustentación.
2. **Riesgos al inicio.** Lo que depende de comportamientos no documentados de Floci se prueba el día 1,
   antes de construir encima.
3. **Mayor peso de la rúbrica primero.** Al terminar la Fase 2 (día 7) están cubiertas las capas que
   suman el 80 % de la nota (IaC 30 %, CI/CD 25 %, backend 25 %). Si algo se atrasa, lo que se recorta
   es lo que menos pesa.

Cada fase produce sus documentos en `aegisvolt-plan/` y termina con un **punto de control**. Si el punto
de control falla, no se pasa a la siguiente.

---

## Decisiones base

| Tema | Decisión |
| --- | --- |
| Equipo de desarrollo | Linux (CachyOS, Arch), shell zsh/fish; todos los scripts en **Bash (`.sh`)** |
| Nube | Floci 2.4 (`floci/floci:latest`, puerto 4566) en Docker Compose; Terraform portable a AWS real cambiando solo el `.tfvars` |
| Backend | Python **3.12** dentro de los contenedores (`python:3.12-slim`): FastAPI + Strawberry (GraphQL) + boto3 |
| Python en el host | Entorno virtual con Python 3.12 mediante `uv` (el sistema trae 3.14, que Prefect y algunas librerías aún no soportan bien) |
| Frontend | React + Vite + TypeScript |
| Kubernetes | EKS por workspace en Floci (plano de control) + un solo minikube con un namespace por ambiente y el addon `ingress` |
| SAGA | Orquestación síncrona en la capa GraphQL, con bitácora en DynamoDB |
| Feature flags | Propias en DynamoDB, una tabla por ambiente, caché de 5 s |
| Observabilidad | Logs JSON con `correlation_id` enviados a CloudWatch **desde la librería común**, no añadidos al final |
| Canary | Peso de tráfico exacto con Ingress canary (`canary-weight: 20` → `100`), promovido o revertido por un script del pipeline |
| Tiempo real | Suscripciones GraphQL sobre **WSS** (`graphql-transport-ws`), con sondeo como respaldo |
| CI/CD | GitHub Actions, con runners de GitHub y un runner self-hosted |
| Repositorio | Público, con rulesets en `dev`, `test` y `prod` desde el día 1 |

---

## Decisiones de entorno

### E1. Scripts en Bash y sin `.wslconfig`

El equipo es Linux nativo, así que el ajuste de memoria de WSL no aplica. Todos los scripts usan
`set -euo pipefail` y se validan con `shellcheck`:

- `scripts/dev.sh`
- `scripts/setup_rulesets.sh`
- `scripts/floci_smoke.sh`
- `scripts/deploy.sh`

### E2. Instalación de herramientas y arreglo de Docker

Hoy faltan kubectl, minikube y Helm, la AWS CLI es v1 y el `credsStore` de Docker (pass/gpg) está roto.

```bash
sudo pacman -S --needed kubectl minikube helm shellcheck uv
sudo pacman -S aws-cli-v2      # reemplaza a aws-cli v1 (pacman pide confirmar el conflicto)
```

Para reparar Docker hay dos opciones:

- **Opción A (simple):** quitar la línea `"credsStore": "pass"` de `~/.docker/config.json`.
- **Opción B:** inicializar el almacén con `pass init <id-gpg>`.

### E3. Python 3.12 en contenedores y en el host

- **Contenedores:** todos los Dockerfiles parten de `python:3.12-slim`.
- **Host:** las pruebas y Prefect corren en un entorno creado con
  `uv venv --python 3.12 && source .venv/bin/activate` (o `activate.fish`).
- **Versión fijada:** `pyproject.toml` declara `requires-python = ">=3.12,<3.13"`.

---

## Decisiones técnicas alineadas con la rúbrica

### T1. `terraform plan` en el runner self-hosted

**Rúbrica:** *"terraform plan en MR"* y *"validando el plan antes de autorizar el merge"*. Un plan contra
un Floci vacío siempre muestra "crear todo", así que no valida nada.

**Decisión:**

- El job `terraform-plan` corre en el runner self-hosted y lee el tfstate real del workspace destino
  (`env:/<ws>/terraform.tfstate`).
- El plan se publica como comentario del PR y como artifact.
- El job falla si el plan intenta destruir recursos con el prefijo de otro ambiente.
- Los demás checks corren en runners de GitHub con Floci efímero.

### T2. Repositorio público

**Rúbrica:** *"reglas de protección de ramas (bloqueo de force push y push directo)"*, que se demuestran
en vivo (6.2). En una cuenta gratuita, esas reglas solo se aplican en repos públicos.

**Decisión:** repo **público**, con rulesets que bloquean el force push, el borrado y el push directo,
y que exigen PR y checks.

### T3. Runner self-hosted protegido frente a forks

Un repo público con runner self-hosted permite que un fork ejecute código en tu equipo. Se mitiga así:

- **Settings → Actions → General:** "Require approval for all outside collaborators".
- **Jobs self-hosted** solo con `if: github.event.pull_request.head.repo.full_name == github.repository`
  o en `push` a ramas protegidas.
- **Disparador:** nunca `pull_request_target`.
- **Permisos y aislamiento:** `permissions:` mínimos; el runner corre con un usuario sin privilegios, con
  la etiqueta `self-hosted, aegisvolt` y sin credenciales de AWS real.

### T4. Canary con peso de tráfico exacto (Ingress canary)

**Rúbrica:** *"10 % - 20 % del tráfico hacia la nueva versión"*. Una proporción de pods 4:1 solo
aproxima el tráfico, así que se descarta como mecanismo principal.

**Decisión:** el addon `ingress` de minikube (ingress-nginx) con dos Ingress sobre el mismo host:

| Ingress | Apunta a | Configuración |
| --- | --- | --- |
| Estable | Service `stable` | Sin anotaciones de canary |
| Canary | Service `canary` | `nginx.ingress.kubernetes.io/canary: "true"` y `canary-weight: "20"` |

**Lógica de `scripts/canary.py`:**

1. Aplica el peso de 20 %.
2. Espera una ventana de observación.
3. Evalúa la condición de promoción (la pregunta 6.2):
   - pods canary en `Ready`;
   - tasa de error ≤ 5 % para `version=<nueva>`;
   - un mínimo de N peticiones.
4. Si se cumple, sube el peso a 100 %, actualiza `stable` a la nueva imagen y pone el canary en 0.
   Si no, pone el peso en 0 y elimina el canary (rollback).

**Fuente de la métrica:** depende de la prueba F0-6.

- Si Floci convierte los logs en métricas, se usa el metric filter de CloudWatch con dimensión `version`.
- Si no, los servicios publican `ErrorCount` y `RequestCount` con `PutMetricData`, con la misma dimensión.

**Respaldo:** proporción de réplicas 4:1. **Evolución:** Argo Rollouts o pesos con Gateway API
(`HTTPRoute`). El proyecto ingress-nginx anunció su retiro; la respuesta queda preparada en el guion
de la sustentación.

### T5. Bloqueo del estado

La rúbrica evalúa el estado en S3, el versionamiento y el aislamiento por workspace, no el mecanismo
de bloqueo.

**Decisión:**

- **Mecanismo principal:** `use_lockfile = true` (bloqueo nativo de S3, el recomendado actualmente).
- **Plan B:** `dynamodb_table` con la tabla de lock que crea `bootstrap/`.
- **Cuál se usa:** lo decide la prueba F0-1.

### T6. Nombres de los checks idénticos en el YAML y en el ruleset

**Rúbrica:** *"checks de CI obligatorios en verde antes de habilitar el botón de merge"*.

**Decisión:** cada job declara un `name:` fijo, y el ruleset lista exactamente los mismos nombres:

- `lint-build`
- `unit-tests`
- `integration-tests`
- `e2e-tests`
- `terraform-plan`
- `promote-guard`

Los seis existen **desde la Fase 0**, con verificaciones reales mínimas que crecen en cada fase. Si un
nombre no coincide, el merge queda bloqueado para siempre.

**Error típico:** `pytest` sin pruebas termina con código 5. En la Fase 0, cada suite arranca con al
menos una prueba real.

### T7. Tiempo real con suscripciones GraphQL sobre WSS

**Rúbrica:** *"reflejando en tiempo real"*. El diagrama de la guía marca `HTTPS / WSS`.

**Decisión:**

- **Suscripciones:** Strawberry expone `stationUpdates`, `sagaUpdates` y `featureFlagsChanged` sobre
  `graphql-transport-ws`.
- **Varias réplicas:** cada suscripción consulta DynamoDB en el servidor cada 1–2 s y emite solo los
  cambios, así funciona con varias réplicas del gateway sin un pub/sub.
- **Ruta del tráfico:** queries y mutaciones van por HTTPS a través de API Gateway; las suscripciones
  van por WSS al Ingress de la capa GraphQL. En AWS real equivaldría a una API Gateway WebSocket API.
- **Respaldo:** si el WebSocket cae, el frontend vuelve al sondeo cada 3 s.

---

## Fases

Resumen:

| Fase | Días | Contenido | Rúbrica que cubre |
| --- | --- | --- | --- |
| 0 | 1 | Entorno, pruebas de Floci y gobernanza Git | CI/CD 25 % (rulesets y checks) |
| 1 | 2–4 | Terraform completo, promovido a los 3 ambientes | IaC 30 % |
| 2 | 5–7 | Backend, SAGA y flags, con logs trazables | Backend 25 % + logs del 12 % |
| 3 | 8 | Docker y pruebas reales en CI | CI/CD 25 % (tests) |
| 4 | 9–10 | Kubernetes, canary y deploy por rama | CI/CD 25 % (apply dinámico y canary) |
| 5 | 11 | Prefect | Observabilidad 12 % |
| 6 | 12 | Frontend | Presentación 8 % |
| 7 | 13–14 | Release final, QA y ensayo | Todo |

### Documentos de `aegisvolt-plan/`

| Documento | Fase |
| --- | --- |
| `00-LEEME.md`: índice, prerrequisitos, calendario, árbol final del repo | Antes de la Fase 0 |
| `01-teoria-y-decisiones.md`: teoría por capa + ADR de E1–E3 y T1–T7 | Antes de la Fase 0 |
| `02-fase-0-entorno-floci-gobernanza.md` | 0 |
| `03-fase-1-terraform.md` | 1 |
| `04-fase-2a-microservicios.md` | 2 |
| `05-fase-2b-graphql-saga-flags.md` | 2 |
| `06-fase-3-docker-y-pruebas.md` | 3 |
| `07-fase-4-kubernetes-canary-deploy.md` | 4 |
| `08-fase-5-prefect.md` | 5 |
| `09-fase-6-frontend.md` | 6 |
| `10-qa.md` | 7 |
| Guion de la sustentación y guía de estudio | 7 (material personal, fuera del repositorio) |

Ya no hay un documento de investigación aparte. Las buenas prácticas y sus fuentes van en la sección
"Teoría previa" de cada fase.

---

### Fase 0 — Entorno, pruebas de Floci y gobernanza Git (día 1)

**Objetivo:** tener las herramientas listas, los riesgos resueltos y el repo protegido antes de escribir
código de negocio.

#### Entorno

- **Herramientas y Python:** E1, E2 y E3.
- **Calidad:** `.gitignore`, `.editorconfig`, `pyproject.toml` (ruff, pytest),
  `.pre-commit-config.yaml` (ruff, shellcheck, `terraform fmt`, gitleaks).
- **Floci:** `compose.yaml` con almacenamiento persistente; `scripts/dev.sh` para levantar y bajar el
  stack.

#### Pruebas de Floci (`scripts/floci_smoke.sh`)

Cada prueba decide una alternativa del plan:

| # | Prueba | Si falla |
| --- | --- | --- |
| F0-1 | Escritura condicional en S3 (`put-object --if-none-match '*'` dos veces; la segunda debe fallar) | T5: lock con DynamoDB |
| F0-2 | Bucket con versionamiento + `list-object-versions` | Bloqueante: hay que investigarlo antes de seguir |
| F0-3 | Crear VPC, subred, VPC Endpoint Gateway e Interface | El módulo se mantiene en Terraform y se activa con un flag; el recorrido del paquete se explica con el diseño para AWS real |
| F0-4 | API Gateway con VPC Link + NLB | Igual que F0-3 |
| F0-5 | EKS `create-cluster` y `describe-cluster` | El EKS queda declarado en Terraform y desactivado con un flag; la ejecución real va en minikube |
| F0-6 | Metric filter → `put-log-events` → `get-metric-statistics` devuelve datos | T4: `PutMetricData` directo |
| F0-7 | `filter-log-events` con el patrón `{ $.correlation_id = "x" }` | Búsqueda por texto simple |
| F0-8 | Un pod en minikube llega a Floci (`host.minikube.internal:4566` o red Docker compartida) | Conectar minikube a la red de Compose |

#### Gobernanza Git

1. Crear el repo **público** en GitHub y las ramas `dev`, `test` y `prod` desde el commit inicial.
2. Crear `.github/rulesets/protected-branches.json` y `scripts/setup_rulesets.sh`, que bloquean el force
   push, el borrado y el push directo, y exigen PR y los 6 checks (T2, T6).
3. Crear `pr-checks.yml` con los 6 jobs ya nombrados. En esta fase hacen verificaciones mínimas reales:
   - `lint-build`: ruff y shellcheck.
   - `unit-tests`: una prueba de humo.
   - `integration-tests` y `e2e-tests`: Floci arranca y responde a `/_floci/health`.
   - `terraform-plan`: `fmt -check` y `validate`.
   - `promote-guard`: el flujo de promoción real.
4. Crear `promote-guard.yml`, que solo acepta `feature/* → dev`, `dev → test` y `test → prod`.
5. Agregar la plantilla de PR y CODEOWNERS.
6. Registrar el runner self-hosted con el endurecimiento de T3.

**Punto de control:**

- `docker pull hello-world`, `kubectl`, `minikube`, `helm` y `aws --version` (`aws-cli/2.x`) responden.
- Las pruebas F0-1 a F0-8 están ejecutadas y cada alternativa decidida queda anotada en `01`.
- `git push --force origin test` y `git push origin dev` son **rechazados**.
- Un PR `feature/fase-0 → dev` corre los 6 checks en verde y se fusiona. Luego se promueve
  `dev → test → prod`.
- **Desde aquí, todo cambio entra por PR.**

---

### Fase 1 — Terraform completo (días 2–4) · IaC 30 %

**Objetivo:** tener el estado versionado en S3 y los tres ambientes aislados por workspace.

**Contenido:**

- **`infra/bootstrap/`:** bucket S3 versionado y cifrado, con bloqueo de acceso público, más la tabla de
  lock (plan B de T5).
- **`infra/envs/`:**
  - backend S3, workspaces y validación que prohíbe el workspace `default`;
  - `${terraform.workspace}` en todos los nombres;
  - `floci.tfvars` y `aws.tfvars`.
- **Módulos:**

  | Módulo | Qué crea |
  | --- | --- |
  | `network` | VPC, subredes públicas y privadas en 2 zonas, tablas de ruta |
  | `vpc_endpoints` | Gateway (S3, DynamoDB) e Interface (`execute-api`, `logs`, `monitoring`) |
  | `dynamodb` | `reservations`, `billing`, `dispatch`, `sagas` y `flags`; siembra de flags con `ignore_changes` |
  | `observability` | Log groups `/aegisvolt/<env>/<servicio>` + metric filter (si F0-6 pasó) |
  | `eks` | Clúster por workspace |
  | `api_gateway` | API con VPC Link + NLB interno |
  | `k8s_namespace` | Namespace por ambiente en minikube |

**Cómo entra al repo:**

- Un PR por grupo de módulos (red, datos, observabilidad, cómputo).
- Cada PR se promueve `dev → test → prod`.
- Cada apply deja una versión nueva del tfstate en S3, que es la evidencia de la pregunta 6.1.
- Desde esta fase, `terraform-plan` (T1) muestra el diff real en el comentario del PR.

**Punto de control:**

- `terraform workspace list` muestra `dev`, `test` y `prod`.
- `aws s3api list-object-versions` muestra varias versiones de `env:/dev/terraform.tfstate`.
- Se practica la restauración de una versión anterior del tfstate.
- La prueba de bloqueo concurrente (T5) falla como se espera.
- Las tablas `aegisvolt-*-dev|test|prod` existen sin colisiones.
- Un `plan` en `test` no menciona ningún recurso `-prod`.

---

### Fase 2 — Backend, SAGA y flags (días 5–7) · Backend 25 % + logs del 12 %

**Objetivo:** que la SAGA y sus compensaciones se puedan demostrar y que cada paso sea trazable por
`correlation_id`.

**`04-fase-2a-microservicios.md`:**

- Librería `aegis_common`:
  - configuración por ambiente;
  - **logging JSON** con `correlation_id`, `saga_id`, `env`, `service` y `version`;
  - **envío a CloudWatch** hacia `/aegisvolt/<env>/<servicio>`;
  - propagación del encabezado `X-Correlation-Id`;
  - cliente DynamoDB y errores.
- Los tres microservicios:
  - `station-reservation-service`: reservar y liberar el conector.
  - `billing-wallet-service`: cobrar y reembolsar con `TransactWriteItems`.
  - `energy-dispatch-service`: activar el cargador, con fallo inducido por flag, y telemetría.

**`05-fase-2b-graphql-saga-flags.md`:**

- Esquema GraphQL con queries, mutaciones y suscripciones WSS (T7).
- Orquestador SAGA con su bitácora y compensaciones en orden inverso.
- **Idempotencia:** `saga_id` como llave + escrituras condicionales. Una compensación repetida no tiene
  efecto.
- Flags `ENABLE_DYNAMIC_SURGE_PRICING`, `ENABLE_FAST_CHARGE_BOOST` y `SIMULATE_CHARGER_HARDWARE_FAULT`,
  con mutación protegida por token.

**Pruebas:** `unit-tests` pasa a ser real (lógica de la saga, compensaciones e idempotencia).

**Punto de control:** tres escenarios por GraphQL, verificados en las tablas y en los logs:

- **Camino feliz:** la sesión queda activa.
- **Fondos insuficientes:** se libera el conector.
- **Fallo de cargador:** se reembolsa y luego se libera.
- **En los tres:** `aws logs filter-log-events` encuentra el `correlation_id` en los log groups de los
  servicios que participaron.

---

### Fase 3 — Docker y pruebas reales en CI (día 8) · CI/CD 25 %

**Objetivo:** que los checks del PR sean completos y reales.

**Contenido:**

- Dockerfiles sobre `python:3.12-slim`, sin root y con sistema de archivos de solo lectura.
- `.dockerignore`, `compose.apps.yaml` (stack completo) y `scripts/seed.py`.
- **Checks completos:**
  - `lint-build` construye las imágenes y valida el esquema GraphQL exportado.
  - `integration-tests` corre contra DynamoDB en Floci.
  - `e2e-tests` levanta el stack en el runner y prueba los tres escenarios por GraphQL.

**Punto de control:**

- Un PR con una prueba rota queda **bloqueado** para el merge.
- El mismo PR, ya corregido, se fusiona.

---

### Fase 4 — Kubernetes, canary y deploy por rama (días 9–10) · CI/CD 25 %

**Objetivo:** que el merge a cada rama aplique solo su ambiente y despliegue con canary.

**Contenido:**

- `minikube addons enable ingress`.
- `k8s/base`: Deployments, Services e Ingress `stable` y `canary`, NetworkPolicy deny-by-default,
  ResourceQuota y Pod Security `restricted`.
- Overlays `dev`, `test` y `prod`, cada uno con su namespace y su tabla de flags.
- `scripts/canary.py` (T4) y `scripts/deploy.sh`.
- `deploy.yml` (self-hosted), en `push` a `dev`, `test` o `prod`:
  1. `ENV=${GITHUB_REF_NAME}`;
  2. `terraform workspace select $ENV`;
  3. `terraform apply -auto-approve`;
  4. build de imágenes y carga en minikube;
  5. canary.

**Punto de control:**

- Con `canary-weight: 20`, alrededor de 20 de cada 100 peticiones responden con la versión nueva.
- Una imagen sana promueve al 100 %.
- Una imagen con errores inducidos revierte sola a 0 %.
- El merge a `dev` solo modifica el workspace y el namespace `dev`.

---

### Fase 5 — Prefect (día 11) · Observabilidad 12 %

**Objetivo:** supervisar la arquitectura y auditar las sagas desde Prefect.

**Contenido:**

- Prefect 3 en el entorno de Python 3.12 (servidor en Compose).
- `health_flow.py`: salud del gateway y de los servicios, más existencia de las tablas, en los 3
  ambientes.
- `saga_audit_flow.py`:
  - sagas huérfanas (reserva sin cobro y sin compensación);
  - sagas atascadas en `RUNNING`;
  - sagas compensadas;
  - inconsistencias entre tablas (cobro sin reembolso en una saga compensada).
- `serve.py`: programación periódica; publica artifacts (tablas y markdown) en el dashboard.

**Punto de control:**

- El dashboard de Prefect muestra la ejecución de los dos flujos.
- La auditoría detecta una saga compensada provocada en la Fase 2.

---

### Fase 6 — Frontend (día 12) · Presentación 8 %

**Objetivo:** tener una UI conectada en tiempo real a cada ambiente.

**Contenido:**

- React con cuatro paneles:
  - estaciones y conectores;
  - flujo de reserva y activación;
  - visor de flags por ambiente;
  - historial de sagas y compensaciones.
- Cliente `graphql-ws` con respaldo de sondeo (T7).
- `public/config.js` inyectado por ambiente (una sola imagen para los tres).
- Dockerfile con nginx sin root; se despliega con el mismo canary.

**Punto de control:**

- Cambiar una flag en `dev` cambia la UI de `dev` **sin recargar ni redesplegar**.
- `prod` permanece intacto.

---

### Fase 7 — Release final, QA y ensayo (días 13–14)

**Objetivo:** que los tres ambientes tengan la versión final y que la demo esté ensayada.

**Contenido:**

- Promover la versión final `dev → test → prod` por PR.
- `10-qa.md`:
  - pruebas unitarias, de integración y e2e (con código);
  - matriz de pruebas manuales por ambiente;
  - checklist por rúbrica.
- Guion de la sustentación (material personal, fuera del repositorio):
  - guion de la demo;
  - respuesta a cada pregunta del banco con su comando exacto;
  - respuestas preparadas sobre T4 (retiro de ingress-nginx) y T7 (WSS fuera de API Gateway);
  - planes B.

**Punto de control:**

- Ensayo completo de la demo sin improvisar comandos.
- En `test` se activa `SIMULATE_CHARGER_HARDWARE_FAULT` y se muestra la compensación mientras `prod`
  sigue intacto.

---

## Cuándo queda lista cada pregunta del banco

| Pregunta | Capa | Lista al terminar |
| --- | --- | --- |
| 6.2: force push o push directo a `test` bloqueado | CI/CD 25 % | Fase 0 |
| 6.1: `apply` en `test` no pisa tablas ni estado de `prod` | IaC 30 % | Fase 1 |
| 6.1: versiones del tfstate de `dev` en S3 y cómo restaurar | IaC 30 % | Fase 1 (crece con cada apply) |
| 6.1: recorrido del paquete API Gateway → VPCE → pod | IaC 30 % | Fase 1 (explicación), Fase 4 (pod real) |
| 6.3: fallo inducido en `test` y compensación en las tablas | Backend 25 % | Fase 2 |
| 6.3: idempotencia de la compensación | Backend 25 % | Fase 2 |
| 6.4: `correlation_id` de una saga fallida en CloudWatch | Observabilidad 12 % | Fase 2 |
| 6.2: cómo el YAML decide el ambiente del `apply` | CI/CD 25 % | Fase 4 |
| 6.2: métrica que evalúa el canary antes del 100 % | CI/CD 25 % | Fase 4 |
| 6.4: ejecución del flujo de observabilidad en Prefect | Observabilidad 12 % | Fase 5 |
| 6.4: flag en `dev` sin redesplegar, `prod` intacto | Presentación 8 % | Fase 6 |
