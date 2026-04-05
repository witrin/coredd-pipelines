import { defineConfig } from "@playwright/test";

export default defineConfig({
  testMatch: ["**/*.test.ts"],
  use: {
    browserName: "chromium",
    headless: true,
  },
  webServer: {
    command: "npx vite preview --port 4173 --host 0.0.0.0",
    port: 4173,
    reuseExistingServer: false,
  },
});
