import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const uiDir = path.resolve(here, "../../ui");

for (const name of fs.readdirSync(uiDir)) {
  if (/^shell-.*\.(?:js|css)$/.test(name)) {
    fs.rmSync(path.join(uiDir, name));
  }
}
