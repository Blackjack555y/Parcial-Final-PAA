# 07 — Fase 5: observabilidad con CloudWatch y Prefect (12 % de la rúbrica)

## Dos capas de observabilidad

| Capa | Herramienta | Qué responde |
| --- | --- | --- |
| **Eventos** (qué pasó en una petición) | CloudWatch Logs + metric filters | Trazabilidad por `correlation_id`, errores 5xx por versión (canary) |
| **Estado** (¿la arquitectura está sana y consistente?) | Prefect | Salud de los 3 ambientes y consistencia de las SAGA entre las 4 tablas |

Los logs JSON con `correlation_id` existen desde la Fase 2: ver
[docs/04](04-fase-2-backend.md#trazabilidad-anticipo-de-la-pregunta-64). Esta fase agrega Prefect.

## Arquitectura de Prefect

```text
compose.observability.yaml
┌──────────────────────────┐        ┌──────────────────────────────────────────────────┐
│ prefect-server (UI+API)  │◄───────│ prefect-flows  (python serve.py, sin root, RO)   │
│ 127.0.0.1:4200 · SQLite  │  API   │  red aegisvolt → Floci: DynamoDB + CloudWatch    │
└──────────────────────────┘        │  red minikube  → Ingress: api.<env>.aegisvolt.local│
                                    └──────────────────────────────────────────────────┘
```

```bash
./scripts/dev.sh up-observability          # requiere Floci y minikube corriendo
open http://127.0.0.1:4200                 # UI de Prefect
```

| Deployment | Horario | Qué hace |
| --- | --- | --- |
| `aegisvolt-salud-ambientes/cada-5-min` | `*/5 * * * *` | Salud de dev/test/prod |
| `aegisvolt-auditoria-saga/cada-10-min` | `*/10 * * * *` | Auditoría de consistencia de la SAGA |
| `aegisvolt-auditoria-saga/con-remediacion` | A demanda | Auditoría + `retryCompensation` de las sagas con compensación fallida |

### Flujo `aegisvolt-salud-ambientes`

Una task por ambiente (`.map`, en paralelo, con 2 reintentos). Cada una revisa:

1. **El gateway por su Ingress real.** Pide `/readyz/deep` (el gateway verifica a los 3
   microservicios) y la versión desplegada.
2. **Las 5 tablas DynamoDB** con `describe_table`; espera estado `ACTIVE`.
3. **Errores 5xx de los últimos 15 min** en CloudWatch (métrica `HttpErrors` de los metric filters).
4. **La flag de falla**: que `SIMULATE_CHARGER_HARDWARE_FAULT` esté activa en **prod** es una alerta.

| Resultado | Condición |
| --- | --- |
| `OK` | Todo en orden |
| `DEGRADED` | Algún servicio caído, errores 5xx recientes o la flag de falla activa en prod |
| `DOWN` | Gateway sin respuesta o una tabla que no está `ACTIVE` |

**Salidas:**

- artifact de tabla `salud-ambientes`;
- métrica `EnvironmentHealthy` (1/0) en `AegisVolt/<env>`;
- el flow run termina en **Failed** si algún ambiente está `DOWN`.

### Flujo `aegisvolt-auditoria-saga`

Lee las 4 tablas de cada ambiente y **cruza la bitácora del orquestador con el estado real de
cada servicio**. Es la verificación de la consistencia eventual. Las reglas
([rules.py](../observability/prefect/aegis_flows/rules.py)) son lógica pura y tienen 10 pruebas
unitarias.

| Regla | Severidad | Qué detecta |
| --- | --- | --- |
| `compensacion_fallida` | CRITICAL | Saga `COMPENSATION_FAILED`, indicando qué quedó pendiente (p. ej. "2040 ¢ sin reembolsar") |
| `cobro_sin_reembolso` | CRITICAL | Saga revertida pero la retención sigue `HELD`/`CHARGED` (no se liberó) |
| `carga_sin_sesion` | CRITICAL | Saga `COMPLETED` pero la sesión no está `ACTIVE` |
| `sesion_activa_huerfana` | CRITICAL | Cargador entregando energía sin una saga activa |
| `cobro_huerfano` | CRITICAL | Cobro `CHARGED` sin saga en la bitácora |
| `retencion_sin_liquidar` | WARNING | Saga `FINISHED` pero la retención sigue `HELD` (no se liquidó) |
| `reserva_vencida_sin_compensar` | WARNING | `AWAITING_ARRIVAL` con el plazo vencido hace más de 120 s (¿supervisor caído?) |
| `saga_atascada` | WARNING | Saga en curso (`STARTED`/`RESERVED`/`CHARGED`/`COMPENSATING`) hace más de 120 s |
| `conector_retenido` | WARNING | Saga revertida o terminada pero la reserva sigue abierta |
| `reserva_huerfana` | WARNING | Conector bloqueado por un `saga_id` inexistente |
| `conector_no_marcado` | WARNING | Carga activa con el conector en `RESERVED` (el `occupy` de mejor esfuerzo falló) |
| `saga_compensada` | INFO | Rollback correcto (se lista para visibilidad) |

**Salidas:**

- artifacts `auditoria-saga-<env>` (tabla de hallazgos) y `auditoria-saga-resumen` (markdown);
- métrica `SagaInconsistencies`;
- el flow run termina en **Failed** si hay hallazgos CRITICAL que no se remediaron.

---

## Pregunta 6.4-a: ejecución del flujo en Prefect y rastreo en CloudWatch

### Guion verificado (2026-11-16, ambiente `dev`)

**1. Provocar una compensación que falla de verdad.** Billing solo falla en el **reembolso**:

```bash
kubectl set env deploy/billing-wallet-service -n aegisvolt-dev CHAOS_ERROR_RATE=1 'CHAOS_PATH_REGEX=.*/refund$'
# + setFeatureFlag SIMULATE_CHARGER_HARDWARE_FAULT=true y una mutación reserveAndStartCharging
```

**Resultado real de la saga:**

```text
saga-0946ec85-… → COMPENSATION_FAILED
   reserve  action        OK      connector_type=CCS2
   charge   action        OK      amount_cents=2040
   dispatch action        FAILED  Falla del cargador med-poblado/c1
   charge   compensation  FAILED  intento 3: CHAOS fallo inyectado   ← 3 reintentos con backoff
   reserve  compensation  OK      released=True                      ← las demás siguen
```

**2. Auditoría:**

```bash
docker exec aegisvolt-prefect-flows prefect deployment run 'aegisvolt-auditoria-saga/cada-10-min' --watch
```

El flow run termina en **Failed**, con el hallazgo `[dev] CRITICAL compensacion_fallida`:

| Ambiente | Sagas | Por estado | CRITICAL | Consistente |
| --- | ---: | --- | ---: | :---: |
| dev | 5 | COMPENSATED=3, COMPENSATION_FAILED=1, FINISHED=1 | 1 | ❌ |

**3. Remediación.** Primero se retira la falla y se apaga la flag:

```bash
kubectl set env deploy/billing-wallet-service -n aegisvolt-dev CHAOS_ERROR_RATE- CHAOS_PATH_REGEX-
docker exec aegisvolt-prefect-flows prefect deployment run 'aegisvolt-auditoria-saga/con-remediacion' --watch
```

```text
[dev] retryCompensation(saga-0946ec85-…) → COMPENSATED
```

En DynamoDB, la transacción pasa a `REFUNDED` (2040 ¢). La siguiente auditoría queda en verde:
`dev | 5 | COMPENSATED=4, FINISHED=1 | 0 | ✅`.

**¿Por qué la remediación es segura?** `retryCompensation` vuelve a ejecutar **todas** las
compensaciones: la liberación ya hecha responde "ya liberada" y el reembolso pendiente se ejecuta
una sola vez (idempotencia, ver [docs/04](04-fase-2-backend.md)).

**4. Rastreo en CloudWatch** del `correlation_id` de esa saga (está en el META de la saga o en la
respuesta de la mutación):

```bash
for g in graphql-gateway billing-wallet-service energy-dispatch-service station-reservation-service; do
  aws --endpoint-url http://localhost:4566 logs filter-log-events --log-group-name /aegisvolt/dev/$g \
    --filter-pattern '{ $.correlation_id = "corr-…" }' --query 'events[].message' --output text
done
```

Muestra `saga_step_failed`, los 3 `saga_compensation_retry`, `saga_compensation_failed` (ERROR) en
el gateway y los `http_request` con status 500 en billing.

### En la UI de Prefect (http://127.0.0.1:4200)

- **Runs:** el historial con los estados Completed y Failed.
- **Flow run:** abrir uno muestra las tasks (`chequear-ambiente` ×3, `leer-tablas`, `auditar-saga`,
  `remediar-compensacion`) y sus logs.
- **Artifacts:** `salud-ambientes` (tabla por ambiente), `auditoria-saga-resumen` (markdown) y
  `auditoria-saga-<env>` (hallazgos).
- **Deployments:** los 3 deployments con su horario; desde ahí se lanzan con *Quick run*.
