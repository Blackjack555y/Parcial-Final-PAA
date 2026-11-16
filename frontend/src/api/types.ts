export type EnvName = "dev" | "test" | "prod";

export interface EnvironmentInfo {
  name: EnvName;
  version: string;
  region: string;
}

export interface Connector {
  connectorId: string;
  type: "CCS2" | "ULTRA";
  powerKw: number;
  status: "AVAILABLE" | "RESERVED" | "CHARGING" | "FAULTED";
  reservedBy: string | null;
  fleetId: string | null;
}

export interface Station {
  stationId: string;
  name: string;
  city: string;
  address: string;
  availableConnectors: number;
  connectors: Connector[];
}

export interface FeatureFlag {
  name: string;
  enabled: boolean;
  description: string;
  updatedBy: string;
  updatedAt: string;
}

export interface Transaction {
  sagaId: string;
  status: "CHARGED" | "REFUNDED";
  amountCents: number;
  kwh: number;
  surgeActive: boolean;
  createdAt: string;
  refundReason: string | null;
}

export interface Wallet {
  fleetId: string;
  name: string;
  balanceCents: number;
  currency: string;
  transactions: Transaction[];
}

export interface Quote {
  unitPriceCents: number;
  surgeActive: boolean;
  surgeMultiplier: number;
  amountCents: number;
}

export interface SagaEvent {
  at: string;
  step: string;
  action: string;
  outcome: "OK" | "FAILED" | "SKIPPED";
  detail: string;
}

export type SagaStatus =
  | "STARTED"
  | "RESERVED"
  | "CHARGED"
  | "COMPLETED"
  | "FINISHED"
  | "FAILED"
  | "COMPENSATING"
  | "COMPENSATED"
  | "COMPENSATION_FAILED";

export interface Saga {
  sagaId: string;
  status: SagaStatus;
  stationId: string;
  connectorId: string;
  fleetId: string;
  kwh: number;
  correlationId: string | null;
  amountCents: number | null;
  surgeActive: boolean | null;
  failureReason: string | null;
  failureMessage: string | null;
  energyKwh: number | null;
  createdAt: string;
  updatedAt: string;
  events: SagaEvent[];
}

export interface ChargingSession {
  sagaId: string;
  status: string;
  stationId: string;
  connectorId: string;
  powerKw: number;
  startedAt: string;
}

export interface Telemetry {
  status: string;
  elapsedS: number;
  powerKw: number;
  energyKwh: number;
  socPercent: number;
}
