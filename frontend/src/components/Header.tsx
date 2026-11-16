import { useState } from "react";
import { getAdminToken, setAdminToken } from "../api/client";
import type { EnvironmentInfo } from "../api/types";
import { useConnection } from "../hooks/useLive";

const MODE_LABEL = { live: "En vivo · WSS", polling: "Sondeo HTTP", connecting: "Conectando…" };

export function Header({
  env,
  theme,
  onToggleTheme,
}: {
  env: EnvironmentInfo;
  theme: "dark" | "light";
  onToggleTheme: () => void;
}) {
  const mode = useConnection();
  const [editing, setEditing] = useState(false);
  const [token, setToken] = useState(getAdminToken());

  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">⚡</span>
        AegisVolt
      </div>
      <span className="env-badge" title="Ambiente al que está conectada esta consola">
        {env.name}
      </span>
      <span className="mono muted" title="Versión desplegada (hash del código)">
        v{env.version}
      </span>
      <span className="conn" data-mode={mode} role="status">
        <span className="conn-dot" aria-hidden="true" />
        {MODE_LABEL[mode]}
      </span>
      <span className="topbar-spacer" />
      {editing ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setAdminToken(token.trim());
            setEditing(false);
          }}
          style={{ display: "flex", gap: 6 }}
        >
          <input
            className="input"
            type="password"
            autoFocus
            placeholder={`X-Admin-Token de ${env.name}`}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            aria-label="Token de administración"
          />
          <button className="btn btn-sm" type="submit">Guardar</button>
        </form>
      ) : (
        <button className="btn btn-sm" type="button" onClick={() => setEditing(true)}>
          {getAdminToken() ? "🔑 Admin" : "🔒 Token admin"}
        </button>
      )}
      <button
        className="btn btn-sm"
        type="button"
        onClick={onToggleTheme}
        aria-label={`Cambiar a tema ${theme === "dark" ? "claro" : "oscuro"}`}
      >
        {theme === "dark" ? "☀" : "☾"}
      </button>
    </header>
  );
}
