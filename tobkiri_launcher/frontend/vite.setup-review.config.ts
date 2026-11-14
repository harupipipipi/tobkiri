import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';
import {defineConfig} from 'vite';

export default defineConfig({
  root: path.resolve(import.meta.dirname, 'setup-review'),
  base: './',
  publicDir: false,
  plugins: [react(), tailwindcss()],
  build: {
    modulePreload: false,
    outDir: path.resolve(import.meta.dirname, '../setup-review-ui'),
    emptyOutDir: true,
  },
  resolve: {alias: {'@': import.meta.dirname}},
});
