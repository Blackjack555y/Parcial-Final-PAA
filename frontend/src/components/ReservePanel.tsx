import { useEffect, useMemo, useState } from "react";
import { gql } from "../api/client";
import { FLEETS, M, Q } from "../api/queries";
import type { FeatureFlag, Quote, Saga, Station } from "../api/types";
import { useNow } from "../hooks/useLive";
import { clock, countdown, money } from "../lib/format";
import { Loader } from "./Loader";
import { Panel } from "./ui";

type Result = { tone: "ok" | "warn" | "danger" | "info"; title: string; body: string; saga?: Saga };

const ARRIVALS = [
  { value: 0, label: "Ya está en la bahía" },
  { value: 1, label: "Llega en 1 min" },
  { value: 2, label: "Llega en 2 min" },
  { value: 5, label: "Llega en 5 min" },
  { value: 10, label: "Llega en 10 min" },
];

export function ReservePanel({
  stations,
  flags,
  fleetId,
  onFleetChange,
  onSagaFinished,
}: {
  stations: Station[] | null;
  flags: FeatureFlag[] | null;
  fleetId: string;
  onFleetChange: (id: string) => void;
  onSagaFinished: (saga: Saga) => void;
}) {
  const boost = flags?.find((f) => f.name === "ENABLE_FAST_CHARGE_BOOST")?.enabled ?? false;
  const [stationId, setStationId] = useState("");
  const [connectorId, setConnectorId] = useState("");
  const [kwh, setKwh] = useState(30);
  const [arrival, setArrival] = useState(0);
  const now = useNow();
  const [quote, setQuote] = useState<Quote | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result | null>(null);

  const station = stations?.find((s) => s.stationId === stationId) ?? stations?.[0];
  const connectors = station?.connectors ?? [];
  const connector = connectors.find((c) => c.connectorId === connectorId);

  // Selección por defecto: primera estación y primer conector libre.
  useEffect(() => {
    if (!station) return;
    if (!stationId) setStationId(station.stationId);
    if (!connector || connector.status !== "AVAILABLE") {
      const free = station.connectors.find((c) => c.status === "AVAILABLE" && (c.type !== "ULTRA" || boost));
      setConnectorId(free?.connectorId ?? "");
    }
  }, [station, stationId, connector, boost]);

  const connectorType = connector?.type ?? "CCS2";
  // Cotización (refleja la tarifa dinámica en vivo).
  useEffect(() => {
    let cancelled = false;
    const t = setTimeout(() => {
      gql<{ quote: Quote }>(Q.quote, { t: connectorType, k: kwh })
        .then((d) => !cancelled && setQuote(d.quote))
        .catch(() => !cancelled && setQuote(null));
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [connectorType, kwh, flags]);

  const canStart = useMemo(
    () => !!station && !!connector && connector.status === "AVAILABLE" && !busy,
    [station, connector, busy],
  );

  async function start() {
    if (!station || !connector) return;
    setBusy(true);
    setResult(null);
    try {
      const data = await gql<{ reserveAndStartCharging: { success: boolean; message: string; saga: Saga } }>(
        M.start,
        {
          i: {
            stationId: station.stationId,
            connectorId: connector.connectorId,
            fleetId,
            kwh,
            // Idempotencia de punta a punta: un doble clic o un reintento no cobra dos veces.
            idempotencyKey: crypto.randomUUID(),
            arrivalMinutes: arrival,
          },
        },
      );
      const { saga, message } = data.reserveAndStartCharging;
      onSagaFinished(saga);
      showResult(saga, message);
    } catch (e) {
      setResult({ tone: "danger", title: "Error", body: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  function showResult(saga: Saga, message: string) {
    if (saga.status === "COMPLETED") {
      setResult({
        tone: "ok",
        title: "⚡ Carga activa",
        body: `Retenidos ${money(saga.amountCents)}; al terminar se cobra solo la energía entregada.`,
        saga,
      });
    } else if (saga.status === "AWAITING_ARRIVAL") {
      setResult({
        tone: "info",
        title: `⏳ Bahía reservada hasta las ${clock(saga.arrivalDeadline)}`,
        body: `Saldo retenido: ${money(saga.amountCents)}. Si el vehículo no llega a tiempo, la reserva vence y se libera todo.`,
        saga,
      });
    } else if (saga.status === "COMPENSATED") {
        setResult({ tone: "warn", title: "↩ Rollback distribuido completado", body: message, saga });
    } else {
      setResult({ tone: "danger", title: `SAGA ${saga.status}`, body: message, saga });
    }
  }

  async function act(kind: "confirm" | "cancel", saga: Saga) {
    setBusy(true);
    try {
      if (kind === "confirm") {
        const d = await gql<{ confirmArrival: { message: string; saga: Saga } }>(M.confirm, { id: saga.sagaId });
        showResult(d.confirmArrival.saga, d.confirmArrival.message);
      } else {
        await gql(M.cancel, { id: saga.sagaId });
        setResult({ tone: "warn", title: "↩ Reserva cancelada", body: "Se liberaron el saldo retenido y la bahía." });
      }
      onSagaFinished(saga);
    } catch (e) {
      setResult({ tone: "danger", title: "Error", body: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="Reservar y activar carga" hint="SAGA: reservar → retener saldo → activar">
      <div className="form">
        <div className="field">
          <label htmlFor="station">Estación</label>
          <select
            id="station"
            className="select"
            value={station?.stationId ?? ""}
            onChange={(e) => {
              setStationId(e.target.value);
              setConnectorId("");
            }}
          >
            {stations?.map((s) => (
              <option key={s.stationId} value={s.stationId}>
                {s.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="connector">Conector</label>
          <select id="connector" className="select" value={connectorId} onChange={(e) => setConnectorId(e.target.value)}>
            {connectors.map((c) => (
              <option
                key={c.connectorId}
                value={c.connectorId}
                disabled={c.status !== "AVAILABLE" || (c.type === "ULTRA" && !boost)}
              >
                {c.connectorId} · {c.type} {c.powerKw} kW
                {c.status !== "AVAILABLE" ? ` (${c.status.toLowerCase()})` : ""}
                {c.type === "ULTRA" && !boost ? " (requiere ultra-carga)" : ""}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="fleet">Flota</label>
          <select id="fleet" className="select" value={fleetId} onChange={(e) => onFleetChange(e.target.value)}>
            {FLEETS.map((f) => (
              <option key={f.id} value={f.id}>
                {f.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="arrival">Llegada del vehículo</label>
          <select id="arrival" className="select" value={arrival} onChange={(e) => setArrival(Number(e.target.value))}>
            {ARRIVALS.map((a) => (
              <option key={a.value} value={a.value}>
                {a.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="kwh">Energía estimada: {kwh} kWh</label>
          <input id="kwh" type="range" min={5} max={80} step={5} value={kwh} onChange={(e) => setKwh(Number(e.target.value))} />
        </div>
      </div>

      <div className="quote">
        <div>
          <div className="muted" style={{ fontSize: 12 }}>Se retiene (estimado)</div>
          <div className="quote-total">{money(quote?.amountCents)}</div>
        </div>
        <div className="muted" style={{ fontSize: 12 }}>
          {quote ? `${money(quote.unitPriceCents)} / kWh` : "—"}
          <br />
          {quote?.surgeActive ? (
            <span style={{ color: "var(--warn)" }}>⚠ Tarifa pico ×{quote.surgeMultiplier} activa</span>
          ) : (
            <span>Tarifa normal</span>
          )}
        </div>
        <span className="topbar-spacer" />
        <button className="btn btn-primary" type="button" disabled={!canStart} onClick={() => void start()}>
          {arrival > 0 ? "⏳ Reservar bahía" : "⚡ Iniciar carga"}
        </button>
      </div>

      {result && (
        <div className="result" data-tone={result.tone} role="status" aria-live="polite">
          <strong>{result.title}</strong>
          <div className="muted" style={{ fontSize: 13 }}>{result.body}</div>
          {result.saga?.status === "AWAITING_ARRIVAL" && (
            <div style={{ display: "flex", gap: 8, marginTop: 10, alignItems: "center", flexWrap: "wrap" }}>
              <button className="btn btn-primary btn-sm" type="button" disabled={busy} onClick={() => void act("confirm", result.saga!)}>
                🚚 Vehículo llegó · activar
              </button>
              <button className="btn btn-sm" type="button" disabled={busy} onClick={() => void act("cancel", result.saga!)}>
                Cancelar reserva
              </button>
              <span className="mono muted">vence en {countdown(result.saga.arrivalDeadline, now)}</span>
            </div>
          )}
          {result.saga?.correlationId && (
            <div className="mono faint" style={{ fontSize: 11, marginTop: 4 }}>
              {result.saga.sagaId} · {result.saga.correlationId}
            </div>
          )}
        </div>
      )}

      {busy && (
        <div className="overlay">
          <div className="overlay-card">
            <Loader size={120} label="" />
            <strong>Ejecutando la SAGA…</strong>
            <span className="muted" style={{ fontSize: 13 }}>
              Reservando la bahía, reteniendo el saldo de la flota y activando el cargador. Si un
              paso falla se ejecutan las compensaciones.
            </span>
          </div>
        </div>
      )}
    </Panel>
  );
}
