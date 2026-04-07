import { defineConfig } from "@playwright/test";

export default defineConfig({
  outputDir: ".playwright",
  testMatch: ["**/*.test.ts"],
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
