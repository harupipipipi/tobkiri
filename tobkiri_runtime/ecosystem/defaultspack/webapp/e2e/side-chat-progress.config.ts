import { defineConfig } from "@playwright/test";
import path from "node:path";
const port = Number(process.env.PLAYWRIGHT_PORT ?? 39191);
export default defineConfig({
  testDir: ".", testMatch: "side-chat-progress.spec.ts", workers: 1, timeout: 30000, retries: 0, reporter: "list",
  use: { baseURL: `http://127.0.0.1:${port}`, browserName: "chromium", headless: true, video: "off", screenshot: "only-on-failure" },
  webServer: { command: `./node_modules/.bin/vite --config e2e/side-chat-progress.vite.config.ts --host 127.0.0.1 --port ${port} --base /`,
    cwd: path.resolve(import.meta.dirname, ".."), url: `http://127.0.0.1:${port}`, reuseExistingServer: false, timeout: 30000 },
});
