import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// In production nginx serves the SPA and the ingress sends /v1 and /api to the
// gateway on the same origin. Locally we recreate that with the dev proxy so
// cookies and the CSRF header behave exactly as they do in the cluster.
const gateway = "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": { target: gateway, changeOrigin: false },
      "/api": { target: gateway, changeOrigin: false },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
