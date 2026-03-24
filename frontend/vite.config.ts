import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import cesium from "vite-plugin-cesium";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), cesium(), tailwindcss()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      "/api": process.env.API_HOST ?? "http://localhost:8000",
      "/tiles": process.env.API_HOST ?? "http://localhost:8000",
    },
  },
  appType: "mpa",
  optimizeDeps: {
    include: ["cesium"],
  },
  build: {
    rollupOptions: {
      input: {
        main: "index.html",
        vworld: "vworld.html",
      },
      output: {
        manualChunks: {
          cesium: ["cesium"],
          recharts: ["recharts"],
        },
      },
    },
  },
});
