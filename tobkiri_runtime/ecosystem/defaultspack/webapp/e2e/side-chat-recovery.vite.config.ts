import path from "node:path";
import { defineConfig, mergeConfig } from "vite";
import application from "../vite.config";

// Keep the component fixture's development cache inside its own checkout,
// including when node_modules is a shared read-only dependency symlink.
export default defineConfig(mergeConfig(application, {
  cacheDir: path.resolve(import.meta.dirname, "../.side-chat-recovery-vite-cache"),
}));
