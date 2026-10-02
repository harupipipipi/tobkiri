const TEXT_EXTENSIONS = new Set([".css", ".html", ".js", ".json", ".map", ".svg"]);
const PATH_KEYS = new Set(["assets", "css", "dynamicImports", "file", "imports", "src"]);

function compareNames(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

function sortJsonKeys(value, parentKey = "") {
  if (Array.isArray(value)) return value.map((item) => sortJsonKeys(item, parentKey));
  if (typeof value === "string") {
    return PATH_KEYS.has(parentKey) ? value.replaceAll("\\", "/") : value;
  }
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.keys(value)
      .map((key) => [key.replaceAll("\\", "/"), key])
      .sort(([left], [right]) => compareNames(left, right))
      .map(([canonicalKey, key]) => [canonicalKey, sortJsonKeys(value[key], canonicalKey)]),
  );
}

export function canonicalBuildBytes(relativePath, bytes) {
  const extension = relativePath.slice(relativePath.lastIndexOf("."));
  if (!TEXT_EXTENSIONS.has(extension)) return bytes;

  const text = bytes.toString("utf8").replace(/\r\n?/g, "\n");
  if (relativePath === "manifest.json") {
    return Buffer.from(`${JSON.stringify(sortJsonKeys(JSON.parse(text)), null, 2)}\n`, "utf8");
  }
  return Buffer.from(text, "utf8");
}
