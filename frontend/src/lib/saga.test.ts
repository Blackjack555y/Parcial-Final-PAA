import { describe, expect, it } from "vitest";
import type { SagaEvent } from "../api/types";
import { money, shortId, timeAgo } from "./format";
import { buildTimeline } from "./saga";

const ev = (step: string, action: string, outcome: "OK" | "FAILED", detail = ""): SagaEvent => ({
  at: "2026-11-16T12:00:00Z",
  step,
  action,
  outcome,
  detail,
});

describe("buildTimeline", () => {
  it("camino feliz: tres acciones OK y sin compensaciones", () => {
    const nodes = buildTimeline([
      ev("reserve", "action", "OK"),
      ev("charge", "action", "OK"),
      ev("dispatch", "action", "OK"),
    ]);
    expect(nodes.map((n) => [n.label, n.state])).toEqual([
      ["Reservar", "ok"],
      ["Cobrar", "ok"],
      ["Activar", "ok"],
    ]);
  });

  it("falla del cargador: compensaciones en orden inverso", () => {
    const nodes = buildTimeline([
      ev("reserve", "action", "OK"),
      ev("charge", "action", "OK"),
      ev("dispatch", "action", "FAILED"),
      ev("charge", "compensation", "OK"),
      ev("reserve", "compensation", "OK"),
    ]);
    expect(nodes.filter((n) => n.kind === "compensation").map((n) => n.label)).toEqual([
      "Reembolsar",
      "Liberar",
    ]);
    expect(nodes.find((n) => n.step === "dispatch")?.state).toBe("failed");
  });

  it("fondos insuficientes: el paso 3 queda como no ejecutado", () => {
    const nodes = buildTimeline([
      ev("reserve", "action", "OK"),
      ev("charge", "action", "FAILED"),
      ev("reserve", "compensation", "OK"),
    ]);
    expect(nodes.find((n) => n.step === "dispatch")?.state).toBe("skipped");
  });

  it("un reintento de compensación muestra solo el último resultado", () => {
    const nodes = buildTimeline([
      ev("charge", "compensation", "FAILED", "intento 3"),
      ev("reserve", "compensation", "OK"),
      ev("charge", "compensation", "OK", "reintento manual"),
    ]);
    const comps = nodes.filter((n) => n.kind === "compensation");
    expect(comps.map((n) => [n.label, n.state])).toEqual([
      ["Liberar", "ok"],
      ["Reembolsar", "ok"],
    ]);
  });
});

describe("format", () => {
  it("money usa centavos enteros", () => {
    expect(money(2720)).toContain("27,20");
    expect(money(null)).toBe("—");
  });
  it("shortId conserva el prefijo", () => {
    expect(shortId("saga-0946ec85-6e44-4868")).toBe("saga-0946ec85…");
  });
  it("timeAgo", () => {
    const now = Date.parse("2026-11-16T12:01:00Z");
    expect(timeAgo("2026-11-16T12:00:30Z", now)).toBe("hace 30 s");
    expect(timeAgo("2026-11-16T11:00:00Z", now)).toBe("hace 1 h");
  });
});
