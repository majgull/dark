// The one build config. `vitest/config` extends Vite's config with the `test`
// block, so one file describes the dev server, the build and the tests.
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: "dist", emptyOutDir: true },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    // Whole-page renders on a machine busy with other lanes are slow; the
    // default 5 s failed them. A test that measures wall time carries the
    // marker `timing` (src/rules.test.ts enforces both).
    testTimeout: 30_000,
  },
});
