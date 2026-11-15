# 04 — Fase 2: microservicios, GraphQL, SAGA y feature flags (25 % de la rúbrica)

## Componentes

| Servicio | Puerto local | Tabla (dueño exclusivo) | Responsabilidad |
| --- | --- | --- | --- |
| `graphql-gateway` | 8000 | `aegisvolt-sagas-<env>`, escribe `flags` | Esquema GraphQL unificado + **orquestador SAGA** |
| `station-reservation-service` | 8001 | `aegisvolt-reservations-<env>` | Estaciones, conectores, reserva y liberación |
| `billing-wallet-service` | 8002 | `aegisvolt-billing-<env>` | Tarifas, cobro, reembolso, billeteras |
| `energy-dispatch-service` | 8003 | `aegisvolt-dispatch-<env>` | Activación del cargador, sesiones, telemetría |

- **Comunicación:** los servicios se comunican **solo por HTTP**; nunca leen la tabla de otro
  servicio (Database-per-Service).
- **Librería común:** `libs/aegis_common` concentra:
  - configuración por ambiente;
  - logs JSON y correlación;
  - clientes de AWS;
  - feature flags;
  - errores de dominio.

**Comandos:**

```bash
python scripts/seed.py dev                 # datos de demostración (3 estaciones, 3 flotas)
./scripts/run_local.sh start dev           # 4 servicios contra las tablas de dev
open http://127.0.0.1:8000/graphql         # GraphiQL (no disponible en prod)
cat .run/admin-token                       # token para mutaciones administrativas
```

---

## Esquema GraphQL

El archivo completo está en [schema.graphql](../services/graphql-gateway/schema.graphql). El check
`lint-build` falla si queda desactualizado respecto del código.

| Tipo | Operación | Qué hace |
| --- | --- | --- |
| Query | `environment` | Ambiente al que apunta el endpoint (`dev`/`test`/`prod`) |
| Query | `stations`, `station(stationId)` | Estaciones y estado de los conectores |
| Query | `wallet(fleetId)` | Saldo y transacciones (cobros y reembolsos) |
| Query | `quote(connectorType, kwh)` | Tarifa actual (muestra si la tarifa dinámica está activa) |
| Query | `sagas(limit)`, `saga(sagaId)` | Bitácora de sagas con cada paso y compensación |
| Query | `chargingSessions`, `telemetry(sagaId)` | Sesiones y telemetría en vivo |
| Query | `featureFlags` | Flags de **este** ambiente |
| Mutation | `reserveAndStartCharging(input)` | **Dispara la SAGA** |
| Mutation | `stopCharging(sagaId)` | Termina la carga y libera el conector |
| Mutation | `retryCompensation(sagaId)` 🔒 | Re-ejecuta compensaciones (demuestra la idempotencia) |
| Mutation | `setFeatureFlag(name, enabled)` 🔒 | Cambia una flag en caliente |
| Mutation | `topUpWallet(fleetId, amountCents)` 🔒 | Recarga saldo (demo) |
| Subscription | `stations`, `sagas`, `featureFlags` | Tiempo real por WSS (`graphql-transport-ws`) |

🔒 = requiere el encabezado `X-Admin-Token`.

**Protecciones de la capa GraphQL:**

- límite de alias (15), de tokens (2000) y de profundidad (8);
- introspección y GraphiQL desactivados en `prod`;
- errores internos enmascarados (`Unexpected error.`);
- validación de identificadores;
- token admin comparado en tiempo constante.

---

## La SAGA "Reservar y activar carga" (orquestación)

```text
graphql-gateway (orquestador)
   │ 1. POST /reservations            → conector AVAILABLE→RESERVED     ⟲ POST /reservations/{id}/release
   │ 2. POST /charges                 → saldo -= monto (TXN CHARGED)     ⟲ POST /charges/{id}/refund
   │ 3. POST /sessions                → cargador ACTIVE                   (último paso)
   │    POST /reservations/{id}/occupy → conector CHARGING
   ▼
bitácora aegisvolt-sagas-<env>:  META (estado) + EVENT#… (cada acción y compensación)
```

| Falla en | Estado final | Compensaciones (en orden inverso) |
| --- | --- | --- |
| Paso 3 (cargador o `SIMULATE_CHARGER_HARDWARE_FAULT`) | `COMPENSATED` | Reembolso (2) → liberación (1) |
| Paso 2 (`INSUFFICIENT_FUNDS`) | `COMPENSATED` | Liberación (1) |
| Paso 1 (conector ocupado, ultra-carga deshabilitada) | `FAILED` | Ninguna: no se hizo nada |
| Una compensación agota sus reintentos | `COMPENSATION_FAILED` | Las demás se ejecutan igual; Prefect la detecta |

**¿Por qué orquestación y no coreografía?** El flujo es corto (3 pasos) y debe poder demostrarse y
auditarse. Un único orquestador:

- deja el estado de la saga en **un solo lugar** (la tabla `sagas`);
- hace explícito el orden de las compensaciones;
- evita dependencias cíclicas entre eventos.

La coreografía escala mejor con muchos participantes, pero el flujo queda repartido y es más
difícil de rastrear.

---

## Pregunta 6.3-a: demo de la compensación en `test`

```bash
./scripts/run_local.sh start test
TOKEN=$(cat .run/admin-token)
gql() { curl -s localhost:8000/graphql -H 'content-type: application/json' \
          -H "x-admin-token: $TOKEN" -d "$1" | python3 -m json.tool; }

# 1. Activar la flag de falla SOLO en test (dev y prod no cambian: cada uno tiene su tabla)
gql '{"query":"mutation { setFeatureFlag(name: \"SIMULATE_CHARGER_HARDWARE_FAULT\", enabled: true) { name enabled } }"}'

# 2. Disparar la SAGA
gql '{"query":"mutation { reserveAndStartCharging(input: {stationId: \"bog-norte\", connectorId: \"c1\", fleetId: \"fleet-rapidoexpress\", kwh: 40}) { success message saga { sagaId status correlationId events { step action outcome detail } } } }"}'
```

**Qué mostrar en las tablas** (reemplaza `$SID` por el `sagaId` de la respuesta):

```bash
A() { aws --endpoint-url http://localhost:4566 "$@"; }
# billing: la transacción quedó REFUNDED y el saldo volvió a su valor
A dynamodb get-item --table-name aegisvolt-billing-test \
  --key '{"pk":{"S":"WALLET#fleet-rapidoexpress"},"sk":{"S":"TXN#'$SID'"}}'
# reservations: la reserva quedó RELEASED y el conector AVAILABLE
A dynamodb get-item --table-name aegisvolt-reservations-test \
  --key '{"pk":{"S":"RESERVATION#'$SID'"},"sk":{"S":"META"}}'
# sagas: la bitácora completa (acciones y compensaciones en orden)
A dynamodb query --table-name aegisvolt-sagas-test \
  --key-condition-expression 'pk = :p' --expression-attribute-values '{":p":{"S":"SAGA#'$SID'"}}'
```

**Resultado verificado** (ensayo en `dev`, 2026-11-15):

| Tabla | Ítem | Antes | Después |
| --- | --- | --- | --- |
| `billing` | `TXN#saga-…` | — | `REFUNDED`, `refund_reason = dispatch:CHARGER_FAULT` |
| `billing` | saldo de `fleet-rapidoexpress` | 50000 | 50000 (cobró 2720 y los reembolsó) |
| `reservations` | `RESERVATION#saga-…` | — | `RELEASED`, `reason = dispatch:CHARGER_FAULT` |
| `reservations` | conector `bog-norte/c1` | `AVAILABLE` | `AVAILABLE` |
| `dispatch` | `SESSION#saga-…` | — | `FAILED`, `SIMULATE_CHARGER_HARDWARE_FAULT` |

---

## Pregunta 6.3-b: ¿cómo se evita el problema si una compensación se procesa dos veces?

La llave de idempotencia es el **`saga_id`**, que viaja en cada llamada. Cada escritura es
**condicional** en DynamoDB, así que repetir una operación no repite su efecto.

| Operación | Escritura atómica (`TransactWriteItems`) | Si llega dos veces |
| --- | --- | --- |
| Reservar | Conector `si status = AVAILABLE` + reserva `si attribute_not_exists(pk)` | La reserva ya existe → devuelve la misma |
| Cobrar | TXN `si attribute_not_exists(pk)` + saldo `si balance ≥ monto` | La TXN ya existe → devuelve el mismo cobro, **no descuenta otra vez** |
| **Reembolsar** | TXN `si status = CHARGED` → `REFUNDED` + saldo `+= monto` | La condición falla → `"ya reembolsado (idempotente)"`, **el saldo no cambia** |
| **Liberar** | Conector `si reserved_by = saga_id` + reserva `si status = RESERVED` | La reserva ya está `RELEASED` → `"ya liberada"`, no hace nada |
| Activar cargador | Sesión `si attribute_not_exists(pk)` | Devuelve la sesión existente; nunca energiza dos veces |

**Por qué es atómico:** la condición y la escritura son **una sola operación en DynamoDB**, sin una
ventana entre "leer" y "escribir". Dos reembolsos concurrentes no pueden pasar ambos la condición
`status = CHARGED`.

**Consecuencias en el diseño:**

- El orquestador **reintenta** cada compensación hasta 3 veces con backoff exponencial, sin miedo
  a duplicar.
- `retryCompensation(sagaId)` re-ejecuta **todas** las compensaciones de una saga. Las ya hechas
  responden "ya reembolsado" o "ya liberada".
- `idempotencyKey` en la mutación: el mismo valor produce siempre el mismo `saga_id` (UUIDv5). Si
  el cliente reintenta, recibe la misma saga y **se cobra una sola vez**.

**Demostración:**

```bash
gql '{"query":"mutation { retryCompensation(sagaId: \"'$SID'\") { success saga { status events { step action detail } } } }"}'
# Los últimos eventos dicen "refunded=False, detail=ya reembolsado (idempotente)": el saldo no cambió
```

**Pruebas que lo cubren:**

| Prueba | Qué verifica |
| --- | --- |
| [test_saga_orchestrator.py](../tests/unit/test_saga_orchestrator.py) | Orden inverso, reintentos, `COMPENSATION_FAILED`, idempotencia |
| [test_saga_flow.py](../tests/integration/test_saga_flow.py) | Los 3 escenarios contra DynamoDB, el doble reembolso y la doble reserva |

---

## Feature flags por ambiente

| Flag | Servicio que la aplica | Efecto |
| --- | --- | --- |
| `ENABLE_DYNAMIC_SURGE_PRICING` | billing | Multiplica la tarifa ×1,5 en horas pico (`PEAK_HOURS`; en dev/test `0-24` para la demo) |
| `ENABLE_FAST_CHARGE_BOOST` | reservations | Permite reservar conectores `ULTRA` de 350 kW |
| `SIMULATE_CHARGER_HARDWARE_FAULT` | dispatch | El cargador "falla" → rollback de la SAGA |

- **Aislamiento:** cada ambiente tiene **su** tabla `aegisvolt-flags-<env>`; cambiar `dev` no puede
  tocar `prod`.
- **Valores iniciales:** los siembra Terraform con `ignore_changes`, así que un apply no revierte un
  cambio en caliente.
- **Propagación:** caché de 5 s por servicio. Un cambio se refleja en todos los servicios en ≤ 5 s,
  **sin redesplegar**.
- **Tiempo real:** la suscripción `featureFlags` lo empuja al frontend por WSS. Verificado en vivo:
  la suscripción recibió `False` y luego `True` al cambiar la flag.

---

## Trazabilidad (anticipo de la pregunta 6.4)

- **Formato:** todos los logs son JSON con `correlation_id`, `saga_id`, `env`, `service`,
  `version` y `event`.
- **Destino:** cada servicio los envía a `/aegisvolt/<env>/<servicio>` (`AEGIS_CLOUDWATCH_LOGS=true`).
- **Propagación:** el gateway reenvía `X-Correlation-Id` y `X-Saga-Id` en cada llamada.

```bash
CID=corr-…   # correlationId de la respuesta de la mutación
for g in graphql-gateway station-reservation-service billing-wallet-service energy-dispatch-service; do
  aws --endpoint-url http://localhost:4566 logs filter-log-events \
    --log-group-name /aegisvolt/test/$g --filter-pattern "{ \$.correlation_id = \"$CID\" }" \
    --query 'events[].message' --output text
done
```

**Verificado:** una saga fallida produjo 17 eventos con el mismo `correlation_id` en los 4 log groups:

- en el gateway: `saga_started` … `saga_compensated`;
- en dispatch: `charger_fault`;
- en billing: `wallet_charged` y `wallet_refunded`;
- en reservations: `connector_reserved` y `connector_released`.

**Métricas generadas por los metric filters** (namespace `AegisVolt/<env>`, dimensiones `Service` y
`Version`):

- `HttpRequests`;
- `HttpErrors`;
- `SagaCompensations`.

El canary de la Fase 4 las usará.
