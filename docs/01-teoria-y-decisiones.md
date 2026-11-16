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
| F0-8 minikube → Floci | ✅ (Fase 4) conectando Floci a la red Docker `minikube` | Service `floci` + EndpointSlice + NetworkPolicy de salida |

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
| T4 | Canary con Ingress y metric filter | ✅ Fase 4: reparto 80/20 medido, PROMOTE y ROLLBACK demostrados |
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

---

## Decisiones de la Fase 4 (Kubernetes y canary)

### K1. Canary por pista completa, no por servicio

- **Decisión:** la versión nueva se despliega como pista `-canary` de los 4 servicios. El gateway
  canary solo llama a los servicios canary.
- **Motivo:** una petición nunca mezcla versiones. Además, la métrica por `Version` mide la
  versión completa.
- **Costo aceptado:** durante el análisis hay 4 pods más.

### K2. Peso exacto con ingress-nginx

- **Decisión:** el peso se fija con `canary-weight`.
- **Motivo:** la rúbrica pide "20 % del tráfico". La proporción de réplicas (4:1) solo aproxima
  ese porcentaje. Medido: 80 % / 20 % (202/52).
- **A tener en cuenta:** el proyecto ingress-nginx anunció su retiro. Las alternativas son Gateway
  API (`HTTPRoute` con `weight`) o Argo Rollouts.

### K3. Etiqueta de imagen = hash del código

- **Decisión:** `git rev-parse HEAD:services HEAD:libs | sha256sum`.
- **Motivo:** el mismo código produce la misma etiqueta. Así, la promoción dev → test → prod usa la
  imagen idéntica ("build once") y un cambio solo de infraestructura no dispara un canary.

### K4. Sin tráfico suficiente, se revierte

- **Decisión:** con menos de 20 peticiones el análisis es inconcluso y se hace rollback.
- **Motivo:** no se promueve lo que no se pudo medir. El script genera tráfico sintético de solo
  lectura durante 60 s para que la medición sea posible.

### K5. Calico como CNI

- **Motivo:** el CNI por defecto de minikube no aplica NetworkPolicy. Con Calico, la política
  deny-by-default se verificó con un pod ajeno que no llegó a billing.

### K6. Floci conectado a la red de minikube, y no al revés

- **Motivo:** conectar minikube a una segunda red Docker rompe su detección de IP
  (`container addresses should have 2 values, got 3`).

### K7. Etiquetas también en las plantillas de pod

- **Contexto:** error real del ensayo. La NetworkPolicy de salida a AWS elige pods por
  `app.kubernetes.io/part-of`. Kustomize `labels` no la copia a los pods sin `includeTemplates`,
  así que los pods quedaban sin salida a Floci (timeout).

---

## Decisiones de la Fase 5 (Prefect)

### O1. Prefect audita, no corrige por su cuenta

- **Decisión:** la auditoría solo lee las tablas.
- **Motivo:** la remediación es un deployment aparte, que se lanza a demanda y actúa por la **API
  pública** del gateway (`retryCompensation`, con token admin). Nunca escribe directamente en las
  tablas de un servicio, porque eso rompería Database-per-Service.

### O2. Las reglas de auditoría son lógica pura

- **Motivo:** `rules.py` no importa Prefect ni boto3, así que se prueba en milisegundos (10
  pruebas). Los flujos solo orquestan lectura, reglas y artifacts.

### O3. La salud se mide por la puerta de entrada real

- **Motivo:** el flujo llega al gateway por el Ingress de cada ambiente
  (`Host: api.<env>.aegisvolt.local`), no por un atajo interno. Si el Ingress o las
  NetworkPolicy fallan, la salud lo refleja.

### O4. Estado Failed como señal

- **Motivo:** un flow run termina en Failed si hay ambientes `DOWN` o hallazgos CRITICAL sin
  remediar. Así el dashboard de Prefect muestra en rojo lo que requiere atención, sin leer logs.

### O5. FastAPI como extra opcional de `aegis_common`

- **Motivo:** Prefect 3.9 fija `fastapi<0.151` y los servicios usan 0.163. Los flujos solo
  necesitan la parte AWS de la librería; los servicios instalan `aegis-common[web]`.

### O6. Fallas inyectadas por ruta (`CHAOS_PATH_REGEX`)

- **Motivo:** permite provocar un fallo **real** de una sola operación (el reembolso) para
  demostrar `COMPENSATION_FAILED`, su detección y su remediación sin fabricar datos. Está apagado
  por defecto.

### O7. IP fija de Floci en la red de minikube (`192.168.49.250`)

- **Contexto:** problema real al reanudar. Floci arrancó antes que minikube y tomó la IP
  `192.168.49.2`, así que minikube no pudo iniciar ("Address already in use").
- **Decisión:** con la IP fija, el orden de arranque deja de importar.

---

## Decisiones de la Fase 6 (frontend)

### W1. La consola usa el mismo origen que su gateway

- **Decisión:** nginx sirve la consola y reenvía `/graphql`, tanto HTTP como WSS, al gateway de su
  namespace.
- **Motivo:** no hace falta CORS para las queries ni hay URLs por ambiente dentro de la imagen; la
  misma imagen sirve a los tres ambientes.
- **Alternativa descartada:** `config.js` inyectado en tiempo de arranque. Obligaba a reescribir
  archivos en un contenedor con sistema de archivos de solo lectura.

### W2. El ambiente lo informa el gateway

- **Decisión:** la query `environment` es la fuente de verdad del ambiente que se muestra.
- **Motivo:** el distintivo DEV/TEST/PROD refleja a qué se conectó la consola, no lo que alguien
  configuró.

### W3. La consola solo vive en la pista estable

- **Motivo:** la consola no lleva lógica de negocio. El canary mide el backend (errores 5xx por
  versión) y la consola se actualiza al promover.

### W4. WSS con respaldo de sondeo

- **Motivo:** si el WebSocket cae, los paneles consultan por HTTP cada 3 s y el indicador cambia a
  "Sondeo HTTP". La consola nunca muestra datos congelados sin avisarlo.

### W5. Allowlist de `Origin` para WebSocket

- **Contexto:** error real del ensayo, el navegador recibía 403.
- **Decisión:** se mantiene la protección contra *Cross-Site WebSocket Hijacking* y se declaran los
  orígenes válidos de cada ambiente en `CORS_ORIGINS`.

### W6. CSP estricta sin `data:`

- **Decisión:** fuentes autoalojadas y `assetsInlineLimit: 0`.
- **Contexto:** error real del ensayo. Vite incrustaba fuentes como `data:` URIs y la CSP las
  bloqueaba.
