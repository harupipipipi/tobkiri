import { defineConfig } from '@playwright/test';
export default defineConfig({ testDir: '.', testMatch: 'fixture.spec.ts', workers: 1, use: { baseURL: 'http://127.0.0.1:41993', headless: true }, reporter: [['list']], outputDir: '../../../browser-fixture-results' });
