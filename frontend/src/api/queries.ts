const STATION_FIELDS = `stationId name city address availableConnectors
  connectors { connectorId type powerKw status reservedBy fleetId }`;
const FLAG_FIELDS = "name enabled description updatedBy updatedAt";
const SAGA_FIELDS = `sagaId status stationId connectorId fleetId kwh correlationId amountCents
  surgeActive failureReason failureMessage energyKwh createdAt updatedAt`;
const EVENT_FIELDS = "events { at step action outcome detail }";

export const Q = {
  environment: "{ environment { name version region } }",
  stations: `{ stations { ${STATION_FIELDS} } }`,
  flags: `{ featureFlags { ${FLAG_FIELDS} } }`,
  sagas: `{ sagas(limit: 15) { ${SAGA_FIELDS} } }`,
  saga: `query($id: String!) { saga(sagaId: $id) { ${SAGA_FIELDS} ${EVENT_FIELDS} } }`,
  wallet: `query($f: String!) { wallet(fleetId: $f) {
    fleetId name balanceCents currency
    transactions { sagaId status amountCents kwh surgeActive createdAt refundReason } } }`,
  quote: `query($t: String!, $k: Float!) { quote(connectorType: $t, kwh: $k) {
    unitPriceCents surgeActive surgeMultiplier amountCents } }`,
  sessions: `{ chargingSessions(status: "ACTIVE") { sagaId status stationId connectorId powerKw startedAt } }`,
  telemetry: `query($id: String!) { telemetry(sagaId: $id) { status elapsedS powerKw energyKwh socPercent } }`,
};

export const S = {
  stations: `subscription { stations { ${STATION_FIELDS} } }`,
  flags: `subscription { featureFlags { ${FLAG_FIELDS} } }`,
  sagas: `subscription { sagas(limit: 15) { ${SAGA_FIELDS} } }`,
};

export const M = {
  start: `mutation($i: ReserveChargeInput!) { reserveAndStartCharging(input: $i) {
    success message saga { ${SAGA_FIELDS} ${EVENT_FIELDS} } } }`,
  stop: `mutation($id: String!) { stopCharging(sagaId: $id) { success message saga { status energyKwh } } }`,
  setFlag: `mutation($n: String!, $e: Boolean!) { setFeatureFlag(name: $n, enabled: $e) { ${FLAG_FIELDS} } }`,
  retry: `mutation($id: String!) { retryCompensation(sagaId: $id) { success message saga { status } } }`,
};

// Flotas sembradas por scripts/seed.py (no hay endpoint para listar billeteras).
export const FLEETS = [
  { id: "fleet-rapidoexpress", name: "RápidoExpress Logística" },
  { id: "fleet-ultimamilla", name: "Última Milla SAS" },
  { id: "fleet-sinfondos", name: "Flota Demo Sin Fondos" },
];
