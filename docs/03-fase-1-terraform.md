# 03 — Fase 1: Infraestructura como código (30 % de la rúbrica)

## Estructura

```text
infra/
├── bootstrap/                 # bucket S3 versionado del estado (y su propio estado dentro)
├── envs/                      # raíz de los 3 ambientes: un workspace por ambiente
│   ├── backends/              # floci.s3.tfbackend | aws.s3.tfbackend
│   ├── floci.tfvars           # emulador local
│   └── aws.tfvars             # AWS real (mismo código)
└── modules/
    ├── network/               # VPC, subredes públicas/privadas en 2 AZ, IGW, NAT, rutas
    ├── vpc_endpoints/         # Gateway (S3, DynamoDB) + Interface (execute-api, logs, monitoring)
    ├── dynamodb/              # 5 tablas por ambiente + siembra de feature flags
    ├── observability/         # log groups /aegisvolt/<env>/<svc> + metric filters
    ├── eks/                   # clúster EKS con API privada
    ├── ingress_nlb/           # NLB interno delante del Ingress + PrivateLink
    └── api_gateway/           # API pública (REGIONAL) e interna (PRIVATE) con VPC Link
```

**Comandos:**

- `./scripts/tf.sh <dev|test|prod> plan|apply|output|versions|restore`
- `./scripts/tf.sh bootstrap plan|apply`

---

## Pregunta 6.1-a: ¿cómo se garantiza que un apply en `test` no toque `prod`?

Hay cinco capas de protección, de la más interna a la más externa:

1. **El ambiente es el workspace, no una variable.** `local.env = terraform.workspace`
   ([locals.tf](../infra/envs/locals.tf)), así que no existe un `-var env=prod` que se pueda
   pasar por error. Todos los nombres se derivan de él:
   - `aegisvolt-reservations-test`;
   - `/aegisvolt/test/billing-wallet-service`;
   - `aegisvolt-test-vpc`.
2. **Cada workspace tiene su propio estado en S3.** Con workspaces, el backend escribe en
   `env:/<workspace>/terraform.tfstate`. Estando en `test`, Terraform **solo carga**
   `env:/test/...`. Los recursos de `prod` ni siquiera existen en ese estado, así que no puede
   modificarlos ni destruirlos.
3. **Guardia del workspace.** Un `precondition` en `terraform_data.workspace_guard` hace fallar el
   plan si el workspace no es `dev`, `test` o `prod`. Por ejemplo, el plan en `default` falla antes
   de crear nada.
4. **Guardia del plan en CI.** [scripts/check_plan_env.py](../scripts/check_plan_env.py) revisa
   cada recurso del plan. Si aparece un nombre de otro ambiente (por ejemplo, `aegisvolt-*-prod`
   en un plan de `test`), el check `terraform-plan` falla y el ruleset bloquea el merge.
5. **El pipeline deriva el workspace de la rama.** `deploy.yml` usa `github.ref_name` (la rama que
   recibió el merge). `scripts/tf.sh` hace `terraform workspace select <rama>` y verifica que el
   workspace activo coincida antes de hacer el apply.

**Defensa en profundidad en la red:** la política del VPC Endpoint de DynamoDB solo permite
`table/aegisvolt-*-<env>`. Aunque un pod de `test` tuviera credenciales de más, a través de la VPC de
`test` no puede tocar una tabla `-prod`.

**Demostración en vivo:**

```bash
./scripts/tf.sh test plan          # solo nombres "-test"; estado env:/test/terraform.tfstate
aws --endpoint-url http://localhost:4566 s3 ls s3://aegisvolt-tfstate/env:/ --recursive
cd infra/envs && terraform workspace select default && \
  terraform plan -var-file=floci.tfvars   # → "Resource precondition failed"
```

---

## Pregunta 6.1-b: versiones del tfstate y cómo restaurar

El bucket `aegisvolt-tfstate` tiene:

- **Versionamiento habilitado.** Cada `apply` crea una versión nueva.
- **Retención:** las versiones anteriores se conservan 90 días (regla de lifecycle).
- **Cifrado** SSE-AES256.
- **Bloqueo de acceso público.**
- **Política que exige HTTPS.**
- **Bloqueo concurrente con `use_lockfile`.** Es un `terraform.tfstate.tflock` escrito con
  `If-None-Match` (verificado en la prueba F0-1).

```bash
./scripts/tf.sh dev versions                    # tabla: Fecha | Actual | Bytes | VersionId
./scripts/tf.sh dev restore <VersionId>         # pide confirmación
./scripts/tf.sh dev plan                        # muestra la diferencia entre ese estado y la realidad
```

**Cómo restaura `restore`:** copia la versión antigua *encima* del objeto actual
(`copy-object --copy-source ...?versionId=...`). Eso crea una versión **nueva** con el contenido
viejo, así que nada se borra y la restauración misma se puede deshacer.

**Equivalente manual con la CLI:**

```bash
aws s3api list-object-versions --bucket aegisvolt-tfstate --prefix env:/dev/terraform.tfstate
aws s3api get-object --bucket aegisvolt-tfstate --key env:/dev/terraform.tfstate \
    --version-id <VersionId> estado-anterior.tfstate
terraform state push -force estado-anterior.tfstate   # alternativa desde Terraform
```

---

## Pregunta 6.1-c: recorrido de un paquete de API Gateway hasta el pod

```text
Cliente
  │ HTTPS  https://<api-id>.execute-api.us-east-1.amazonaws.com/<env>/graphql
  ▼
API Gateway (REST, REGIONAL) ── stage <env>, throttling 100 rps / ráfaga 200
  │ integración HTTP_PROXY, connection_type = VPC_LINK
  ▼
VPC Link ─── ENI administrada por AWS dentro de la VPC del ambiente
  │ TCP :80
  ▼
NLB INTERNO (subredes privadas 10.x.10.0/24 y 10.x.11.0/24, sin IP pública)
  │ target group tipo "ip", health check HTTP /healthz
  ▼
Ingress Controller (pods de K8s) ── Ingress stable / canary (peso 80/20 en la Fase 4)
  │
  ▼
Service graphql-gateway ──► pod graphql-gateway
  │ llamadas a los microservicios (ClusterIP dentro del clúster)
  ▼
pods station-reservation / billing-wallet / energy-dispatch
  │ boto3 → dynamodb.us-east-1.amazonaws.com
  ▼
Tabla de rutas privada: destino = prefix list de DynamoDB → VPC Endpoint Gateway
  │ (la política del VPCE solo permite tablas aegisvolt-*-<env>)
  ▼
DynamoDB  ── el tráfico nunca sale a internet
```

**Qué tabla de rutas y qué políticas intervienen:**

| Tramo | Control |
| --- | --- |
| Cliente → API Gateway | Throttling del stage; en la API interna, una *resource policy* que niega todo lo que no venga de `aws:SourceVpce` |
| API Gateway → NLB | VPC Link (solo a ese NLB); el NLB es `internal = true` |
| NLB → pods | Subredes privadas (ruta `0.0.0.0/0` hacia NAT, nunca IGW); health check HTTP |
| Pods → DynamoDB / S3 | **Tablas de rutas privadas** con la ruta hacia la prefix list del **Gateway Endpoint** |
| Pods → CloudWatch Logs / Metrics | **Interface Endpoints** `logs` y `monitoring` con DNS privado; su *security group* solo admite 443 desde el CIDR de la VPC |
| Clientes internos → API | API `PRIVATE` a través del VPCE `execute-api` |
| Otras VPC → Kubernetes | VPC Endpoint Service (PrivateLink) sobre el NLB; en AWS real (`enable_privatelink`) |

---

## Diferencias entre Floci y AWS real

| Recurso | Floci 2.4 | AWS real |
| --- | --- | --- |
| VPC, subredes, rutas, NAT, VPCE Gateway e Interface | ✅ | ✅ |
| NLB, VPC Link, API Gateway (REGIONAL y PRIVATE) | ✅ | ✅ |
| DynamoDB (PITR, GSI, TTL, ítems) | ✅ | ✅ |
| CloudWatch Logs y metric filters con dimensiones | ✅ | ✅ |
| EKS (plano de control) | ✅ | ✅ |
| EKS node group | Desactivado: los pods corren en minikube | ✅ |
| VPC Endpoint Service (PrivateLink) | ❌ `UnsupportedOperation` → `enable_privatelink = false` | ✅ |

**Aviso conocido:** con Floci, el provider muestra `Attribute "protocol_version" cannot be specified
when "protocol" is "TCP"`. La causa es que Floci devuelve un `protocol_version` en el target group
del NLB. Es solo un aviso y no afecta al apply.

**Para AWS real:**

```bash
TF_TARGET=aws ./scripts/tf.sh bootstrap apply
TF_TARGET=aws ./scripts/tf.sh dev plan
```

En AWS real, el nombre del bucket debe ser único globalmente: cámbialo en `infra/bootstrap` y en
`backends/aws.s3.tfbackend`.

---

## CI/CD de la Fase 1

| Evento | Job | Dónde | Qué hace |
| --- | --- | --- | --- |
| PR hacia `dev`/`test`/`prod` | `terraform-plan` | Runner self-hosted | `fmt` → plan del workspace **de la rama destino** → guardia de aislamiento → comentario en el PR con el plan |
| Merge (push) a `dev`/`test`/`prod` | `terraform-apply` | Runner self-hosted | Workspace = rama → plan → guardia → `apply` del plan guardado → resumen con las versiones del tfstate |

**El runner self-hosted** se instala con `./scripts/setup_runner.sh`:

- es un servicio de `systemd --user`, sin `sudo`;
- usa la etiqueta `aegisvolt`;
- el binario se verifica por SHA-256 antes de instalarse.

Los PR de forks nunca llegan a este runner: el job corre en GitHub y falla a propósito.
