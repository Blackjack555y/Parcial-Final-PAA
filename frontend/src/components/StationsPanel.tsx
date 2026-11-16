import type { Station } from "../api/types";
import { useNow } from "../hooks/useLive";
import { CONNECTOR_STATUS, countdown } from "../lib/format";
import { Chip, Panel } from "./ui";

export function StationsPanel({ stations, error }: { stations: Station[] | null; error: string | null }) {
  const total = stations?.reduce((n, s) => n + s.connectors.length, 0) ?? 0;
  const free = stations?.reduce((n, s) => n + s.availableConnectors, 0) ?? 0;
  const now = useNow();
  return (
    <Panel
      title="Electrolineras"
      hint={stations ? `${free}/${total} conectores libres` : undefined}
    >
      {error && <p className="error-text">{error}</p>}
      {!stations && !error && <p className="empty">Cargando estaciones…</p>}
      <div className="stations">
        {stations?.map((station) => (
          <article className="station" key={station.stationId}>
            <div className="station-name">{station.name}</div>
            <div className="station-meta">
              {station.city} · {station.address}
            </div>
            {station.connectors.map((c) => {
              const st = CONNECTOR_STATUS[c.status] ?? { label: c.status, tone: "neutral" as const, icon: "?" };
              return (
                <div className="connector" key={c.connectorId}>
                  <span className="connector-id">{c.connectorId}</span>
                  <span className="connector-spec">
                    {c.type} · {c.powerKw} kW
                    {c.fleetId && <span className="faint"> · {c.fleetId}</span>}
                  </span>
                  <Chip tone={st.tone} icon={st.icon} live={st.live}>
                    {st.label}
                    {c.status === "RESERVED" && c.reservedUntil && (
                      <span title="Bloqueo temporal: se libera solo si no arranca la carga">
                        {" "}· {countdown(c.reservedUntil, now)}
                      </span>
                    )}
                  </Chip>
                </div>
              );
            })}
          </article>
        ))}
      </div>
    </Panel>
  );
}
