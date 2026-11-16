import { useCallback, useEffect, useState } from "react";
import { gql } from "./api/client";
import { FLEETS, Q, S } from "./api/queries";
import type { EnvironmentInfo, FeatureFlag, Saga, Station } from "./api/types";
import { FlagsPanel } from "./components/FlagsPanel";
import { Header } from "./components/Header";
import { Loader } from "./components/Loader";
import { ReservePanel } from "./components/ReservePanel";
import { SagaHistory } from "./components/SagaHistory";
import { SessionsPanel } from "./components/SessionsPanel";
import { StationsPanel } from "./components/StationsPanel";
import { WalletPanel } from "./components/WalletPanel";
import { useLive } from "./hooks/useLive";

const THEME_KEY = "aegisvolt.theme";

function initialTheme(): "dark" | "light" {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === "dark" || saved === "light") return saved;
  } catch {
    /* sin almacenamiento */
  }
  return "dark";
}

export function App() {
  const [env, setEnv] = useState<EnvironmentInfo | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [theme, setTheme] = useState(initialTheme);
  const [fleetId, setFleetId] = useState(FLEETS[0]!.id);
  const [version, setVersion] = useState(0); // fuerza el refresco de billetera y sesiones
  const [lastSaga, setLastSaga] = useState<string | null>(null);

  const stations = useLive<Station[], { stations: Station[] }>(S.stations, Q.stations, (d) => d.stations);
  const flags = useLive<FeatureFlag[], { featureFlags: FeatureFlag[] }>(S.flags, Q.flags, (d) => d.featureFlags);
  const sagas = useLive<Saga[], { sagas: Saga[] }>(S.sagas, Q.sagas, (d) => d.sagas);

  // El ambiente lo dice el gateway (fuente de verdad), no la URL de la consola.
  useEffect(() => {
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout>;
    const load = () =>
      gql<{ environment: EnvironmentInfo }>(Q.environment)
        .then((d) => setEnv(d.environment))
        .catch((e: Error) => {
          setBootError(e.message);
          timer = setTimeout(load, Math.min(1000 * 2 ** attempt++, 8000));
        });
    void load();
    return () => clearTimeout(timer);
  }, []);

  useEffect(() => {
    const root = document.documentElement;
    if (env) root.dataset.env = env.name;
    root.dataset.theme = theme;
    document.title = env ? `AegisVolt · ${env.name.toUpperCase()}` : "AegisVolt · Consola";
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch {
      /* sin almacenamiento */
    }
  }, [env, theme]);

  const bump = useCallback(() => setVersion((v) => v + 1), []);

  if (!env) {
    return (
      <div className="app">
        <div className="boot">
          <div className="boot-inner">
            <Loader size={150} label="Conectando con el gateway del ambiente…" />
            {bootError && <span className="error-text">{bootError} · reintentando</span>}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <div className="env-strip" />
      <Header env={env} theme={theme} onToggleTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))} />
      <main className="layout">
        <div className="area-main">
        <StationsPanel stations={stations.data} error={stations.error} />
        <ReservePanel
          stations={stations.data}
          flags={flags.data}
          fleetId={fleetId}
          onFleetChange={setFleetId}
          onSagaFinished={(saga) => {
            setLastSaga(saga.sagaId);
            bump();
            void stations.refresh();
            void sagas.refresh();
          }}
        />
        </div>
        <aside className="area-side">
          <FlagsPanel env={env.name} flags={flags.data} error={flags.error} />
          <WalletPanel fleetId={fleetId} version={version} />
          <SessionsPanel version={version} onChanged={bump} />
        </aside>
        <SagaHistory sagas={sagas.data} error={sagas.error} highlight={lastSaga} />
      </main>
      <footer className="footer">
        AegisVolt · ambiente <strong>{env.name}</strong> · {env.region} · loader por{" "}
        <a href="https://uiverse.io/Juanes200122" target="_blank" rel="noreferrer noopener">
          Juanes200122 (Uiverse.io, MIT)
        </a>
      </footer>
    </div>
  );
}
