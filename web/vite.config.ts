import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Assets are bundled locally and referenced relatively; nothing loads from external hosts.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 1500 },
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000", "/health": "http://127.0.0.1:8000" },
  },
});
