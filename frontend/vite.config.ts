/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// En desarrollo, /graphql (HTTP y WebSocket) se reenvía al gateway local.
// En producción lo hace nginx: el navegador SIEMPRE habla con su mismo origen (sin CORS).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/graphql": {
        target: process.env.GATEWAY_URL ?? "http://127.0.0.1:8000",
        ws: true,
        changeOrigin: true,
      },
    },
  },
  // assetsInlineLimit 0: nada se incrusta como data: URI (la CSP solo permite fuentes 'self').
  build: { sourcemap: false, chunkSizeWarningLimit: 600, assetsInlineLimit: 0 },
  test: { environment: "node" },
});
