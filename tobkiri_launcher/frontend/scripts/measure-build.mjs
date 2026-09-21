import {gzipSync} from 'node:zlib';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {dirname, resolve} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';

import {canonicalBuildBytes} from './canonical-build-output.mjs';

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(SCRIPT_DIR, '..');
const DEFAULT_DIST_DIR = resolve(FRONTEND_ROOT, 'dist');
const STATIC_ROUTE_SOURCES = [
  'src/pages/Setup.tsx',
  'src/pages/Dashboard.tsx',
];

function comparePaths(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

function sortedUnique(values) {
  return [...new Set(values)].sort(comparePaths);
}

/**
 * Keep build metrics mechanically aligned with the lazy route registry. The
 * registry is TypeScript and is intentionally parsed here rather than
 * maintaining a second hand-written list that can drift from the app shell.
 */
async function lazyRouteSources() {
  const source = await readFile(resolve(FRONTEND_ROOT, 'src/lib/routeModules.ts'), 'utf8');
  const block = /export const routeModuleSources:[^{]+\{([\s\S]*?)\n\};/.exec(source)?.[1];
  if (!block) throw new Error('routeModuleSources registry is missing');
  const sources = [...block.matchAll(/:\s*'([^']+)'/g)].map((match) => match[1]);
  if (sources.length === 0 || new Set(sources).size !== sources.length) {
    throw new Error('routeModuleSources registry is empty or duplicated');
  }
  return sources;
}

async function fileMetric(distDir, relativePath) {
  const path = resolve(distDir, relativePath);
  const data = await readFile(path);
  return {file: relativePath, raw_bytes: data.byteLength, gzip_bytes: gzipSync(data).byteLength};
}

function manifestKey(manifest, source) {
  if (manifest[source]) return source;
  return [...Object.keys(manifest)].sort(comparePaths).find((key) => key.endsWith(source)) ?? null;
}

function collectStaticClosure(manifest, startKeys) {
  const keys = new Set();
  const visit = (key) => {
    if (!key || keys.has(key) || !manifest[key]) return;
    keys.add(key);
    for (const imported of [...(manifest[key].imports ?? [])].sort(comparePaths)) visit(imported);
  };
  for (const key of [...startKeys].sort(comparePaths)) visit(key);
  return [...keys].sort(comparePaths);
}

function sumMetrics(files) {
  const sortedFiles = [...files].sort((left, right) => comparePaths(left.file, right.file));
  return {
    files: sortedFiles,
    raw_bytes: sortedFiles.reduce((sum, item) => sum + item.raw_bytes, 0),
    gzip_bytes: sortedFiles.reduce((sum, item) => sum + item.gzip_bytes, 0),
  };
}

export async function measureBuild({distDir = DEFAULT_DIST_DIR, baselinePath} = {}) {
  const manifestPath = resolve(distDir, 'manifest.json');
  const manifestBytes = await readFile(manifestPath);
  const canonicalManifestBytes = canonicalBuildBytes('manifest.json', manifestBytes);
  if (!manifestBytes.equals(canonicalManifestBytes)) await writeFile(manifestPath, canonicalManifestBytes);
  const manifest = JSON.parse(canonicalManifestBytes.toString('utf8'));
  const entryKey = [...Object.keys(manifest)].sort(comparePaths).find((key) => manifest[key].isEntry)
    ?? manifestKey(manifest, 'src/main.tsx');
  if (!entryKey) throw new Error('Vite manifest does not contain an application entry');

  const initialKeys = collectStaticClosure(manifest, [entryKey]);
  const initialJsFiles = new Set();
  const initialCssFiles = new Set();
  for (const key of initialKeys) {
    if (manifest[key].file.endsWith('.js')) initialJsFiles.add(manifest[key].file);
    for (const css of manifest[key].css ?? []) initialCssFiles.add(css);
  }

  const allJs = [...new Set(Object.values(manifest).map((entry) => entry.file).filter((file) => file.endsWith('.js')))];
  const initialJavaScript = await Promise.all(sortedUnique(initialJsFiles).map((file) => fileMetric(distDir, file)));
  const initialCss = await Promise.all(sortedUnique(initialCssFiles).map((file) => fileMetric(distDir, file)));
  const allJavaScript = await Promise.all(sortedUnique(allJs).map((file) => fileMetric(distDir, file)));

  const routes = {};
  const routeSources = [...STATIC_ROUTE_SOURCES, ...(await lazyRouteSources())];
  for (const source of routeSources) {
    const key = manifestKey(manifest, source);
    if (!key) {
      routes[source] = {present: false};
      continue;
    }
    const closure = collectStaticClosure(manifest, [key]);
    const files = sortedUnique(closure.map((item) => manifest[item].file));
    const metrics = await Promise.all(files.map((file) => fileMetric(distDir, file)));
    routes[source] = {present: true, ...sumMetrics(metrics)};
  }

  const report = {
    entry: entryKey,
    initial_javascript: sumMetrics(initialJavaScript),
    initial_css: sumMetrics(initialCss),
    initial_assets: sumMetrics([...initialJavaScript, ...initialCss]),
    all_javascript: sumMetrics(allJavaScript),
    routes,
  };

  if (baselinePath) {
    const baseline = JSON.parse(await readFile(resolve(baselinePath), 'utf8'));
    const previousInitialJs = baseline.initial_javascript ?? baseline.initial;
    report.delta = {
      initial_javascript_raw_bytes: report.initial_javascript.raw_bytes - previousInitialJs.raw_bytes,
      initial_javascript_gzip_bytes: report.initial_javascript.gzip_bytes - previousInitialJs.gzip_bytes,
      all_javascript_raw_bytes: report.all_javascript.raw_bytes - baseline.all_javascript.raw_bytes,
      all_javascript_gzip_bytes: report.all_javascript.gzip_bytes - baseline.all_javascript.gzip_bytes,
    };
  }

  await mkdir(distDir, {recursive: true});
  const outputPath = resolve(distDir, 'build-metrics.json');
  await writeFile(outputPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  console.log(JSON.stringify(report, null, 2));
  return {outputPath, report};
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const baselineArg = process.argv.find((argument) => argument.startsWith('--baseline='));
  const distArg = process.argv.find((argument) => argument.startsWith('--dist='));
  measureBuild({
    distDir: distArg?.slice('--dist='.length) || DEFAULT_DIST_DIR,
    baselinePath: baselineArg?.slice('--baseline='.length),
  }).catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}
