/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dashboard talks to the API through a same-origin path (/api), so the session
// cookie (HttpOnly, SameSite=Strict, Path=/api/v1) is first-party and no CORS or
// credentials-mode configuration is needed. In development Vite proxies /api to the
// FastAPI backend; in production put both behind one origin (reverse proxy).
const backend = process.env.APPROVAL_LINE_BACKEND_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: backend, changeOrigin: false } },
  },
  build: { sourcemap: false, outDir: "dist" },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
