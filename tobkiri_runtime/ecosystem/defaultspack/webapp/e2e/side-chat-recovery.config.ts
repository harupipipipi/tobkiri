import { defineConfig } from "@playwright/test";
import path from "node:path";

const port = Number(process.env.PLAYWRIGHT_PORT ?? 39189);
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH?.trim() || undefined;

export default defineConfig({
  testDir: ".", testMatch: "side-chat-recovery.spec.ts", workers: 1,
  timeout: 30_000, retries: 0, reporter: "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`, browserName: "chromium", headless: true,
    launchOptions: executablePath ? { executablePath } : undefined,
    video: "off", screenshot: "only-on-failure",
  },
  webServer: {
    command: `./node_modules/.bin/vite --config e2e/side-chat-recovery.vite.config.ts --host 127.0.0.1 --port ${port} --base /`,
    cwd: path.resolve(import.meta.dirname, ".."),
    url: `http://127.0.0.1:${port}`, reuseExistingServer: false, timeout: 30_000,
  },
});
