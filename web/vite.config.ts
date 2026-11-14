import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The UI is a pure client of the public API. In dev and preview, Vite forwards /api (including
// the workspace terminal WebSocket) and the health probes to the backend, so the browser talks
// to one origin and the API needs no CORS configuration.
const target = process.env.AGENTOPS_API_URL ?? "http://localhost:8000";
const proxy = {
  "/api": { target, changeOrigin: true, ws: true },
  "/health": { target, changeOrigin: true },
  "/ready": { target, changeOrigin: true },
};

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy },
  preview: { port: 4173, proxy },
  test: { environment: "node" },
});
