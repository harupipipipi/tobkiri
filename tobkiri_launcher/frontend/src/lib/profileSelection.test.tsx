import assert from 'node:assert/strict';
import {act, useEffect, type ReactNode} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {renderToStaticMarkup} from 'react-dom/server';
import {JSDOM} from 'jsdom';
import test from 'node:test';
import {MemoryRouter} from 'react-router';

import {InspectedProfileNotice} from '@/src/components/advanced/InspectedProfileNotice';
import {
  isProfileMutationBlocked,
  ProfileSelectionProvider,
  useProfileSelection,
} from '@/src/lib/profileSelection';

function createDom(): {container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {container, root: createRoot(container)};
}

function SelectionWriter({profileId, children}: {profileId: string | null; children: ReactNode}) {
  const {selectProfile} = useProfileSelection();
  useEffect(() => selectProfile(profileId), [profileId, selectProfile]);
  return children;
}

test('Profile selection is preserved across renders and performs no runtime call', async () => {
  const observed: Array<string | null> = [];
  const originalFetch = globalThis.fetch;
  let fetchCalls = 0;
  globalThis.fetch = ((...args: Parameters<typeof fetch>) => {
    fetchCalls += 1;
    return originalFetch(...args);
  }) as typeof fetch;

  function Probe() {
    const {selectedProfileId} = useProfileSelection();
    observed.push(selectedProfileId);
    return null;
  }

  const {root} = createDom();
  try {
    await act(async () => {
      root.render(
        <ProfileSelectionProvider>
          <SelectionWriter profileId="research">
            <Probe />
          </SelectionWriter>
        </ProfileSelectionProvider>,
      );
    });
    await act(async () => {
      root.render(
        <ProfileSelectionProvider>
          <SelectionWriter profileId="research">
            <Probe />
          </SelectionWriter>
        </ProfileSelectionProvider>,
      );
    });

    assert.equal(observed.at(-1), 'research');
    assert.ok(observed.length >= 2, 'selection must persist across re-renders');
    assert.equal(fetchCalls, 0, 'selecting a Profile must never call the runtime');
  } finally {
    await act(async () => root.unmount());
    globalThis.fetch = originalFetch;
  }
});

test('Profile selection outside the provider stays empty and safe', async () => {
  const observed: Array<string | null> = [];
  function Probe() {
    const {selectedProfileId} = useProfileSelection();
    observed.push(selectedProfileId);
    return null;
  }
  const {root} = createDom();
  await act(async () => {
    root.render(
      <MemoryRouter>
        <Probe />
        <InspectedProfileNotice surfaceProfileId="defaults" />
      </MemoryRouter>,
    );
  });
  assert.deepEqual(observed, [null]);
  assert.equal(document.querySelector('[data-testid="inspected-profile-notice"]'), null);
  await act(async () => root.unmount());
});

test('InspectedProfileNotice distinguishes mismatch, active, and unknown surface bindings', async () => {
  const {container, root} = createDom();
  const noticeText = () =>
    container.querySelector('[data-testid="inspected-profile-notice"]')?.textContent ?? '';
  const noticeHref = () =>
    container.querySelector('[data-testid="inspected-profile-notice"] a')?.getAttribute('href');
  const renderWith = (surfaceProfileId: string | null | undefined) => act(async () => {
    root.render(
      <MemoryRouter>
        <ProfileSelectionProvider>
          <SelectionWriter profileId="research">
            <InspectedProfileNotice surfaceProfileId={surfaceProfileId} />
          </SelectionWriter>
        </ProfileSelectionProvider>
      </MemoryRouter>,
    );
  });

  await renderWith('defaults');
  assert.match(noticeText(), /research.*not the active execution Profile/s);
  assert.match(noticeText(), /defaults/);
  assert.equal(noticeHref(), '/profile?profile_id=research');

  await renderWith('research');
  assert.match(noticeText(), /Inspecting the active execution Profile/);
  assert.match(noticeText(), /research/);

  await renderWith(null);
  assert.match(noticeText(), /active execution Profile only/);
  assert.equal(noticeHref(), '/profile?profile_id=research');

  await act(async () => root.unmount());
});

test('Profile selection mutation lock fails closed on mismatch or unknown binding', () => {
  assert.equal(isProfileMutationBlocked(null, 'defaults'), false);
  assert.equal(isProfileMutationBlocked(null, null), false);
  assert.equal(isProfileMutationBlocked('research', 'research'), false);
  assert.equal(isProfileMutationBlocked('research', 'defaults'), true);
  assert.equal(isProfileMutationBlocked('research', null), true);
  assert.equal(isProfileMutationBlocked('research', undefined), true);
});

test('direct Flow URL preserves the inspected Profile from the first render',async()=>{
 const {root}=createDom();const observed:Array<string|null>=[];
 function Probe(){const {selectedProfileId}=useProfileSelection();observed.push(selectedProfileId);return null;}
 try{
  await act(async()=>root.render(<MemoryRouter initialEntries={['/panel/flows?profile_id=research']}><ProfileSelectionProvider><Probe/></ProfileSelectionProvider></MemoryRouter>));
  assert.ok(observed.length>0);assert.ok(observed.every(value=>value==='research'));
  assert.equal(isProfileMutationBlocked(observed[0],'defaults'),true);
 }finally{await act(async()=>root.unmount());}
});

test('ambiguous or invalid URL Profile selections never silently target defaults',async()=>{
 const {inspectedProfileFromSearch}=await import('./profileSelection');
 for(const query of ['?profile_id=','?profile_id=a&profile_id=b','?profile_id=%3Cbad%3E','?profile_id=a&profile=b']){
  const selected=inspectedProfileFromSearch(query);assert.equal(isProfileMutationBlocked(selected??null,'defaults'),true);
 }
 assert.equal(inspectedProfileFromSearch(''),undefined);
});
