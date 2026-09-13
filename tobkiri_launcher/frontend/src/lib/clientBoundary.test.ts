import assert from 'node:assert/strict';
import {readFileSync, existsSync} from 'node:fs';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {afterEach, beforeEach, test} from 'node:test';
import ts from 'typescript';
import {hostApiFetch} from './hostClient';
import {defaultspackApiFetch} from './defaultspackClient';
import {clearApiPrefetchCache} from './apiTransport';
import {setRuntimeDispatchStatus} from './runtimeDispatchGate';

const originalFetch = globalThis.fetch;
const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
let requests: Array<{path: string; init?: RequestInit}> = [];
beforeEach(() => {
  requests = [];
  clearApiPrefetchCache();
  setRuntimeDispatchStatus('runtime_ready');
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {location: {href: 'http://127.0.0.1:8765/panel'}},
  });
  globalThis.fetch = async (input, init) => {
    requests.push({path: String(input), init});
    return new Response(JSON.stringify({success: true, data: {available: true}}));
  };
});
afterEach(() => {
  globalThis.fetch = originalFetch;
  clearApiPrefetchCache();
  if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow);
  else Reflect.deleteProperty(globalThis, 'window');
});

const dashboard = '/api/contracts/defaultspack/GET%20%2Fapi%2Fhome%2Fdashboard';

test('Host startup routes work while Application dispatch requires reconfirmation', async () => {
  setRuntimeDispatchStatus('profile_reconfirmation_required');
  for (const path of ['/health', '/api/v4/profiles', '/api/setup/packs']) {
    assert.deepEqual(await hostApiFetch(path), {available: true});
  }
  await assert.rejects(defaultspackApiFetch(dashboard), /Profile reconfirmation/);
  assert.equal(requests.length, 3);
});

test('Host and Application clients cannot dispatch each other’s routes', async () => {
  await assert.rejects(hostApiFetch(dashboard), /exact method\/path allowlist/);
  for (const path of ['/health', '/api/v4/profiles', '/api/v4/packvm/doctor']) {
    await assert.rejects(defaultspackApiFetch(path), /exact method\/path allowlist/);
  }
  for (const client of [hostApiFetch, defaultspackApiFetch]) {
    for (const path of ['https://example.invalid/api', '//example.invalid/api', '/api/unknown']) {
      await assert.rejects(client(path), /exact method\/path allowlist/);
    }
  }
  assert.deepEqual(requests, []);
});

test('Application operation identity and exact HTTP method survive transport separation', async () => {
  await assert.rejects(defaultspackApiFetch(dashboard, {method: 'POST'}), /exact method\/path allowlist/);
  assert.deepEqual(await defaultspackApiFetch(dashboard), {available: true});
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, dashboard);
  assert.ok(new Headers(requests[0].init?.headers).get('X-Tobkiri-Request-ID'));
  assert.equal(requests[0].init?.credentials, 'same-origin');
});

test('Host dependency graph does not load the optional Defaultspack generated client', () => {
  const libraryRoot = dirname(fileURLToPath(import.meta.url));
  const visited = new Set<string>();
  const visit = (path: string) => {
    if (visited.has(path)) return;
    visited.add(path);
    assert.doesNotMatch(path, /\/(api|defaultspackClient|generatedFrontendContractMap)\.ts$/);
    const source = ts.createSourceFile(path, readFileSync(path, 'utf8'), ts.ScriptTarget.Latest, true);
    for (const node of source.statements) {
      if (!ts.isImportDeclaration(node) || node.importClause?.isTypeOnly
        || !ts.isStringLiteral(node.moduleSpecifier)) continue;
      const clause = node.importClause;
      if (clause && !clause.name && clause.namedBindings && ts.isNamedImports(clause.namedBindings)
        && clause.namedBindings.elements.every((element) => element.isTypeOnly)) continue;
      const specifier = node.moduleSpecifier.text;
      if (!specifier.startsWith('.')) continue;
      const imported = resolve(dirname(path), `${specifier}.ts`);
      assert.ok(existsSync(imported), `Unresolved Host import: ${specifier}`);
      visit(imported);
    }
  };
  visit(resolve(libraryRoot, 'hostClient.ts'));
  assert.ok(visited.has(resolve(libraryRoot, 'apiTransport.ts')));
  assert.ok(visited.has(resolve(libraryRoot, 'desktopHost.ts')));
});
