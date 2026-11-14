// One compiler invocation per process: preview2-shim preopens are global.
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { componentize } from "@bytecodealliance/componentize-js";
import { _setPreopens, _clearPreopens } from "@bytecodealliance/preview2-shim/filesystem";

const [sourcePath, witDirectory, scratch] = process.argv.slice(2);
try {
  _setPreopens({ "/tobkiri-wit": witDirectory });
  const { component, imports } = await componentize({
    sourcePath, witPath: "/tobkiri-wit", env: false, worldName: "pack",
    disableFeatures: ["stdio", "random", "clocks", "http", "fetch-event"],
  });
  await writeFile(path.join(scratch, "component.wasm"), component);
  await writeFile(path.join(scratch, "imports.json"), JSON.stringify(imports));
} finally {
  _clearPreopens();
}
