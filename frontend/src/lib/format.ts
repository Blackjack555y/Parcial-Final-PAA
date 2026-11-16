import type { SagaStatus } from "../api/types";

export type Tone = "ok" | "info" | "warn" | "danger" | "neutral";

const usd = new Intl.NumberFormat("es-CO", { style: "currency", currency: "USD" });

export function money(cents: number | null | undefined): string {
  return cents == null ? "—" : usd.format(cents / 100);
}

export function shortId(id: string | null | undefined, keep = 8): string {
  if (!id) return "—";
  const [prefix, rest] = id.includes("-") ? [id.split("-")[0], id.slice(id.indexOf("-") + 1)] : ["", id];
  return `${prefix ? `${prefix}-` : ""}${rest.slice(0, keep)}…`;
}

export function timeAgo(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `hace ${seconds} s`;
  if (seconds < 3600) return `hace ${Math.round(seconds / 60)} min`;
  if (seconds < 86_400) return `hace ${Math.round(seconds / 3600)} h`;
  return `hace ${Math.round(seconds / 86_400)} d`;
}

export const CONNECTOR_STATUS: Record<string, { label: string; tone: Tone; icon: string; live?: boolean }> = {
  AVAILABLE: { label: "Disponible", tone: "ok", icon: "●" },
  RESERVED: { label: "Reservado", tone: "info", icon: "◐", live: true },
  CHARGING: { label: "Cargando", tone: "info", icon: "⚡", live: true },
  FAULTED: { label: "Falla", tone: "danger", icon: "✕" },
};

export const SAGA_STATUS: Record<SagaStatus, { label: string; tone: Tone; icon: string }> = {
  STARTED: { label: "Iniciada", tone: "info", icon: "…" },
  RESERVED: { label: "Reservada", tone: "info", icon: "…" },
  CHARGED: { label: "Cobrada", tone: "info", icon: "…" },
  COMPLETED: { label: "Cargando", tone: "ok", icon: "⚡" },
  FINISHED: { label: "Finalizada", tone: "neutral", icon: "✓" },
  FAILED: { label: "Rechazada", tone: "warn", icon: "⊘" },
  COMPENSATING: { label: "Compensando", tone: "warn", icon: "↩" },
  COMPENSATED: { label: "Compensada", tone: "warn", icon: "↩" },
  COMPENSATION_FAILED: { label: "Compensación fallida", tone: "danger", icon: "✕" },
};
