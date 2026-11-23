import { defineConfig } from "vite";

export default defineConfig({
  build: {
    target: "es2022",
    sourcemap: false,
    rolldownOptions: { input: { dashboard: "index.html", splash: "splash.html" } },
  },
  server: {
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
});
