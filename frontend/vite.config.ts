import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// GA_API_PROXY points the dev server at another API, e.g. production, when no local backend runs.
const api = process.env.GA_API_PROXY ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": { target: api, changeOrigin: true } } },
});
