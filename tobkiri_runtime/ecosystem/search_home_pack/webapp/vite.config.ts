import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/search/",
  plugins: [react()],
  build: {
    outDir: "../ui",
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        manualChunks: undefined,
        entryFileNames: "search-home.js",
        chunkFileNames: "search-home.js",
        assetFileNames: (assetInfo) =>
          assetInfo.name?.endsWith(".css") ? "search-home.css" : "assets/[name][extname]",
      },
    },
  },
});
