/// <reference types="vitest/config" />
// The console app (slice 9.5a), served by the Hub at /app/. `make console-dev` runs
// Vite's dev server against a local Hub: the proxy adds the Tailscale identity that
// `tailscale serve` would, in development only; the built app never sends one.
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

declare const process: { env: Record<string, string | undefined> };

const hub = process.env.SQUERO_DEV_HUB ?? "http://127.0.0.1:18100";
const login = process.env.SQUERO_DEV_LOGIN ?? "";
const proxy = {
  target: hub,
  headers: login ? { "Tailscale-User-Login": login } : {},
};

export default defineConfig({
  base: "/app/",
  plugins: [react(), tailwindcss()],
  build: { outDir: "dist", assetsDir: "assets", sourcemap: false },
  server: { host: "127.0.0.1", port: 5173, proxy: { "/api": proxy } },
  test: { environment: "jsdom" },
});
