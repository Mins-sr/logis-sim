import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  root: "source",
  resolve: { preserveSymlinks: true },
  cacheDir: "../.cache/vite",
  build: {
    outDir: process.env.TOF_LOGIS_VITE_OUT_DIR ?? "../dist",
    emptyOutDir: true,
  },
  server: { port: 5173 },
});
