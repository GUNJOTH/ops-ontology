import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: "127.0.0.1",
    proxy: {
      "/api": "http://127.0.0.1:8001",
    },
  },
  resolve: {
    // Avoid Windows realpath probing in restricted runners where child
    // process creation is blocked. This keeps module identity stable here.
    preserveSymlinks: true,
  },
  esbuild: false,
  build: {
    minify: false,
  },
});
