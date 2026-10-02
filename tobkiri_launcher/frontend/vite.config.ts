import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { defineConfig } from 'vite';

export default defineConfig({
  base: '/panel/',
  plugins: [react(), tailwindcss()],
  build: {
    manifest: 'manifest.json',
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, '.'),
    },
  },
  server: {
    hmr: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8765',
        changeOrigin: true,
      },
      '/callback': {
        target: 'http://localhost:8765',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://localhost:8765',
        changeOrigin: true,
      },
    },
  },
});
