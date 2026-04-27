import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8601";

export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.VITE_DEV_PORT ?? 5173),
    host: "0.0.0.0",
    allowedHosts: ["radio.public-air.net"],
    // Optimierungen für bessere Performance
    hmr: {
      overlay: true,
      host: "radio.public-air.net",
      protocol: "wss",
      clientPort: 443,
      path: "/__vite_hmr",
    },
    // Pre-bundling optimieren
    optimizeDeps: {
      include: ["react", "react-dom", "axios"],
      exclude: [],
    },
    proxy: {
      "/api": {
        target: apiProxyTarget,
        changeOrigin: true,
        timeout: 30000,
      },
      "/docs": {
        target: apiProxyTarget,
        changeOrigin: true,
        timeout: 30000,
      },
      "/openapi.json": {
        target: apiProxyTarget,
        changeOrigin: true,
        timeout: 30000,
      },
      "/terminal": {
        target: apiProxyTarget,
        changeOrigin: true,
        timeout: 30000,
      },
      "/terminal/": {
        target: apiProxyTarget,
        changeOrigin: true,
        timeout: 30000,
      }
    }
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    emptyOutDir: true,
    // Code-Splitting optimieren
    rollupOptions: {
      output: {
        manualChunks: {
          "react-vendor": ["react", "react-dom"],
          "chart-vendor": ["recharts"],
        },
      },
    },
    // Chunk-Größe erhöhen (warnt bei >500KB)
    chunkSizeWarningLimit: 1000,
  },
});
