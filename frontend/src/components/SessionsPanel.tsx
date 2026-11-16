import { useState } from "react";
import { gql } from "../api/client";
import { M, Q } from "../api/queries";
import type { ChargingSession, Telemetry } from "../api/types";
import { usePolling } from "../hooks/useLive";
import { Panel } from "./ui";

function SessionCard({ session, onStopped }: { session: ChargingSession; onStopped: () => void }) {
  const [stopping, setStopping] = useState(false);
  const { data: t } = usePolling(
    () => gql<{ telemetry: Telemetry }>(Q.telemetry, { id: session.sagaId }).then((d) => d.telemetry),
    [session.sagaId],
    2000,
  );
  return (
    <div className="session">
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
        <strong>
          {session.stationId} / {session.connectorId}
        </strong>
        <button
          className="btn btn-sm btn-danger"
          type="button"
          disabled={stopping}
          onClick={() => {
            setStopping(true);
            void gql(M.stop, { id: session.sagaId }).finally(onStopped);
          }}
        >
          ■ Detener
        </button>
      </div>
      <div className="metrics">
        <div>
          <div className="metric-value">{t ? t.powerKw.toFixed(1) : "—"}</div>
          <div className="metric-label">kW</div>
        </div>
        <div>
          <div className="metric-value">{t ? t.energyKwh.toFixed(2) : "—"}</div>
          <div className="metric-label">kWh entregados</div>
        </div>
        <div>
          <div className="metric-value">{t ? `${Math.round(t.elapsedS)} s` : "—"}</div>
          <div className="metric-label">tiempo</div>
        </div>
      </div>
      <div className="soc" role="progressbar" aria-valuenow={t?.socPercent ?? 0} aria-valuemin={0} aria-valuemax={100} aria-label="Estado de carga de la batería">
        <span style={{ width: `${t?.socPercent ?? 0}%` }} />
      </div>
      <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
        Batería {t ? `${t.socPercent.toFixed(1)} %` : "—"}
      </div>
    </div>
  );
}

export function SessionsPanel({ version, onChanged }: { version: number; onChanged: () => void }) {
  const { data, refresh } = usePolling(
    () => gql<{ chargingSessions: ChargingSession[] }>(Q.sessions).then((d) => d.chargingSessions),
    [version],
    3000,
  );
  return (
    <Panel title="Cargas activas" hint={data ? `${data.length}` : undefined}>
      {data?.length === 0 && <p className="empty">No hay cargadores entregando energía.</p>}
      {data?.map((s) => (
        <SessionCard
          key={s.sagaId}
          session={s}
          onStopped={() => {
            void refresh();
            onChanged();
          }}
        />
      ))}
    </Panel>
  );
}
