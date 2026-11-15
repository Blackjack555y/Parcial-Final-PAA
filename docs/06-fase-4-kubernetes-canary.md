# 06 — Fase 4: Kubernetes y despliegue canary (25 % de la rúbrica, CI/CD)

## Clúster

- **minikube** (driver Docker, 4 CPU / 5 GB):
  - **Calico** como CNI, para que las NetworkPolicy se apliquen de verdad (el CNI por defecto de
    minikube las ignora);
  - addons `ingress` (ingress-nginx) y `metrics-server`.
- **Un namespace por ambiente:** `aegisvolt-dev`, `aegisvolt-test` y `aegisvolt-prod`.
- **EKS:** sigue declarado en Terraform por ambiente (plano de control en Floci). Los pods corren en
  minikube porque Floci no ejecuta nodos de EKS sin el socket de Docker y porque levantar tres
  clústeres no cabe en la RAM disponible.

```bash
minikube start --driver=docker --cpus=4 --memory=5g --cni=calico --addons=ingress --addons=metrics-server
```

## Manifiestos (Kustomize)

```text
k8s/
├── base/
│   ├── platform/      # por namespace: ServiceAccount sin token, NetworkPolicy deny-by-default,
│   │                  # ResourceQuota, LimitRange, Service "floci" sin selector
│   └── apps/          # UNA pista de la app: 4 Deployments + 4 Services + Ingress + PDB (track=stable)
└── overlays/<env>/
    ├── kustomization.yaml   # namespace (Pod Security "restricted") + platform + apps + ConfigMap
    ├── apps/                # host del Ingress api.<env>.aegisvolt.local (prod: 2 réplicas)
    └── canary/              # la misma pista con sufijo -canary, track=canary, 1 réplica,
                             # Ingress canary con peso controlado por el script
```

### Seguridad de los pods

Todos cumplen Pod Security **restricted**, que el namespace **hace cumplir**:

- **Usuario:** `runAsNonRoot`, UID/GID 10001.
- **Privilegios:** `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile:
  RuntimeDefault`.
- **Sistema de archivos:** `readOnlyRootFilesystem: true` con un `emptyDir` en `/tmp`.
- **Credenciales:** `automountServiceAccountToken: false`.
- **Recursos:** `requests` y `limits` en cada contenedor, más una cuota por namespace.
- **Probes:** readiness en `/readyz` y liveness en `/healthz`.

### Red (NetworkPolicy, deny-by-default)

```text
ingress-nginx ──► graphql-gateway ──► station-reservation / billing-wallet / energy-dispatch
                        │                              │
                        └──────────► Floci:4566 ◄──────┘   (+ DNS en kube-system)
```

Cualquier otro tráfico se descarta.

**Prueba realizada:** un pod cualquiera del mismo namespace **no** pudo llegar a
`billing-wallet-service` (timeout).

**Cómo llegan los pods a Floci:**

- Floci se conecta también a la red Docker `minikube`.
- `scripts/deploy.sh` crea el EndpointSlice del Service `floci` con esa IP y una NetworkPolicy
  `allow-aws-egress` que solo permite `<ip>/32:4566`.
- En EKS real, esa regla apuntaría a las IP de los VPC Endpoints (puerto 443).

---

## Estrategia canary

Hay **dos pistas completas** en cada namespace: `stable` y `canary`. El gateway canary solo llama a
los servicios canary, así que **una petición nunca mezcla versiones**: si la v3 cambia el contrato
entre el gateway y un servicio, el canary no rompe la versión estable.

El peso lo controla **ingress-nginx** (`nginx.ingress.kubernetes.io/canary-weight`). Es un
porcentaje exacto de peticiones, no una aproximación por número de pods.

### Flujo de `scripts/deploy.sh <env> <tag>`

| # | Paso | Detalle |
| --- | --- | --- |
| 0 | Etiqueta | `image-tag` = hash del código (`services/` + `libs/`). La **misma imagen** se promueve dev→test→prod (build once); un cambio solo de infraestructura no dispara un canary |
| 1 | Primer despliegue | Va directo a la pista estable (no hay versión con qué comparar) |
| 2 | Misma etiqueta | Solo reaplica configuración; sin canary |
| 3 | Pista canary | 4 Deployments `-canary` con la versión nueva, Ingress canary al 0 % |
| 4 | **20 %** | `canary-weight: 20` |
| 5 | Ventana de análisis | 60 s de tráfico sintético de solo lectura por el Ingress (≈ 250 peticiones) |
| 6 | **Condición** | Ver la pregunta 6.2-c |
| 7a | OK → **100 %** | La pista estable pasa a la versión nueva (rolling update, `maxUnavailable: 0`); canary a 0 % y se elimina |
| 7b | NO → **rollback** | Canary a 0 % y se elimina; la estable **nunca se tocó**; el job termina en rojo |

---

## Pregunta 6.2-c: ¿qué métrica o condición se evalúa antes de pasar del 20 % al 100 %?

`scripts/canary.py evaluate` consulta **CloudWatch**, namespace `AegisVolt/<env>`, filtrando por
la dimensión **`Version = <etiqueta canary>`**. Esas métricas las generan los **metric filters de
Terraform** a partir de los logs JSON de cada servicio:

| Métrica | Metric filter (Terraform, módulo `observability`) |
| --- | --- |
| `HttpRequests` | `{ $.event = "http_request" }` |
| `HttpErrors` | `{ $.event = "http_request" && $.status >= 500 }` |

Las dos condiciones deben cumplirse:

1. **Tráfico suficiente:** `HttpRequests(graphql-gateway, canary) ≥ 20`. Sin datos no se promueve.
2. **Tasa de error:** `Σ HttpErrors / Σ HttpRequests` de los 4 servicios de la versión canary
   **≤ 5 %**.

Además, todos los pods canary deben estar `Ready` y **sin reinicios**.

### Resultados reales (ensayo en `dev`, 2026-11-15)

**v1 → v2 (versión sana): PROMOTE**

```text
Tráfico sintético: 254 peticiones
  versión v1   202  (80%)
  versión v2    52  (20%)
| graphql-gateway | 52 | 0 |   …   Tasa de error 0.00% ≤ 5% ✅  →  Decisión: PROMOTE
```

**v2 → v3 con 50 % de errores inyectados (`--chaos 0.5`): ROLLBACK**

```text
  versión v2   213  (86%)
  versión v3    36  (14%)    códigos HTTP: {200: 228, 500: 21}
| graphql-gateway | 36 | 21 |  …  Tasa de error 45.65% > 5% ❌  →  Decisión: ROLLBACK
▶ ROLLBACK automático: la versión estable v2 sigue sirviendo el 100 %
```

La inyección de fallos (`CHAOS_ERROR_RATE`) está **apagada por defecto**. Solo se activa en el
gateway canary con `--chaos`, para la demo.

**Demostración en vivo:**

```bash
./scripts/deploy.sh build demo-ok   && ./scripts/deploy.sh dev demo-ok              # → PROMOTE
./scripts/deploy.sh build demo-bad  && ./scripts/deploy.sh dev demo-bad --chaos 0.5 # → ROLLBACK
./scripts/deploy.sh status dev
```

---

## Pregunta 6.2-b: ¿cómo determina el YAML el ambiente del `terraform apply`?

[deploy.yml](../.github/workflows/deploy.yml) se dispara con `push` a `dev`, `test` o `prod`. Por
el ruleset, ese push solo puede ser el merge de un PR. La rama determina todo lo demás:

```yaml
env:
  TARGET_ENV: ${{ github.ref_name }}          # rama que recibió el merge = ambiente
jobs:
  terraform-apply:   ./scripts/tf.sh "$TARGET_ENV" plan/apply   # terraform workspace select $TARGET_ENV
  deploy-k8s:        ./scripts/deploy.sh "$TARGET_ENV" "$IMAGE_TAG"  # namespace aegisvolt-$TARGET_ENV
```

- `environment: ${{ github.ref_name }}` registra cada despliegue en el GitHub Environment del
  ambiente (pestaña *Deployments*).
- `concurrency: deploy-<rama>` impide dos despliegues simultáneos del mismo ambiente.

---

## Recorrido del paquete hasta el pod (completa la pregunta 6.1-c)

| Tramo | AWS real | Local |
| --- | --- | --- |
| Entrada | API Gateway → VPC Link → NLB interno | `kubectl port-forward` al Ingress Controller (Docker Desktop no expone la IP de minikube) |
| Ingress | ingress-nginx, host `api.<env>.aegisvolt.local`, peso stable/canary | Igual |
| Pod | Service `graphql-gateway` (ClusterIP) → pod | Igual |
| Microservicios | Services ClusterIP; NetworkPolicy solo desde el gateway | Igual |
| AWS | SDK → DNS privado → VPC Endpoint (Gateway para DynamoDB, Interface para logs) | Service `floci` → EndpointSlice → contenedor Floci |

## Acceso local a un ambiente

```bash
kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller 8080:80 &
curl -H 'host: api.dev.aegisvolt.local' -H 'content-type: application/json' \
     -d '{"query":"{ environment { name version } }"}' http://127.0.0.1:8080/graphql
kubectl get secret aegisvolt-admin -n aegisvolt-dev -o jsonpath='{.data.token}' | base64 -d  # X-Admin-Token
```
