import type { SagaEvent } from "../api/types";

export const STEP_LABEL: Record<string, string> = {
  reserve: "Reservar",
  charge: "Cobrar",
  dispatch: "Activar",
};
export const COMPENSATION_LABEL: Record<string, string> = {
  reserve: "Liberar",
  charge: "Reembolsar",
};

export type NodeState = "ok" | "failed" | "skipped" | "pending";

export interface TimelineNode {
  key: string;
  kind: "action" | "compensation";
  step: string;
  label: string;
  state: NodeState;
  detail: string;
}

const STEPS = ["reserve", "charge", "dispatch"] as const;

/**
 * Convierte la bitácora de la saga en la línea de tiempo que muestra la consola:
 *   acciones en orden (Reservar → Cobrar → Activar), los pasos que no llegaron a ejecutarse
 *   como "skipped", y después las compensaciones en el orden en que ocurrieron (inverso).
 * Si una compensación se reintentó, se muestra solo su último resultado.
 */
export function buildTimeline(events: SagaEvent[]): TimelineNode[] {
  const actions = new Map<string, SagaEvent>();
  const compensations = new Map<string, SagaEvent>();
  for (const e of events) {
    if (e.action === "action") actions.set(e.step, e);
    else if (e.action === "compensation") {
      compensations.delete(e.step); // conserva el orden del ÚLTIMO intento
      compensations.set(e.step, e);
    }
  }

  const nodes: TimelineNode[] = STEPS.map((step) => {
    const e = actions.get(step);
    return {
      key: `a-${step}`,
      kind: "action",
      step,
      label: STEP_LABEL[step] ?? step,
      state: e ? (e.outcome === "OK" ? "ok" : "failed") : "skipped",
      detail: e?.detail ?? "no se ejecutó",
    };
  });

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
