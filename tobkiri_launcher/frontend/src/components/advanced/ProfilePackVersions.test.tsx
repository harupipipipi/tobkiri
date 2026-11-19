import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {ProfilePackVersions} from './ProfilePackVersions';

const digest = (character: string) => `sha256:${character.repeat(64)}`;

test('a same-Profile refresh during save unlocks the editor and uses the successor revision', async () => {
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const dom = new JSDOM('<div id="root"></div>', {url: 'http://localhost/panel/'});
  for (const name of ['window', 'document', 'navigator', 'localStorage', 'sessionStorage'] as const) {
    Object.defineProperty(globalThis, name, {
      value: name === 'window' ? dom.window : dom.window[name], configurable: true,
    });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector('#root')!;
  const root = createRoot(container);
  const response = (data: unknown) => new Response(JSON.stringify({success: true, data}), {
    headers: {'Content-Type': 'application/json'},
  });
  const posts: Record<string, unknown>[] = [];
  const busyChanges: boolean[] = [];
  let packReads = 0;
  let revision = digest('a');
  let selected = digest('1');
  let completeRefresh!: () => void;
  const refreshing = new Promise<void>((resolve) => {completeRefresh = resolve;});
  const render = (definitionDigest: string) => root.render(<ProfilePackVersions profileId="defaults"
    definitionDigest={definitionDigest} disabled={false}
    onSaved={() => refreshing} onBusyChange={(busy) => busyChanges.push(busy)} />);
  const select = () => container.querySelector('select')!;
  const save = () => container.querySelector('button')!;
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    if (init?.method === 'POST') {
      assert.ok(url.endsWith('/api/v4/profiles/select-pack-version'));
      posts.push(JSON.parse(String(init.body)));
      selected = posts.length === 1 ? digest('2') : digest('1');
      revision = posts.length === 1 ? digest('b') : digest('c');
      return response({profile_id: 'defaults', profile_definition_revision: revision, activation_required: true});
    }
    assert.ok(url.endsWith('/api/v4/profiles/pack-versions?profile_id=defaults'));
    packReads++;
    return response({
      profile_id: 'defaults', profile_definition_revision: revision,
      store_generation: posts.length + 1, catalog_revision: digest('d'),
      packs: [{pack_id: 'provider', role: 'provider', selected_digest: selected,
        versions: ['1', '2'].map((version) => ({
          artifact_digest: digest(version), version, origin: 'bundled', capabilities: [], contracts: [],
        }))}],
    });
  };
  const choose = (value: string) => act(async () => {
    select().value = value;
    select().dispatchEvent(new dom.window.Event('change', {bubbles: true}));
  });
  try {
    await act(async () => render(digest('a')));
    await choose(digest('2'));
    await act(async () => save().click());
    assert.equal(posts.length, 1);
    await act(async () => render(digest('b')));
    assert.equal(select().value, digest('2'));
    assert.equal(select().disabled, true);
    await act(async () => completeRefresh());
    assert.equal(select().disabled, false);
    assert.deepEqual(busyChanges, [true, false]);
    assert.equal(packReads, 2, 'the new definition already refreshed the versions');

    await choose(digest('1'));
    await act(async () => save().click());
    assert.equal(posts.length, 2);
    assert.equal(posts[1]?.expected_selected_digest, digest('2'));
    assert.equal(posts[1]?.expected_profile_revision, digest('b'));
    assert.equal(posts[1]?.expected_store_generation, 2);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const name of ['window', 'document', 'navigator', 'localStorage', 'sessionStorage', 'fetch', 'IS_REACT_ACT_ENVIRONMENT']) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]!);
      else Reflect.deleteProperty(globalThis, name);
    }
  }
});
