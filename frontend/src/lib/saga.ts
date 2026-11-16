import type { SagaEvent } from "../api/types";

export const STEP_LABEL: Record<string, string> = {
  reserve: "Reservar",
  charge: "Retener saldo",
  arrival: "Esperar llegada",
  dispatch: "Activar",
};
export const COMPENSATION_LABEL: Record<string, string> = {
  reserve: "Liberar bahía",
  charge: "Liberar saldo",
};

export type NodeState = "ok" | "failed" | "skipped" | "pending";

export interface TimelineNode {
  key: string;
  kind: "action" | "compensation" | "settlement";
  step: string;
  label: string;
  state: NodeState;
  detail: string;
}

const STEPS = ["reserve", "charge", "dispatch"] as const;

/**
 * Convierte la bitácora de la saga en la línea de tiempo que muestra la consola:
 *   acciones en orden (Reservar → Retener saldo → [Esperar llegada] → Activar), los pasos que no
 *   llegaron a ejecutarse como "skipped", la liquidación al terminar y después las
 *   compensaciones en el orden en que ocurrieron (inverso).
 * Si una compensación se reintentó, se muestra solo su último resultado.
 */
export function buildTimeline(events: SagaEvent[]): TimelineNode[] {
  const actions = new Map<string, SagaEvent>();
  const compensations = new Map<string, SagaEvent>();
  let arrival: SagaEvent | undefined;
  let capture: SagaEvent | undefined;
  for (const e of events) {
    if (e.action === "action") actions.set(e.step, e);
    else if (e.action === "compensation") {
      compensations.delete(e.step); // conserva el orden del ÚLTIMO intento
      compensations.set(e.step, e);
    } else if (e.step === "arrival") arrival = e; // wait → confirm (o FAILED si venció)
    else if (e.action === "capture") capture = e;
  }

  const order: string[] = arrival ? ["reserve", "charge", "arrival", "dispatch"] : [...STEPS];
  const nodes: TimelineNode[] = order.map((step) => {
    const e = step === "arrival" ? arrival : actions.get(step);
    return {
      key: `a-${step}`,
      kind: "action",
      step,
      label: STEP_LABEL[step] ?? step,
      state: e ? (e.outcome === "OK" ? "ok" : "failed") : "skipped",
      detail: e?.detail ?? "no se ejecutó",
    };
  });
  if (capture) {
    nodes.push({
      key: "s-capture",
      kind: "settlement",
      step: "charge",
      label: "Liquidar",
      state: capture.outcome === "OK" ? "ok" : "failed",
      detail: capture.detail,
    });
  }

  for (const [step, e] of compensations) {
    nodes.push({
      key: `c-${step}`,
      kind: "compensation",
      step,
      label: COMPENSATION_LABEL[step] ?? step,
      state: e.outcome === "OK" ? "ok" : "failed",
      detail: e.detail,
    });
  }
  return nodes;
}
