import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../roscope/visualizer/static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/ws": {
        target: "ws://127.0.0.1:8765",
        ws: true,
      },
    },
  },
});
