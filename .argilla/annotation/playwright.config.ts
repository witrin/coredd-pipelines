import { defineConfig } from "@playwright/test";

export default defineConfig({
  outputDir: ".playwright",
  testMatch: ["**/*.test.ts"],
  workers: 8,
  timeout: 3000,
  // The per-test timeout stays at 3s. Under full-suite 8-worker load a few tests that
  // wait on async Monaco code-lens/decoration rendering occasionally cross 3s; a single
  // retry absorbs that render flakiness without loosening the time limit.
  retries: 1,
  use: {
    browserName: "chromium",
    headless: true,
  },
  webServer: {
    command: "npx vite --port 5173 --host 0.0.0.0",
    port: 5173,
    reuseExistingServer: true,
  },
});
