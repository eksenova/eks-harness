import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const daemon = process.env.EKS_HARNESS_DEV_DAEMON ?? "http://127.0.0.1:7171";

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
    target: "es2022",
    chunkSizeWarningLimit: 1024,
  },
  server: {
    proxy: {
      "/api": { target: daemon, changeOrigin: false },
      "/raw": { target: daemon, changeOrigin: false },
      "/thumb": { target: daemon, changeOrigin: false },
      "/site": { target: daemon, changeOrigin: false },
      "^/s/[^/]+/.+": { target: daemon, changeOrigin: false },
    },
  },
});
