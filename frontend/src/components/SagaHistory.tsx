import { Fragment, useEffect, useState } from "react";
import { getAdminToken, gql } from "../api/client";
import { M, Q } from "../api/queries";
import type { Saga } from "../api/types";
import { useNow } from "../hooks/useLive";
import { countdown, money, SAGA_STATUS, shortId, timeAgo } from "../lib/format";
import { buildTimeline } from "../lib/saga";
import { Chip, CopyButton, Panel } from "./ui";

function Timeline({ saga }: { saga: Saga }) {
  const nodes = buildTimeline(saga.events);
  return (
    <div className="timeline" aria-label="Pasos y compensaciones de la saga">
      {nodes.map((n, i) => (
        <Fragment key={n.key}>
          {i > 0 && (
            <span className="arrow" data-back={n.kind === "compensation"} aria-hidden="true">
              {n.kind === "compensation" ? "↩" : n.kind === "settlement" ? "⇒" : "→"}
            </span>
          )}
          <span className="node" data-state={n.state} data-kind={n.kind} title={n.detail}>
            {n.state === "ok" ? "✓" : n.state === "failed" ? "✕" : "·"} {n.label}
          </span>
        </Fragment>
      ))}
    </div>
  );
}

function SagaDetail({ sagaId }: { sagaId: string }) {
  const now = useNow();
  const [saga, setSaga] = useState<Saga | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const load = () => gql<{ saga: Saga }>(Q.saga, { id: sagaId }).then((d) => setSaga(d.saga));

  useEffect(() => {
    void load();
  }, [sagaId]);

  if (!saga) return <div className="saga-detail empty">Cargando bitácora…</div>;
  return (
    <div className="saga-detail">
      <Timeline saga={saga} />
      <dl className="kv">
        <dt>saga_id</dt>
        <dd className="mono">
          {saga.sagaId} <CopyButton value={saga.sagaId} />
        </dd>
        <dt>correlation_id</dt>
        <dd className="mono">
          {saga.correlationId ?? "—"} {saga.correlationId && <CopyButton value={saga.correlationId} />}
        </dd>
        <dt>Flota · energía</dt>
        <dd>
          {saga.fleetId} · {saga.kwh} kWh pedidos · retenido {money(saga.amountCents)}
          {saga.surgeActive ? " (tarifa pico)" : ""}
        </dd>
        {saga.status === "FINISHED" && (
          <>
            <dt>Liquidación</dt>
            <dd>
              {saga.energyKwh} kWh entregados · cobrado <strong>{money(saga.capturedCents)}</strong> ·
              liberado {money(saga.releasedCents)}
              {saga.finishReason === "target_reached" ? " · cierre automático" : " · detenida por el operador"}
            </dd>
          </>
        )}
        {saga.arrivalDeadline && (
          <>
            <dt>Llegada</dt>
            <dd>
              plazo {new Date(saga.arrivalDeadline).toLocaleTimeString("es-CO")}
              {saga.status === "AWAITING_ARRIVAL" && ` · vence en ${countdown(saga.arrivalDeadline, now)}`}
            </dd>
          </>
        )}
        {saga.failureReason && (
          <>
            <dt>Motivo</dt>
            <dd>
              <span className="mono">{saga.failureReason}</span> — {saga.failureMessage}
            </dd>
          </>
        )}
        {saga.events.map((e) => (
          <Fragment key={`${e.at}-${e.step}-${e.action}`}>
            <dt className="mono">
              {e.action === "compensation" ? "↩ " : ""}
              {e.step}
            </dt>
            <dd>
              <span style={{ color: e.outcome === "OK" ? "var(--ok)" : "var(--danger)" }}>{e.outcome}</span>{" "}
              <span className="muted">{e.detail}</span>
            </dd>
          </Fragment>
        ))}
      </dl>
      {saga.status === "AWAITING_ARRIVAL" && (
        <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
          <button className="btn btn-primary btn-sm" type="button"
            onClick={() => void gql(M.confirm, { id: saga.sagaId }).then(() => load()).catch((e: Error) => setMessage(e.message))}>
            🚚 Vehículo llegó · activar
          </button>
          <button className="btn btn-sm" type="button"
            onClick={() => void gql(M.cancel, { id: saga.sagaId }).then(() => load()).catch((e: Error) => setMessage(e.message))}>
            Cancelar reserva
          </button>
        </div>
      )}
      {saga.status === "COMPENSATION_FAILED" && (
        <button
          className="btn btn-sm"
          type="button"
          style={{ marginTop: 12 }}
          onClick={() => {
            if (!getAdminToken()) return setMessage("Requiere token admin.");
            void gql(M.retry, { id: saga.sagaId }, { admin: true })
              .then(() => load())
              .catch((e: Error) => setMessage(e.message));
          }}
        >
          ↻ Reintentar compensación (idempotente)
        </button>
      )}
      {message && <p className="error-text">{message}</p>}
    </div>
  );
}

export function SagaHistory({ sagas, error, highlight }: { sagas: Saga[] | null; error: string | null; highlight: string | null }) {
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => {
    if (highlight) setOpen(highlight);
  }, [highlight]);

  return (
    <Panel title="Historial de sagas" className="area-sagas" hint="bitácora del orquestador · clic para ver pasos">
      {error && <p className="error-text">{error}</p>}
      {sagas?.length === 0 && <p className="empty">Aún no hay sagas en este ambiente.</p>}
      <div style={{ margin: "-16px" }}>
        {sagas?.map((s) => {
          const st = SAGA_STATUS[s.status] ?? { label: s.status, tone: "neutral" as const, icon: "?" };
          const expanded = open === s.sagaId;
          return (
            <div className="saga" key={s.sagaId}>
              <button
                type="button"
                className="saga-row"
                aria-expanded={expanded}
                onClick={() => setOpen(expanded ? null : s.sagaId)}
              >
                <span className="mono">{shortId(s.sagaId)}</span>
                <Chip tone={st.tone} icon={st.icon}>
                  {st.label}
                </Chip>
                <span className="saga-route muted">
                  {s.stationId}/{s.connectorId} · {s.fleetId} ·{" "}
                  {s.status === "FINISHED" ? `cobrado ${money(s.capturedCents)}` : money(s.amountCents)}
                  {s.failureReason && <span className="mono faint"> · {s.failureReason}</span>}
                </span>
                <span className="saga-when faint">{timeAgo(s.createdAt)}</span>
              </button>
              {expanded && <SagaDetail sagaId={s.sagaId} />}
            </div>
          );
        })}
      </div>
    </Panel>
  );
}
