import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {MemoryRouter} from 'react-router';
import {clearApiPrefetchCache, setRuntimeDispatchStatus} from '@/src/lib/api';
import {useAppStore} from '@/src/store';
import {Setup} from './Setup';

test('changing the additive proposal clears consent and never retains a failed candidate', async () => {
  const dom = new JSDOM('<div id="root"></div>', {url: 'http://localhost/panel/setup'});
  const previousState = useAppStore.getState();
  const descriptors = Object.getOwnPropertyDescriptors(globalThis);
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
    IS_REACT_ACT_ENVIRONMENT: {value: true, configurable: true},
  });
  const fixture = JSON.parse(readFileSync(new URL(
    '../../../../tobkiri_runtime/tobkiri_protocol/fixtures/defaults_setup_v4.canonical.json',
    import.meta.url,
  ), 'utf8'));
  const requests: string[] = [];
  let pending: ((response: Response) => void) | undefined;
  const response = () => new Response(JSON.stringify({success: true, data: fixture}), {
    headers: {'Content-Type': 'application/json'},
  });
  Object.defineProperty(globalThis, 'fetch', {configurable: true, value: async (url: string) => {
    requests.push(String(url));
    if (requests.length === 1) return response();
    return new Promise<Response>((resolve) => { pending = resolve; });
  }});
  clearApiPrefetchCache();
  setRuntimeDispatchStatus('profile_reconfirmation_required');
  useAppStore.setState({runtimeStatus: 'profile_reconfirmation_required'});
  const container = dom.window.document.getElementById('root')!;
  const root = createRoot(container);
  const boxes = () => [...container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
  try {
    await act(async () => { root.render(<MemoryRouter><Setup /></MemoryRouter>); });
    assert.equal(boxes().length, 2);
    await act(async () => { boxes()[1].click(); });
    assert.equal(boxes()[1].checked, true);
    await act(async () => { boxes()[0].click(); });
    assert.equal(requests.at(-1), '/api/setup/packs?include_source_additions=true');
    assert.equal(boxes().length, 1, 'old confirmation is removed during review');
    assert.equal(boxes()[0].disabled, true);
    await act(async () => { pending!(response()); });
    assert.equal(boxes().length, 2);
    assert.equal(boxes()[1].checked, false, 'new proposal needs new consent');
    await act(async () => { boxes()[0].click(); });
    assert.equal(requests.at(-1), '/api/setup/packs');
    await act(async () => {
      pending!(new Response(JSON.stringify({success: false, error: 'review unavailable'}), {status: 409}));
    });
    assert.equal(boxes().length, 1, 'failed review cannot expose previous consent');
    assert.match(container.textContent ?? '', /review unavailable/);
    assert.ok(requests.every((path) => !path.includes('/install')));
  } finally {
    await act(async () => { root.unmount(); });
    useAppStore.setState(previousState, true);
    for (const key of ['window', 'document', 'navigator', 'localStorage', 'fetch', 'IS_REACT_ACT_ENVIRONMENT']) {
      if (descriptors[key]) Object.defineProperty(globalThis, key, descriptors[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
    dom.window.close();
  }
});
