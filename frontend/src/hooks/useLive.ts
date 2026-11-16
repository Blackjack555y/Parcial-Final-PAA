import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { connection, gql, wsClient } from "../api/client";

/** Estado de la conexión WebSocket compartido por toda la app. */
export function useConnection() {
  return useSyncExternalStore(connection.subscribe, connection.get);
}

/**
 * Datos en vivo: suscripción GraphQL por WebSocket; si el socket no está conectado,
 * sondeo HTTP cada `pollMs` con la query equivalente. Al reconectar, vuelve a la suscripción.
 */
export function useLive<T, R>(
  subscription: string,
  query: string,
  select: (data: R) => T,
  pollMs = 3000,
) {
  const mode = useConnection();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const selectRef = useRef(select);
  selectRef.current = select;

  const refresh = useCallback(async () => {
    try {
      setData(selectRef.current(await gql<R>(query)));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [query]);

  // Suscripción WSS
  useEffect(() => {
    const dispose = wsClient().subscribe<R>(
      { query: subscription },
      {
        next: (result) => {
          if (result.data) {
            setData(selectRef.current(result.data as R));
            setError(null);
          }
        },
        error: () => undefined, // el cliente reintenta solo; mientras tanto, sondeo
        complete: () => undefined,
      },
    );
    return dispose;
  }, [subscription]);

  // Sondeo de respaldo (y primera carga inmediata)
  const hasData = data !== null;
  useEffect(() => {
    if (mode === "live" && hasData) return;
    void refresh();
    if (mode === "live") return;
    const timer = setInterval(() => void refresh(), pollMs);
    return () => clearInterval(timer);
  }, [mode, hasData, pollMs, refresh]);

  return { data, error, refresh, mode };
}

/** Consulta HTTP periódica (para datos sin suscripción: billetera, sesiones, telemetría). */
export function usePolling<T>(
  fn: () => Promise<T>,
  deps: unknown[],
  intervalMs = 3000,
  enabled = true,
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const refresh = useCallback(async () => {
    try {
      setData(await fnRef.current());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    setData(null);
    void refresh();
    const timer = setInterval(() => void refresh(), intervalMs);
    return () => clearInterval(timer);
  }, [enabled, intervalMs, refresh, ...deps]);

  return { data, error, refresh };
}

/** Hora actual que se actualiza cada `ms` (para cuentas regresivas). */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(timer);
  }, [ms]);
  return now;
}
