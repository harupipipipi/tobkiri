import {gzipSync} from 'node:zlib';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {dirname, resolve} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(SCRIPT_DIR, '..');
const DEFAULT_DIST_DIR = resolve(FRONTEND_ROOT, 'dist');
const ROUTE_SOURCES = [
  'src/pages/Packs.tsx',
  'src/pages/PackDetail.tsx',
  'src/pages/NodeManager.tsx',
  'src/pages/GraphEditor.tsx',
  'src/pages/ProfileGraphEditor.tsx',
  'src/pages/AiInputInspector.tsx',
  'src/pages/ApiMap.tsx',
  'src/pages/ProfileWorkspace.tsx',
  'src/pages/StartupProfiles.tsx',
  'src/pages/Flows.tsx',
  'src/pages/Settings.tsx',
];

async function fileMetric(distDir, relativePath) {
  const path = resolve(distDir, relativePath);
  const data = await readFile(path);
  return {file: relativePath, raw_bytes: data.byteLength, gzip_bytes: gzipSync(data).byteLength};
}

function manifestKey(manifest, source) {
  if (manifest[source]) return source;
  return Object.keys(manifest).find((key) => key.endsWith(source)) ?? null;
}

function collectStaticClosure(manifest, startKeys) {
  const keys = new Set();
  const visit = (key) => {
    if (!key || keys.has(key) || !manifest[key]) return;
    keys.add(key);
    for (const imported of manifest[key].imports ?? []) visit(imported);
  };
  for (const key of startKeys) visit(key);
  return [...keys];
}

function sumMetrics(files) {
  return {
    files,
    raw_bytes: files.reduce((sum, item) => sum + item.raw_bytes, 0),
    gzip_bytes: files.reduce((sum, item) => sum + item.gzip_bytes, 0),
  };
}

export async function measureBuild({distDir = DEFAULT_DIST_DIR, baselinePath} = {}) {
  const manifest = JSON.parse(await readFile(resolve(distDir, 'manifest.json'), 'utf8'));
  const entryKey = Object.keys(manifest).find((key) => manifest[key].isEntry) ?? manifestKey(manifest, 'src/main.tsx');
  if (!entryKey) throw new Error('Vite manifest does not contain an application entry');

  const initialKeys = collectStaticClosure(manifest, [entryKey]);
  const initialJsFiles = new Set();
  const initialCssFiles = new Set();
  for (const key of initialKeys) {
    if (manifest[key].file.endsWith('.js')) initialJsFiles.add(manifest[key].file);
    for (const css of manifest[key].css ?? []) initialCssFiles.add(css);
  }

  const allJs = [...new Set(Object.values(manifest).map((entry) => entry.file).filter((file) => file.endsWith('.js')))];
  const initialJavaScript = await Promise.all([...initialJsFiles].map((file) => fileMetric(distDir, file)));
  const initialCss = await Promise.all([...initialCssFiles].map((file) => fileMetric(distDir, file)));
  const allJavaScript = await Promise.all(allJs.map((file) => fileMetric(distDir, file)));

  const routes = {};
  for (const source of ROUTE_SOURCES) {
    const key = manifestKey(manifest, source);
    if (!key) {
      routes[source] = {present: false};
      continue;
    }
    const closure = collectStaticClosure(manifest, [key]);
    const files = [...new Set(closure.map((item) => manifest[item].file))];
    const metrics = await Promise.all(files.map((file) => fileMetric(distDir, file)));
    routes[source] = {present: true, ...sumMetrics(metrics)};
  }

  const report = {
    generated_at: new Date().toISOString(),
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
  measureBuild({baselinePath: baselineArg?.slice('--baseline='.length)}).catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}
