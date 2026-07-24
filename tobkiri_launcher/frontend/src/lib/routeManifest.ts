export interface ViteManifestEntry {
  file: string;
  css?: string[];
  imports?: string[];
  dynamicImports?: string[];
  isEntry?: boolean;
  src?: string;
}

export type ViteManifest = Record<string, ViteManifestEntry>;

function findManifestKey(manifest: ViteManifest, source: string): string | null {
  if (manifest[source]) return source;
  return Object.keys(manifest).find((key) => key.endsWith(source)) ?? null;
}

export function collectManifestAssets(
  manifest: ViteManifest,
  sources: Iterable<string>,
): {scripts: string[]; styles: string[]} {
  const scripts = new Set<string>();
  const styles = new Set<string>();
  const visited = new Set<string>();

  const visit = (key: string) => {
    if (visited.has(key)) return;
    visited.add(key);
    const entry = manifest[key];
    if (!entry) return;
    if (entry.file.endsWith('.js')) scripts.add(entry.file);
    for (const cssFile of entry.css ?? []) styles.add(cssFile);
    for (const importedKey of entry.imports ?? []) visit(importedKey);
  };

  for (const source of sources) {
    const key = findManifestKey(manifest, source);
    if (key) visit(key);
  }

  return {
    scripts: [...scripts],
    styles: [...styles],
  };
}
