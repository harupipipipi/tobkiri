import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {ProfilePackEditor} from './ProfilePackEditor';
import type {RuntimeProfileCatalogEntry} from '@/src/lib/runtimeSurface';
import type {NamedProfileRegistry} from '@/src/lib/profileRegistry';

const digest = (char: string) => `sha256:${char.repeat(64)}`;
const entry = {
  profile_id: 'editing', display_name: 'Editing', definition: {digest: digest('a')},
  pack_closure: [{pack_id: 'base-pack', role: 'base'}],
} as RuntimeProfileCatalogEntry;
function registry(): NamedProfileRegistry {
  return {
    profile_registry_api_version: 'io.tobkiri.profile-registry.v4', generation: 4,
    active_profile_id: null, active_profile_revision: null,
    profiles: [{profile_id: 'editing', profile_revision: digest('a'),
      profile: {profile_id: 'editing', display_name: 'Editing', packs: [{pack_id: 'one', role: 'provider'}]},
      order: 0, tombstone: false, parent_revision: null, created_at: 0, updated_at: 0, legacy_ids: []}],
  };
}
const catalog = {
  composition_api_version: 'io.tobkiri.profile-composition.v4',
  profile_catalog_digest: digest('b'), bundle_lock_digest: digest('c'),
  packs: ['one', 'two'].map((id) => ({pack_id: id, display_name: id, version: '1', kind: 'provider', artifact_digest: digest('d'), dependencies: []})),
};
const response = (data: unknown) => new Response(JSON.stringify({success: true, data}), {headers: {'Content-Type': 'application/json'}});

for (const rejected of [false, true]) test(`Pack selection ${rejected ? 'retains a rejected draft without automatic replay' : 'saves the selected Profile with revision fences before review'}`, async () => {
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const dom = new JSDOM('<div id="root"></div>', {url: 'http://localhost/panel/', pretendToBeVisual: true});
  for (const name of ['window', 'document', 'navigator', 'localStorage', 'sessionStorage'] as const) {
    Object.defineProperty(globalThis, name, {value: name === 'window' ? dom.window : dom.window[name], configurable: true});
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  let saved = 0;
  let editing = false;
  let current = registry();
  const posts: Record<string, unknown>[] = [];
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    if (init?.method === 'POST') {
      assert.ok(path.endsWith('/api/v4/profiles/update'));
      posts.push(JSON.parse(String(init.body)));
      if (rejected) return new Response(JSON.stringify({success: false, error: 'Profile revision is stale'}), {status: 409});
      const changed = {...current.profiles[0]!, profile_revision: digest('e'), parent_revision: digest('a'),
        profile: {...current.profiles[0]!.profile, packs: [{pack_id: 'one', role: 'provider'}, {pack_id: 'two', role: 'provider'}]}};
      current = {...current, generation: 5, profiles: [changed]};
      return response({...current, changed_profile: changed, action: 'update'});
    }
    if (path.endsWith('/api/v4/profiles/catalog')) return response(catalog);
    assert.ok(path.endsWith('/api/v4/profiles'));
    return response(current);
  };
  const container = dom.window.document.querySelector<HTMLElement>('#root')!;
  const root = createRoot(container);
  try {
    await act(async () => {root.render(<ProfilePackEditor entry={entry} locked={false} onEditingChange={(value) => {editing = value;}} onSaved={async () => {saved++;}} />);});
    const choices = container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');
    assert.equal(choices.length, 2);
    assert.equal(choices[0]?.checked, true);
    await act(async () => {choices[1]!.click();});
    assert.equal(editing, true);
    const save = [...container.querySelectorAll('button')].find((button) => button.textContent === 'Save Pack selection')!;
    await act(async () => {save.click(); save.click();});
    assert.equal(posts.length, 1);
    assert.deepEqual(posts[0], {profile_id: 'editing', display_name: 'Editing', expected_profile_revision: digest('a'), expected_store_generation: 4,
      composition: {pack_ids: ['one', 'two'], profile_catalog_digest: digest('b'), bundle_lock_digest: digest('c')}});
    assert.equal(saved, rejected ? 0 : 1);
    assert.equal(editing, rejected);
    if (rejected) {
      assert.ok(container.textContent?.includes('Profile revision is stale'));
      assert.equal(choices[1]?.checked, true);
      assert.equal(save.disabled, true);
    } else assert.match(container.textContent ?? '', /Pack selection saved/);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const name of ['window', 'document', 'navigator', 'localStorage', 'sessionStorage', 'fetch']) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]!);
      else Reflect.deleteProperty(globalThis, name);
    }
  }
});
