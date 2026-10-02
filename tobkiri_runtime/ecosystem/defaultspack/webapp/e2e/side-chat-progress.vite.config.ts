import path from "node:path";
import { defineConfig, mergeConfig } from "vite";
import application from "../vite.config";
export default defineConfig(mergeConfig(application, { cacheDir: path.resolve(import.meta.dirname, "../.side-chat-progress-vite-cache") }));
