import { copyFile, mkdir, readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const sourceRoot = fileURLToPath(new URL("../../ui/", import.meta.url));
const targetRoot = fileURLToPath(new URL("../../../defaultspack/ui/search/", import.meta.url));
const files = ["index.html", "search-home.js", "search-home.css"];
const check = process.argv.includes("--check");

if (!check) await mkdir(targetRoot, { recursive: true });
for (const filename of files) {
  const source = join(sourceRoot, filename);
  const target = join(targetRoot, filename);
  const expected = await readFile(source);
  if (filename === "index.html" && !expected.toString().includes('src="/search/search-home.js"')) {
    throw new Error("Search UI must reference assets under /search/.");
  }
  if (check) {
    const actual = await readFile(target);
    if (!expected.equals(actual)) throw new Error(`Search UI copy is stale: ${filename}. Run npm run build.`);
  } else {
    await copyFile(source, target);
  }
}
console.log(check ? "Search UI copies match." : "Search UI copied to the shared Defaults app.");
