import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import cesium from "vite-plugin-cesium";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), cesium(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/tiles": "http://localhost:8000",
    },
  },
  optimizeDeps: {
    include: ["cesium"],
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          cesium: ["cesium"],
          recharts: ["recharts"],
        },
      },
    },
  },
});
