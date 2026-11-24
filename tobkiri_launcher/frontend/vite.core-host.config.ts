import { defineConfig } from 'vite';
import path from 'node:path';

export default defineConfig({
  root: 'core-host',
  base: './',
  build: { outDir: '../../core-host-ui', emptyOutDir: true },
  resolve: { alias: { '/src': path.resolve(import.meta.dirname, 'src') } },
});
