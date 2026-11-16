/**
 * Cliente GraphQL de la consola.
 *
 * - Queries y mutations por HTTP (POST /graphql).
 * - Suscripciones por WebSocket (graphql-transport-ws) en el MISMO origen: nginx (o el proxy de
 *   Vite en desarrollo) reenvía /graphql al gateway del ambiente. Sin CORS y sin URLs por
 *   ambiente en el código: la misma imagen sirve a dev, test y prod.
 * - Si el WebSocket cae, el estado de conexión pasa a "polling" y los paneles consultan por
 *   HTTP cada pocos segundos hasta que se reconecta.
 */
import { createClient, type Client } from "graphql-ws";

export type ConnectionMode = "connecting" | "live" | "polling";

const ADMIN_TOKEN_KEY = "aegisvolt.adminToken";

export class GraphQLRequestError extends Error {}

export function getAdminToken(): string {
  try {
    return localStorage.getItem(ADMIN_TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setAdminToken(token: string): void {
  try {
    if (token) localStorage.setItem(ADMIN_TOKEN_KEY, token);
    else localStorage.removeItem(ADMIN_TOKEN_KEY);
  } catch {
    /* almacenamiento no disponible: el token vive solo en esta sesión */
  }
}

export async function gql<T>(
  query: string,
  variables?: Record<string, unknown>,
  options: { admin?: boolean } = {},
): Promise<T> {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (options.admin) headers["x-admin-token"] = getAdminToken();
  let response: Response;
  try {
    response = await fetch("/graphql", {
      method: "POST",
      headers,
      body: JSON.stringify({ query, variables }),
    });
  } catch {
    throw new GraphQLRequestError("Sin conexión con el gateway del ambiente");
  }
  const body = (await response.json().catch(() => ({}))) as {
    data?: T;
    errors?: { message: string }[];
  };
  if (body.errors?.length) throw new GraphQLRequestError(body.errors[0]!.message);
  if (!response.ok || body.data === undefined) {
    throw new GraphQLRequestError(`El gateway respondió ${response.status}`);
  }
  return body.data;
}

// --- Estado de la conexión WebSocket (store mínimo para useSyncExternalStore) -------------
let mode: ConnectionMode = "connecting";
const listeners = new Set<() => void>();

function setMode(next: ConnectionMode): void {
  if (next === mode) return;
  mode = next;
  listeners.forEach((l) => l());
}

export const connection = {
  get: (): ConnectionMode => mode,
  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
};

let client: Client | null = null;

export function wsClient(): Client {
  if (client) return client;
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  client = createClient({
    url: `${protocol}://${location.host}/graphql`,
    lazy: false,
    keepAlive: 10_000,
    retryAttempts: Number.POSITIVE_INFINITY,
    shouldRetry: () => true,
    retryWait: async (retries) => {
      await new Promise((r) => setTimeout(r, Math.min(1000 * 2 ** retries, 10_000)));
    },
    on: {
      connected: () => setMode("live"),
      closed: () => setMode("polling"),
      error: () => setMode("polling"),
    },
  });
  return client;
}
