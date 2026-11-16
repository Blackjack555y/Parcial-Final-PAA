# 08 — Fase 6: consola web (8 % de la rúbrica)

## Qué es

Una **consola de operaciones** (React 19 + Vite + TypeScript) para el operador de flota. Tema
oscuro, con modo claro opcional y paleta derivada del loader (violeta `#5727CC` y cian `#97E6FF`).

| Panel | Rúbrica | Datos | Tiempo real |
| --- | --- | --- | --- |
| **Electrolineras** | Estado y disponibilidad de conectores | Suscripción `stations` | WSS |
| **Reservar y activar carga** | Dispara la SAGA | Mutación `reserveAndStartCharging` + cotización `quote` | La cotización cambia al instante con la tarifa dinámica |
| **Feature flags · `<env>`** | Flags activas del ambiente | Suscripción `featureFlags`; interruptores con `setFeatureFlag` | WSS |
| **Historial de sagas** | Transacciones y compensaciones | Suscripción `sagas` + detalle `saga(sagaId)` | WSS |
| Billetera | Saldo, cobros y reembolsos | `wallet(fleetId)` | Sondeo 4 s |
| Cargas activas | Telemetría y detener la carga | `chargingSessions`, `telemetry`, `stopCharging` | Sondeo 2 s |

### Detalles pensados para la sustentación

- **El ambiente se ve siempre.** La franja superior y el distintivo toman el color del ambiente:
  `DEV` violeta, `TEST` ámbar y `PROD` rojo. La versión desplegada (`v<hash>`) aparece al lado. El
  ambiente lo informa **el gateway** (query `environment`), no la URL: la consola muestra a qué se
  conectó de verdad.
- **Estado de la conexión:** `● En vivo · WSS` en verde, o `Sondeo HTTP` en ámbar si el
  WebSocket cae. En ese caso los paneles siguen actualizándose cada 3 s y la consola vuelve a WSS
  sola al reconectar.
- **Línea de tiempo de cada saga:** `✓ Reservar → ✓ Cobrar → ✕ Activar ↩ ✓ Reembolsar ↩ ✓ Liberar`.
  Las compensaciones aparecen con flechas de retorno, en el **orden inverso** en que ocurrieron.
- **Botones de copiar** junto a `saga_id` y `correlation_id`, para pegarlos directo en el comando de
  CloudWatch (pregunta 6.4).
- **Reintento de compensación:** si una saga está `COMPENSATION_FAILED`, aparece un botón que llama a
  `retryCompensation`. Es seguro porque las compensaciones son idempotentes.
- **`idempotencyKey` por clic** (`crypto.randomUUID()`): un doble clic no cobra dos veces.
- **Loader** durante la carga inicial y mientras corre la saga. Es el servidor isométrico de
  **Juanes200122 (Uiverse.io, licencia MIT)**, con IDs únicos por instancia, tamaño por prop y
  respeto de `prefers-reduced-motion`.
- **Accesibilidad:** el estado nunca depende solo del color (ícono + texto); los interruptores usan
  `role="switch"`, el resultado de la saga se anuncia con `aria-live` y el foco es visible.

---

## Arquitectura de despliegue

```text
Navegador ──► nginx (consola, mismo namespace) ──/graphql (HTTP + WSS)──► graphql-gateway:8080
```

- **Mismo origen:** el navegador solo habla con el nginx de la consola, que reenvía `/graphql` al
  gateway **de su namespace**. No hay CORS ni URLs por ambiente dentro de la imagen: **la misma
  imagen sirve a dev, test y prod**.
- **Despliegue:** la consola vive solo en la pista **estable** (`k8s/base/frontend`). Se actualiza
  cuando el canary del backend se promueve al 100 %. Su código entra en el hash de la etiqueta de
  imagen.
- **Acceso local:** Docker Desktop no expone la IP de minikube, así que se usa port-forward:

```bash
./scripts/ui.sh start          # dev → http://127.0.0.1:3001 · test → :3002 · prod → :3003
./scripts/ui.sh token dev      # X-Admin-Token del ambiente (botón "Token admin" de la consola)
./scripts/ui.sh stop
```

### Seguridad

| Medida | Detalle |
| --- | --- |
| Contenedor | `nginx-unprivileged`, UID 10001, sistema de archivos de solo lectura, `cap_drop: ALL`; sin el entrypoint que reescribe la configuración |
| Cabeceras | CSP `default-src 'self'` (sin scripts externos ni `data:` para fuentes), `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, `server_tokens off` |
| Fuentes | Inter y JetBrains Mono **autoalojadas** (`@fontsource`); `assetsInlineLimit: 0` para que la CSP no tenga que permitir `data:` |
| **WebSocket** | El gateway solo acepta WebSocket cuyo `Origin` esté en `CORS_ORIGINS` del ambiente (protección contra *Cross-Site WebSocket Hijacking*) |
| Red | NetworkPolicy: la consola recibe tráfico solo del Ingress y solo puede hablar con el gateway |
| Token admin | Lo escribe el operador; se guarda en `localStorage` **por origen** (un token por ambiente); nunca viaja en la imagen |

**Error real del ensayo:** el WebSocket del navegador recibía **403**. Strawberry rechaza por
defecto cualquier `Origin` que no esté declarado. En lugar de desactivar la protección, se declaró
la lista de orígenes válidos de cada ambiente (`allowed_origins`). Hay pruebas unitarias y e2e que
verifican que un origen ajeno sigue siendo rechazado.

---

## Pregunta 6.4-b: cambiar una flag en dev sin redesplegar, con prod intacto

```bash
./scripts/ui.sh start                      # abre dev (:3001) y prod (:3003) lado a lado
./scripts/ui.sh token dev                  # pegar en "🔒 Token admin" de la consola de dev
```

1. En **dev**, activar *Simular falla del cargador*. El interruptor cambia en la consola de dev
   **sin recargar**: el cambio llega por la suscripción WSS `featureFlags`.
2. La consola de **prod**, abierta al lado, no cambia: su tabla es `aegisvolt-flags-prod`.
3. En dev, *Iniciar carga*. Aparece el loader mientras corre la SAGA y luego el resultado
   "↩ Rollback distribuido completado". En el historial se ve `Activar ✕ ↩ Reembolsar ✓ ↩ Liberar ✓`
   y en la billetera, la transacción `Reembolsado`.
4. Desactivar la flag: el siguiente intento termina en "⚡ Carga activa", con telemetría en vivo.

**Por qué no hay que redesplegar:**

- la flag vive en DynamoDB, no en el código ni en la configuración del pod;
- los servicios la leen con una caché de 5 s;
- el gateway la empuja a las consolas abiertas por WSS.

## Pruebas

| Suite | Qué cubre |
| --- | --- |
| `frontend/src/lib/saga.test.ts` (vitest, 7) | Línea de tiempo de la saga (orden inverso, pasos no ejecutados, reintentos) y formatos de dinero y tiempo |
| `tests/unit/test_websocket_origin.py` (2) | El gateway acepta el origen de la consola y rechaza uno ajeno |
| `tests/e2e/test_frontend.py` (5) | nginx sirve la consola con CSP y cabeceras, fallback de rutas, proxy `/graphql`, WSS con origen propio aceptado y ajeno rechazado (403) |
| CI `lint-build` | `npm ci` → `tsc --noEmit` → `vite build` → vitest; build de la imagen y Trivy |
