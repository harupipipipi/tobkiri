import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {MemoryRouter} from 'react-router';

import type {NamedProfileRecord} from '@/src/lib/profileRegistry';
import {ProfileCard} from './ProfileCard';

const profile: NamedProfileRecord = {
  profile_id: 'broken-profile',
  profile_revision: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  profile: {},
  order: 0,
  parent_revision: null,
  tombstone: false,
  created_at: 1,
  updated_at: 1,
  legacy_ids: [],
};

test('ProfileCard copies the complete visible Profile error diagnostic', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  let root: Root | null = null;
  let copied = '';
  try {
    Object.defineProperties(globalThis, {
      window: {value: dom.window, configurable: true},
      document: {value: dom.window.document, configurable: true},
      navigator: {value: dom.window.navigator, configurable: true},
    });
    Object.defineProperty(dom.window.navigator, 'clipboard', {
      configurable: true,
      value: {writeText: async (text: string) => { copied = text; }},
    });
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    const container = dom.window.document.querySelector<HTMLElement>('#root');
    assert.ok(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <MemoryRouter>
          <ProfileCard
            activationHref="/profile/activate"
            activeProfileReady={false}
            actionType={null}
            browseHref="/profile"
            closureHref="/profile#profile-closure"
            desktopShellAvailable
            editing={false}
            editingName="Broken Profile"
            isActive={false}
            isBrowsing={false}
            isBusy={false}
            launchReady={false}
            mutationsAvailable
            onCancelEdit={() => undefined}
            onDelete={() => undefined}
            onDuplicate={() => undefined}
            onEdit={() => undefined}
            onEditingNameChange={() => undefined}
            onLaunch={() => undefined}
            onSubmitEdit={() => undefined}
            profile={profile}
            profileCeremonyAvailable
            profileView={{
              basePackId: null,
              displayName: 'Broken Profile',
              packIds: [],
              status: 'error',
              statusDescription: 'The v4 Base Pack is missing.',
              statusLabel: 'Error',
            }}
          />
        </MemoryRouter>,
      );
    });
    assert.match(container.textContent ?? '', /Error\. The v4 Base Pack is missing\./);
    const copy = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Copy Broken Profile Profile error"]',
    );
    assert.ok(copy);
    await act(async () => {
      copy.click();
      await Promise.resolve();
    });
    assert.equal(copied, 'Error. The v4 Base Pack is missing.');
  } finally {
    await act(async () => root?.unmount());
    dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});
