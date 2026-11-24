/** Hash-bind source assets and a supplied real component. Never builds or runs it. */
import { createHash } from 'node:crypto';
import { mkdir, readFile, realpath, stat, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const repo = resolve(root, '../..');
const shape = 'local.native-acceptance-provider.review';
const uiId = 'local.native-acceptance-ui';
const providerId = 'local.native-acceptance-provider';
const uiFiles = ['app.js', 'float.js', 'index.html', 'ports.js', 'style.css'];
const providerFiles = ['component.wasm', 'wit/pack.wit', 'wit/deps/core.wit', 'wit/review.shape.json'];
const digest = (bytes) => `sha256:${createHash('sha256').update(bytes).digest('hex')}`;
const ordered = (value) => Array.isArray(value) ? value.map(ordered)
  : value !== null && typeof value === 'object'
    ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, ordered(value[key])])) : value;
const canonical = (value) => Buffer.from(JSON.stringify(ordered(value)));
const writeJson = (path, value) => writeFile(path, `${JSON.stringify(ordered(value), null, 2)}\n`);

const permissions = ({ ui = false, global = false } = {}) => ({
  network: { origins: [] }, fs: [], proc: [], pty: false, secrets: [],
  storage: { private: false, public_read: false, public_write: false },
  native: false, cross_profile_storage: global, audit_read: false, audit_append: true,
  host_io: [], ports: ui ? [`${shape}@1.0`] : [], mediator: false,
  wasm: { fuel_per_call: 100000000, memory_mib: 64, wall_ms: 300000,
    table_elements: 16384, output_bytes: 65536 },
  loops: { max_iterations: 4, max_wall_ms: 300000, max_cost_units: 10000 },
});
const declaration = { shape, version: '1.0', optional: false };
const baseManifest = (id, runtime, limits) => ({
  format: 'tobkiri.pack/1', id, version: '0.1.0', publisher_key: null, runtime,
  shapes: [], ports: { provides: [], consumes: [], untyped: [] },
  flows: [], overlays: [], stores: [], permissions: limits, ui: null, files: [],
});
async function bindFiles(directory, manifest, files) {
  for (const path of [...files].sort()) {
    const bytes = await readFile(join(directory, path));
    manifest.files.push({ path, size: bytes.length, sha256: digest(bytes) });
  }
  await writeJson(join(directory, 'pack.json'), manifest);
  return digest(canonical(manifest));
}
async function requireSourceCopies() {
  for (const [copy, original] of [
    ['ui/ports.js', 'sdk/js/pack-ui/index.js'],
    ['ui/float.js', 'sdk/js/pack-ui/float.js'],
    ['provider/wit/deps/core.wit', 'crates/tobkiri-wit/wit/core/core.wit'],
  ]) {
    if (!(await readFile(join(root, copy))).equals(await readFile(join(repo, original)))) {
      throw new Error(`Reviewed source copy differs from ${original}; reconcile it before staging.`);
    }
  }
}
function uiManifest() {
  const manifest = baseManifest(uiId, { kind: 'data' }, permissions({ ui: true }));
  manifest.ports.consumes = [declaration];
  manifest.ui = { entry: 'index.html', ports_used: [`${shape}@1.0`] };
  return manifest;
}

const args = process.argv.slice(2);
await requireSourceCopies();
if (args.length === 1 && args[0] === '--ui-manifest-only') {
  const hash = await bindFiles(join(root, 'ui'), uiManifest(), uiFiles);
  console.log(JSON.stringify({ ui_manifest: hash, component: 'not-built', native_execution: 'not-run' }));
} else {
  if (args.length !== 4 || args[0] !== '--component' || args[2] !== '--output') {
    throw new Error('Usage: node stage.mjs --ui-manifest-only | --component REAL.wasm --output NEW_DIRECTORY');
  }
  const component = await realpath(args[1]);
  const componentStat = await stat(component);
  if (!componentStat.isFile() || componentStat.size > 32 * 1024 * 1024) {
    throw new Error('The supplied component must be a regular file of at most 32 MiB.');
  }
  const bytes = await readFile(component);
  if (!bytes.subarray(0, 8).equals(Buffer.from([0, 97, 115, 109, 13, 0, 1, 0]))) {
    throw new Error('The supplied artifact is not a WebAssembly component.');
  }
  const sources = {};
  const sourceBytes = new Map();
  for (const path of ['stage.mjs', 'provider/Cargo.toml', 'provider/Cargo.lock', 'provider/src/lib.rs',
    'provider/wit/pack.wit', 'provider/wit/deps/core.wit', 'provider/wit/review.shape.json',
    ...uiFiles.map((file) => `ui/${file}`)]) {
    const content = await readFile(join(root, path));
    sources[path] = digest(content);
    sourceBytes.set(path, content);
  }
  // A new directory only: staging cannot replace a running/previous fixture,
  // Profile, DataRoot, receipt or recovery artifact. All required inputs,
  // including the reviewed isolated lock, were read before this mutation.
  const output = resolve(args[3]);
  await mkdir(output);
  const ui = join(output, uiId);
  const provider = join(output, providerId);
  await mkdir(ui);
  await mkdir(join(provider, 'wit/deps'), { recursive: true });
  for (const file of uiFiles) await writeFile(join(ui, file), sourceBytes.get(`ui/${file}`));
  await writeFile(join(provider, 'component.wasm'), bytes);
  for (const file of providerFiles.filter((file) => file !== 'component.wasm')) {
    await writeFile(join(provider, file), sourceBytes.get(`provider/${file}`));
  }
  const providerManifest = baseManifest(providerId, {
    kind: 'wasm', component: 'component.wasm',
    toolchain: 'rust/1.94.0;wit-bindgen/0.62.0', config: null,
  }, permissions({ global: true }));
  providerManifest.shapes = [{ name: shape, version: '1.0', kind: 'port', wit: 'wit/pack.wit' }];
  providerManifest.ports.provides = [declaration];
  const uiDocument = uiManifest();
  const providerHash = await bindFiles(provider, providerManifest, providerFiles);
  const uiHash = await bindFiles(ui, uiDocument, uiFiles);
  const profile = {
    format: 'tobkiri.profile/1', id: 'prof_native_acceptance', name: 'Tobkiri native acceptance',
    packs: [
      { id: providerId, version: '0.1.0', sha256: providerHash, source: `file:${provider}` },
      { id: uiId, version: '0.1.0', sha256: uiHash, source: `file:${ui}` },
    ],
    publishers: {}, ceiling: permissions({ ui: true, global: true }),
    pack_limits: { [providerId]: providerManifest.permissions, [uiId]: uiDocument.permissions },
    bindings: { [`${shape}@1`]: providerId }, overlay_order: [], user_overlays: [],
    startup: { inputs: [], ui: uiId },
  };
  const profilePath = join(output, 'native-acceptance.profile.json');
  await writeJson(profilePath, profile);
  await writeJson(join(output, 'stage-receipt.json'), {
    format: 'tobkiri.native-acceptance-stage/1', sources,
    component: { sha256: digest(bytes), bytes: bytes.length,
      status: 'supplied-artifact; compilation-and-exact-ABI-verification-pending' },
    packs: { [providerId]: providerHash, [uiId]: uiHash },
    profile_document_sha256: digest(await readFile(profilePath)),
    native_execution: 'not-run', native_sheet_presentation: 'not-observed',
  });
  console.log(JSON.stringify({ output, profile: profilePath, component_bytes: bytes.length,
    provider_manifest: providerHash, ui_manifest: uiHash, native_execution: 'not-run' }));
}
