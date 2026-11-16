import { useState } from "react";
import { getAdminToken, gql } from "../api/client";
import { M } from "../api/queries";
import type { EnvName, FeatureFlag } from "../api/types";
import { timeAgo } from "../lib/format";
import { Panel } from "./ui";

const LABEL: Record<string, string> = {
  ENABLE_DYNAMIC_SURGE_PRICING: "Tarifa dinámica en hora pico",
  ENABLE_FAST_CHARGE_BOOST: "Ultra-carga 350 kW",
  SIMULATE_CHARGER_HARDWARE_FAULT: "Simular falla del cargador",
};

export function FlagsPanel({ env, flags, error }: { env: EnvName; flags: FeatureFlag[] | null; error: string | null }) {
  const [pending, setPending] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function toggle(flag: FeatureFlag) {
    if (!getAdminToken()) {
      setMessage("Configura el token admin (arriba a la derecha) para cambiar flags.");
      return;
    }
    setPending(flag.name);
    setMessage(null);
    try {
      await gql(M.setFlag, { n: flag.name, e: !flag.enabled }, { admin: true });
      // No se actualiza el estado local: el cambio llega por la suscripción WSS, igual que
      // lo vería cualquier otra consola abierta en este ambiente.
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setPending(null);
    }
  }

  return (
    <Panel title={`Feature flags · ${env}`} hint="en vivo, sin redesplegar">
      {error && <p className="error-text">{error}</p>}
      {!flags && !error && <p className="empty">Cargando flags…</p>}
      {flags?.map((flag) => {
        const danger = flag.name === "SIMULATE_CHARGER_HARDWARE_FAULT";
        return (
          <div className="flag" key={flag.name}>
            <span className="flag-name">{LABEL[flag.name] ?? flag.name}</span>
            <span className="flag-desc">{flag.name}</span>
            <span className="flag-meta">
              {flag.updatedAt ? `${flag.updatedBy} · ${timeAgo(flag.updatedAt)}` : flag.updatedBy}
            </span>
            <button
              type="button"
              className="switch"
              role="switch"
              aria-checked={flag.enabled}
              aria-label={`${LABEL[flag.name] ?? flag.name}: ${flag.enabled ? "activada" : "desactivada"}`}
              data-danger={danger}
              disabled={pending === flag.name}
              onClick={() => void toggle(flag)}
            />
          </div>
        );
      })}
      {message && <p className="error-text">{message}</p>}
    </Panel>
  );
}
