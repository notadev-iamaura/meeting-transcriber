import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  root: "ui/web-src",
  base: "/app-assets/",
  plugins: [react()],
  build: {
    outDir: "../web-dist",
    emptyOutDir: true,
  },
  server: {
    host: "127.0.0.1",
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test.setup.ts",
  },
});
