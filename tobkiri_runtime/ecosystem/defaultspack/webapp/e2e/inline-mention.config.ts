import { defineConfig } from '@playwright/test';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  testDir: '.',
  testMatch: ['shared-search.spec.ts', 'inline-mention-consumers.spec.ts'],
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:48839', headless: true },
  reporter: 'list',
  webServer: {
    command: './node_modules/.bin/vite --config e2e/shared-search.vite.config.ts --port 48839 --strictPort',
    cwd: fileURLToPath(new URL('..', import.meta.url)),
    url: 'http://127.0.0.1:48839/e2e/shared-search.fixture.html',
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
