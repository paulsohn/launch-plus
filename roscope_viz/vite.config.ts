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
      "/api": {
        target: `http://127.0.0.1:${process.env.VITE_API_PORT || "8765"}`,
      },
    },
  },
});
