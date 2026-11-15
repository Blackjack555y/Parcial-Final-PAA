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
| T1 | `terraform plan` en runner self-hosted | ✅ Fase 1: plan real del workspace destino, comentado en el PR |
| T2 | Repo público | `scripts/setup_rulesets.sh` lo verifica antes de aplicar las reglas |
| T3 | Endurecimiento frente a forks | `setup_rulesets.sh` exige aprobación para PR externos y token de solo lectura |
| T4 | Canary con Ingress y metric filter | Confirmado por F0-6 |
| T5 | `use_lockfile` | Confirmado por F0-1 |
| T6 | Nombres de los checks idénticos | `pr-checks.yml` / `promote-guard.yml` ↔ `protected-branches.json` |
| T7 | Suscripciones GraphQL sobre WSS | ✅ Fase 2: `stations`, `sagas`, `featureFlags` con sondeo en el servidor |

---

## Decisiones de la Fase 1 (Terraform)

### I1. El ambiente sale de `terraform.workspace`, no de una variable

- **Motivo:** con una variable `env`, nada impide `-var env=prod` estando en el workspace `test`.
  Así, el estado de `test` terminaría con recursos de `prod`.
- **Decisión:** `local.env = terraform.workspace`, más un `precondition` que rechaza cualquier
  workspace distinto de `dev`, `test` o `prod`.

### I2. Una sola raíz con workspaces, no carpetas por ambiente

- **Motivo:** la guía exige workspaces. Con una sola raíz, los tres ambientes son idénticos por
  construcción. Las diferencias legítimas son mapas indexados por el ambiente:
  - CIDR;
  - retención de logs;
  - valores iniciales de las flags;
  - `deletion_protection` solo en `prod`.

### I3. El estado del bootstrap también vive en el bucket

- **Motivo:** ningún estado de Terraform queda solo en un disco local.
- **Cómo:** la primera vez, `tf.sh` aplica con un backend local temporal y migra el estado a
  `bootstrap/terraform.tfstate`. El bucket tiene `prevent_destroy`.

### I4. Las flags se siembran con `ignore_changes`

- **Motivo:** Terraform crea el valor inicial por ambiente. Después, los cambios en caliente (Fase 2)
  no se revierten con el siguiente apply.

### I5. Dos APIs por ambiente: pública (REGIONAL) e interna (PRIVATE)

- **Motivo:** la guía pide VPCE hacia API Gateway. Una API `PRIVATE` solo es invocable a través del
  VPCE `execute-api`, y su resource policy niega cualquier otro origen. La API `REGIONAL` es la
  entrada del frontend. Ambas llegan al clúster por el mismo VPC Link → NLB interno.

### I6. Health check HTTP en el NLB

- **Motivo:** un chequeo TCP solo verifica que el puerto esté abierto. El chequeo HTTP a `/healthz`
  saca de rotación un pod que acepta conexiones pero responde con errores. Además evita una
  incompatibilidad de Floci con los atributos del target group TCP.

### I7. Limitaciones de Floci

- **PrivateLink:** Floci 2.4 no implementa `CreateVpcEndpointServiceConfiguration`. Se usa
  `enable_privatelink = false` en `floci.tfvars` y el recurso se crea en AWS real.
- **EKS node group:** está desactivado en Floci porque los pods corren en minikube.

---

## Decisiones de la Fase 2 (backend)

### B1. SAGA por orquestación dentro de la capa GraphQL

- **Motivo:** el flujo es de 3 pasos y tiene que poder demostrarse y auditarse. Un orquestador
  deja el estado de la saga en un solo lugar y fija el orden de las compensaciones.
- **Alternativa descartada:** coreografía por eventos (SNS/SQS). Escala mejor con muchos
  participantes, pero el flujo queda repartido en varios servicios y es más difícil de rastrear.

### B2. El `saga_id` es la llave de idempotencia en los tres servicios

- **Motivo:** cada escritura es condicional (`attribute_not_exists`, `status = CHARGED`,
  `reserved_by = saga_id`). Así, repetir una acción o una compensación no repite su efecto.
- **Consecuencia:** el orquestador puede reintentar las compensaciones con seguridad.

### B3. `TransactWriteItems` dentro de cada servicio

- **Motivo:** el cobro cambia la transacción y el saldo, y la reserva cambia el conector y su
  registro. Cada par cambia junto o no cambia.
- **Alcance:** es ACID **local** a un servicio. Entre servicios la consistencia es eventual,
  mediante la SAGA.

### B4. La tabla de flags es configuración transversal

- **Motivo:** la tabla es "del ambiente", al estilo de AppConfig. Cada servicio lee la flag que
  aplica, con caché de 5 s, y solo el gateway escribe en ella (con token).
- **Por qué no duplicarla:** las flags son configuración, no datos de dominio de un servicio.
  Duplicarlas por servicio rompería la regla de "un cambio, un lugar".

### B5. Montos en centavos enteros

- **Motivo:** nunca se usa `float` para dinero. El redondeo se hace con `Decimal` y `ROUND_HALF_UP`.

### B6. Activar el conector (`occupy`) es de mejor esfuerzo

- **Motivo:** cuando el cargador ya entrega energía, compensar (reembolsar y liberar) sería peor
  que dejar una inconsistencia menor.
- **Cómo se trata:** se registra en el log y la auditoría de Prefect la detecta (conector
  `RESERVED` con una sesión `ACTIVE`).

### B7. Suscripciones con sondeo en el servidor

- **Motivo:** cada suscripción consulta DynamoDB cada 1,5 s y emite solo los cambios. Funciona con
  N réplicas del gateway sin agregar un broker de pub/sub.

### B8. Los campos fijos del log no se pueden sobrescribir

- **Contexto:** un error real detectado en el ensayo. Un evento con `service=...` reemplazaba el
  servicio emisor en la línea JSON.
- **Decisión:** el formateador protege `service`, `env`, `version`, `correlation_id`, etc.
  Las métricas por servicio dependen de ello.

---

## Decisiones de la Fase 3 (contenedores y pruebas)

### C1. Un Dockerfile por servicio, contexto en la raíz

- **Motivo:** cada servicio es una imagen independiente (despliegue y canary por separado), pero
  todas necesitan `libs/aegis_common`. El contexto en la raíz permite copiarla sin publicarla en un
  registro de paquetes.

### C2. La base se fija por digest y se aplica `apt-get upgrade`

- **Motivo:** el digest garantiza builds reproducibles. El upgrade incorpora parches publicados
  después de la imagen base: Trivy encontró CVE-2026-76844 y CVE-2026-85091 en `zlib1g`, ambos
  con parche.
- **Costo aceptado:** dos builds en fechas distintas pueden traer paquetes del SO distintos.

### C3. Trivy como gate en `lint-build`

- **Criterio:** HIGH y CRITICAL con parche disponible (`--ignore-unfixed`). Lo que no tiene parche
  no se puede corregir y bloquearía el pipeline sin aportar seguridad.

### C4. El e2e de CI usa el mismo Terraform

- **Motivo:** si el e2e creara las tablas con un script aparte, podría divergir del Terraform real.
  Usar el mismo código (con un backend local desechable) valida también la infraestructura en cada
  PR.

### C5. Pruebas e2e repetibles sobre datos persistentes

- **Motivo:** las pruebas buscan conectores libres, usan `idempotencyKey` únicas, detienen las
  cargas que inician y restauran las flags al terminar. Por eso se pueden correr contra el `dev`
  local sin "ensuciarlo".
