import { fileURLToPath } from "node:url";
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: "shared-search.spec.ts",
  workers: 1,
  use: { baseURL: "http://127.0.0.1:48829", headless: true },
  reporter: "list",
  webServer: {
    cwd: fileURLToPath(new URL("..", import.meta.url)),
    command: "./node_modules/.bin/vite --config e2e/shared-search.vite.config.ts",
    url: "http://127.0.0.1:48829/e2e/shared-search.fixture.html",
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
