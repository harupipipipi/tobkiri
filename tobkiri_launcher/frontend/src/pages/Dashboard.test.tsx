import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {MemoryRouter} from 'react-router';

import {
  copyTextToClipboard,
  nextDuplicateProfileId,
  Dashboard,
} from './Dashboard';
import type {NamedProfileRegistry} from '@/src/lib/api';
import {DialogContainer} from '@/src/components/ui/DialogContainer';
import {useAppStore} from '@/src/store';

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

function profileRecord(profileId: string, displayName: string, revision: string) {
  const resolved = profileId === 'defaults';
  return {
    profile_id: profileId,
    profile_revision: revision,
    profile: {
      profile_id: profileId,
      display_name: displayName,
      profile_api_version: 'io.tobkiri.profile.v4',
      state: resolved ? 'resolved' : 'needs_resolution',
      mode: 'interactive',
      catalog_revision: resolved ? digest('c') : null,
      base: {
        pack_id: 'defaults-basepack',
        artifact_digest: resolved ? digest('d') : null,
        definition_revision: resolved ? digest('e') : null,
      },
      shell: resolved ? {
        provider_id: 'shell.tauri.default',
        pack_id: 'shell.tauri.default',
        artifact_digest: digest('f'),
        definition_revision: digest('1'),
      } : null,
      packs: [{pack_id: 'defaultspack', artifact_digest: resolved ? digest('2') : null}],
    },
    order: profileId === 'defaults' ? 0 : 1,
    parent_revision: null,
    tombstone: false,
    created_at: 1,
    updated_at: 1,
    legacy_ids: [],
  };
}

function profileRegistry(): NamedProfileRegistry {
  return {
    profile_registry_api_version: 'io.tobkiri.profile-registry.v4',
    generation: 1,
    active_profile_id: 'defaults',
    active_profile_revision: digest('e'),
    profiles: [
      profileRecord('defaults', 'Defaults Profile', digest('a')),
      profileRecord('research', 'Research Profile', digest('b')),
    ],
  };
}

function legacyFixtureProfileRecord(
  profileId: string,
  displayName: string,
  order: number,
): ReturnType<typeof profileRecord> {
  return {
    profile_id: profileId,
    profile_revision: digest(String(order)),
    profile: {
      profile_id: profileId,
      display_name: displayName,
      profile_api_version: 'io.tobkiri.profile.v4',
      state: 'needs_resolution',
      mode: 'interactive',
      catalog_revision: null,
      base: {pack_id: 'defaultspack', artifact_digest: null, definition_revision: null},
      shell: null,
      packs: [{pack_id: 'defaultspack', artifact_digest: null}],
    },
    order,
    parent_revision: null,
    tombstone: false,
    created_at: order,
    updated_at: order,
    legacy_ids: [profileId],
  };
}

function legacyFixtureRegistry(): NamedProfileRegistry {
  return {
    profile_registry_api_version: 'io.tobkiri.profile-registry.v4',
    generation: 7,
    active_profile_id: null,
    active_profile_revision: null,
    profiles: [
      {
        ...profileRecord('defaults', 'Tobkiri Defaults', digest('a')),
        profile: {
          ...profileRecord('defaults', 'Tobkiri Defaults', digest('a')).profile,
          base: {
            pack_id: 'defaultspack',
            artifact_digest: digest('b'),
            definition_revision: digest('c'),
          },
          catalog_revision: digest('d'),
          shell: {
            provider_id: 'shell.tauri.default',
            pack_id: 'defaultspack',
            artifact_digest: digest('e'),
            definition_revision: digest('f'),
          },
          packs: [{pack_id: 'defaultspack', artifact_digest: digest('1')}],
        },
        order: 0,
      },
      legacyFixtureProfileRecord('default-profile', 'Default Profile', 1),
      legacyFixtureProfileRecord('new-custom-profile', 'New custom profile', 2),
      legacyFixtureProfileRecord('new-custom-profile-2', 'New custom profile 2', 3),
    ],
  };
}

function createDashboardDom(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/panel/',
  });
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
    sessionStorage: {value: dom.window.sessionStorage, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {dom, container, root: createRoot(container)};
}

function jsonResponse(data: unknown): Response {
  return new Response(JSON.stringify({success: true, data}), {
    headers: {'Content-Type': 'application/json'},
  });
}

function buttonByLabel(container: HTMLElement, label: string): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  assert.ok(button, `missing button ${label}`);
  return button;
}

function linkByLabel(container: HTMLElement, label: string): HTMLAnchorElement {
  const link = container.querySelector<HTMLAnchorElement>(`a[aria-label="${label}"]`);
  assert.ok(link, `missing link ${label}`);
  return link;
}

function menuItemByText(text: string): HTMLElement {
  const item = [...document.querySelectorAll<HTMLElement>('[role="menuitem"]')]
    .find((candidate) => candidate.textContent?.includes(text));
  assert.ok(item, `missing menu item ${text}`);
  return item;
}

async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function changeControlValue(
  dom: JSDOM,
  control: HTMLInputElement | HTMLSelectElement,
  value: string,
): void {
  control.value = value;
  control.dispatchEvent(new dom.window.Event('input', {bubbles: true}));
  control.dispatchEvent(new dom.window.Event('change', {bubbles: true}));
}

test('copyTextToClipboard copies the complete runtime error message', async () => {
  let copied = '';
  const success = await copyTextToClipboard('Kernel failed to start', {
    writeText: async (text: string) => {
      copied = text;
    },
  });

  assert.equal(success, true);
  assert.equal(copied, 'Kernel failed to start');
});

test('copyTextToClipboard returns false when the clipboard is unavailable', async () => {
  const success = await copyTextToClipboard('message', undefined);
  assert.equal(success, false);
});

test('duplicate Profile IDs are deterministic and never privilege Defaults', () => {
  assert.equal(nextDuplicateProfileId('work-a', ['defaults', 'work-a']), 'work-a-copy');
  assert.equal(
    nextDuplicateProfileId('work-a', ['work-a-copy', 'work-a-copy-2']),
    'work-a-copy-3',
  );
});

test('Home keeps the Profile catalog visible while gating ceremony in unresolved states', async () => {
  const previousState = useAppStore.getState();
  const previousFetch = globalThis.fetch;
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const previousLocalStorage = (globalThis as typeof globalThis & {localStorage?: unknown}).localStorage;
  const previousSessionStorage = (globalThis as typeof globalThis & {sessionStorage?: unknown}).sessionStorage;

  try {
    globalThis.fetch = (async (input) => {
      const path = new URL(String(input), 'http://localhost').pathname;
      assert.equal(path, '/api/v4/profiles');
      return jsonResponse(profileRegistry());
    }) as typeof fetch;

    const scenarios = [
      {
        name: 'needs_setup',
        isSetupDone: false,
        runtimeDisconnected: false,
        defaultsBootstrapRequired: false,
      },
      {
        name: 'disconnected',
        isSetupDone: true,
        runtimeDisconnected: true,
        defaultsBootstrapRequired: false,
      },
      {
        name: 'empty_bootstrap',
        isSetupDone: false,
        runtimeDisconnected: false,
        defaultsBootstrapRequired: true,
      },
    ] as const;

    for (const scenario of scenarios) {
      useAppStore.setState({
        isSetupDone: scenario.isSetupDone,
        runtimeReady: false,
        runtimeStatus: scenario.name === 'disconnected' ? 'error' : 'starting',
        runtimeError: scenario.name === 'disconnected' ? 'runtime disconnected' : null,
        runtimeDisconnected: scenario.runtimeDisconnected,
        lastRuntimeHealthyAt: null,
        hostCatalogVerified: true,
        profileCeremonyAvailable: scenario.name !== 'disconnected',
        defaultsBootstrapRequired: scenario.defaultsBootstrapRequired,
        activeProfileReady: false,
        launchReady: false,
      });
      const {dom, container, root} = createDashboardDom();
      try {
        await act(async () => {
          root.render(<MemoryRouter><Dashboard /></MemoryRouter>);
        });
        await settle();

        assert.match(container.textContent ?? '', /Profiles/, scenario.name);
        assert.match(container.textContent ?? '', /Defaults Profile/, scenario.name);
        assert.match(container.textContent ?? '', /Research Profile/, scenario.name);
        const summary = container.querySelector('[aria-label="Workspace summary"]');
        assert.ok(summary);
        assert.match(summary.textContent ?? '', /Not verified/, scenario.name);
        assert.doesNotMatch(summary.textContent ?? '', /Stopped|Running/, scenario.name);
        assert.ok(container.querySelector('input[aria-label="Search Profiles"]'));

        const addProfile = [...container.querySelectorAll('button')].find(
          (button) => button.textContent?.includes('Add Profile'),
        );
        assert.ok(addProfile, `${scenario.name}: Add Profile should be visible`);
        await act(async () => { addProfile.click(); });
        assert.ok(container.querySelector('input[aria-label="New Profile ID"]'));
        assert.ok(container.querySelector('select[aria-label="Source Profile"]'));

        assert.ok(container.querySelector('[data-testid="profile-grid"]'));
        assert.equal(container.querySelectorAll('[data-profile-card]').length, 2);
        assert.ok(container.querySelector('[data-profile-card="defaults"][data-profile-status="ready"]'));
        assert.ok(container.querySelector('[data-profile-card="research"][data-profile-status="error"]'));
        assert.equal(
          container.querySelector<HTMLAnchorElement>('a[aria-label="View Pack closure for Defaults Profile"]')?.getAttribute('href'),
          '/profile?profile_id=defaults#profile-closure',
        );
        assert.equal(
          container.querySelector<HTMLAnchorElement>('a[aria-label="Browse and review Research Profile"]')?.getAttribute('href'),
          '/profile?profile_id=research',
        );

        await act(async () => { buttonByLabel(container, 'Open actions for Defaults Profile').click(); });
        assert.ok(menuItemByText('Edit'));
        assert.ok(menuItemByText('Active'));
        assert.ok(menuItemByText('Duplicate'));
        const defaultsDelete = menuItemByText('Delete') as HTMLButtonElement;
        assert.equal(defaultsDelete.disabled, true);
        await act(async () => { buttonByLabel(container, 'Open actions for Defaults Profile').click(); });

        await act(async () => { buttonByLabel(container, 'Open actions for Research Profile').click(); });
        const activate = menuItemByText('Set Active') as HTMLAnchorElement;
        assert.equal(activate.getAttribute('aria-label'), 'Activate Research Profile');
        if (scenario.name !== 'needs_setup') {
          assert.equal(activate.getAttribute('aria-disabled'), 'true', scenario.name);
          assert.equal(activate.getAttribute('tabindex'), '-1', scenario.name);
        } else {
          assert.notEqual(activate.getAttribute('aria-disabled'), 'true', scenario.name);
          assert.notEqual(activate.getAttribute('tabindex'), '-1', scenario.name);
          assert.equal(activate.getAttribute('href'), '/profile?profile_id=research#profile-ceremony');
        }
        assert.match(activate.textContent ?? '', /Set Active/);
        const researchDelete = menuItemByText('Delete') as HTMLButtonElement;
        assert.equal(researchDelete.disabled, false);
        assert.equal(buttonByLabel(container, 'Launch Defaults Profile').disabled, true, scenario.name);
        await act(async () => { researchDelete.click(); });
        assert.equal(useAppStore.getState().dialog?.title, 'Delete Research Profile?');
        await act(async () => useAppStore.getState().closeDialog());
      } finally {
        await act(async () => root.unmount());
        dom.window.close();
      }
    }
  } finally {
    globalThis.fetch = previousFetch;
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
      localStorage: {value: previousLocalStorage, configurable: true},
      sessionStorage: {value: previousSessionStorage, configurable: true},
    });
    useAppStore.setState(previousState, true);
  }
});

test('Home exposes a fresh active-none catalog without privileging Defaults', async () => {
  const previousState = useAppStore.getState();
  const previousFetch = globalThis.fetch;
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const previousLocalStorage = (globalThis as typeof globalThis & {localStorage?: unknown}).localStorage;
  const previousSessionStorage = (globalThis as typeof globalThis & {sessionStorage?: unknown}).sessionStorage;

  try {
    globalThis.fetch = (async (input) => {
      const path = new URL(String(input), 'http://localhost').pathname;
      assert.equal(path, '/api/v4/profiles');
      return jsonResponse(legacyFixtureRegistry());
    }) as typeof fetch;
    useAppStore.setState({
      isSetupDone: false,
      runtimeReady: false,
      runtimeStatus: 'starting',
      runtimeError: null,
      runtimeDisconnected: false,
      lastRuntimeHealthyAt: null,
      hostCatalogVerified: true,
      profileCeremonyAvailable: true,
      activeProfileReady: false,
      launchReady: false,
    });
    const {dom, container, root} = createDashboardDom();
    try {
      await act(async () => {
        root.render(<MemoryRouter><Dashboard /></MemoryRouter>);
      });
      await settle();

      assert.ok(container.querySelector('[data-testid="profile-grid"]'));
      assert.equal(container.querySelectorAll('[data-profile-card]').length, 4);
      for (const profileId of [
        'default-profile',
        'new-custom-profile',
        'new-custom-profile-2',
        'defaults',
      ]) {
        assert.ok(container.querySelector(`[data-profile-card="${profileId}"]`), profileId);
      }
      assert.match(container.textContent ?? '', /Tobkiri Defaults/);
      assert.match(container.textContent ?? '', /No active execution Profile/);
      assert.doesNotMatch(container.textContent ?? '', /unsupported v4 definition/i);
      assert.ok(container.querySelector('input[aria-label="Search Profiles"]'));
      assert.ok([...container.querySelectorAll('button')].some(
        (button) => button.textContent?.includes('Add Profile'),
      ));

      for (const displayName of [
        'Tobkiri Defaults',
        'Default Profile',
        'New custom profile',
        'New custom profile 2',
      ]) {
        assert.ok(buttonByLabel(container, `Launch ${displayName}`).disabled, displayName);
        assert.ok(linkByLabel(container, `Browse and review ${displayName}`));
        assert.ok(linkByLabel(container, `View Pack closure for ${displayName}`));
      }

      await act(async () => {
        buttonByLabel(container, 'Open actions for New custom profile').click();
      });
      const activate = menuItemByText('Set Active') as HTMLAnchorElement;
      assert.equal(activate.getAttribute('href'), '/profile?profile_id=new-custom-profile#profile-ceremony');
      assert.notEqual(activate.getAttribute('aria-disabled'), 'true');
      assert.notEqual(activate.getAttribute('tabindex'), '-1');
      assert.equal(buttonByLabel(container, 'Launch New custom profile').disabled, true);
    } finally {
      await act(async () => root.unmount());
      dom.window.close();
    }
  } finally {
    globalThis.fetch = previousFetch;
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
      localStorage: {value: previousLocalStorage, configurable: true},
      sessionStorage: {value: previousSessionStorage, configurable: true},
    });
    useAppStore.setState(previousState, true);
  }
});

test('Home keeps browsing selection separate from active execution', async () => {
  const previousState = useAppStore.getState();
  const previousFetch = globalThis.fetch;
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const previousLocalStorage = (globalThis as typeof globalThis & {localStorage?: unknown}).localStorage;
  const previousSessionStorage = (globalThis as typeof globalThis & {sessionStorage?: unknown}).sessionStorage;

  try {
    globalThis.fetch = (async () => jsonResponse(profileRegistry())) as typeof fetch;
    useAppStore.setState({
      isSetupDone: false,
      runtimeReady: false,
      runtimeStatus: 'starting',
      runtimeError: null,
      runtimeDisconnected: false,
      lastRuntimeHealthyAt: null,
      hostCatalogVerified: true,
      profileCeremonyAvailable: true,
      activeProfileReady: false,
      launchReady: false,
    });
    const {dom, container, root} = createDashboardDom();
    try {
      await act(async () => {
        root.render(<MemoryRouter initialEntries={['/?profile_id=research']}><Dashboard /></MemoryRouter>);
      });
      await settle();
      const defaultsCard = container.querySelector<HTMLElement>('[data-profile-card="defaults"]');
      const researchCard = container.querySelector<HTMLElement>('[data-profile-card="research"]');
      assert.ok(defaultsCard);
      assert.ok(researchCard);
      assert.match(defaultsCard.textContent ?? '', /Active execution/);
      assert.doesNotMatch(defaultsCard.textContent ?? '', /Selected browsing/);
      assert.match(researchCard.textContent ?? '', /Selected browsing/);
      assert.doesNotMatch(researchCard.textContent ?? '', /Active execution/);
    } finally {
      await act(async () => root.unmount());
      dom.window.close();
    }
  } finally {
    globalThis.fetch = previousFetch;
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
      localStorage: {value: previousLocalStorage, configurable: true},
      sessionStorage: {value: previousSessionStorage, configurable: true},
    });
    useAppStore.setState(previousState, true);
  }
});

test('Home presents the deletion confirmation without deleting a Profile', async () => {
  const previousState = useAppStore.getState();
  const previousFetch = globalThis.fetch;
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const previousLocalStorage = (globalThis as typeof globalThis & {localStorage?: unknown}).localStorage;
  const previousSessionStorage = (globalThis as typeof globalThis & {sessionStorage?: unknown}).sessionStorage;

  try {
    globalThis.fetch = (async () => jsonResponse(profileRegistry())) as typeof fetch;
    useAppStore.setState({
      isSetupDone: true,
      runtimeReady: false,
      runtimeStatus: 'starting',
      runtimeError: null,
      runtimeDisconnected: false,
      lastRuntimeHealthyAt: null,
      hostCatalogVerified: true,
      profileCeremonyAvailable: true,
      activeProfileReady: false,
      launchReady: false,
    });
    const {dom, container, root} = createDashboardDom();
    try {
      await act(async () => {
        root.render(<MemoryRouter><><Dashboard /><DialogContainer /></></MemoryRouter>);
      });
      await settle();
      await act(async () => { buttonByLabel(container, 'Open actions for Research Profile').click(); });
      await act(async () => { menuItemByText('Delete').click(); });
      assert.equal(container.querySelector('[role="alertdialog"] h2')?.textContent, 'Delete Research Profile?');
      assert.equal(useAppStore.getState().dialog?.title, 'Delete Research Profile?');
      assert.ok(container.textContent?.includes('Keep Profile'));
      assert.ok(container.textContent?.includes('Delete Profile'));
      assert.equal(container.querySelector('[data-profile-card="research"]') !== null, true);
    } finally {
      await act(async () => useAppStore.getState().closeDialog());
      await act(async () => root.unmount());
      dom.window.close();
    }
  } finally {
    globalThis.fetch = previousFetch;
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
      localStorage: {value: previousLocalStorage, configurable: true},
      sessionStorage: {value: previousSessionStorage, configurable: true},
    });
    useAppStore.setState(previousState, true);
  }
});

test('Home requires an explicit source Profile and does not use registry order for Add', async () => {
  const previousState = useAppStore.getState();
  const previousFetch = globalThis.fetch;
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const previousLocalStorage = (globalThis as typeof globalThis & {localStorage?: unknown}).localStorage;
  const previousSessionStorage = (globalThis as typeof globalThis & {sessionStorage?: unknown}).sessionStorage;
  const requests: Array<{path: string; method: string; body: Record<string, unknown> | null}> = [];
  const reordered = profileRegistry();
  reordered.profiles = [...reordered.profiles].reverse();

  try {
    globalThis.fetch = (async (input, init) => {
      const path = new URL(String(input), 'http://localhost').pathname;
      const method = init?.method ?? 'GET';
      const body = typeof init?.body === 'string' ? JSON.parse(init.body) as Record<string, unknown> : null;
      requests.push({path, method, body});
      if (path === '/api/v4/profiles') return jsonResponse(reordered);
      assert.equal(path, '/api/v4/profiles/create');
      assert.equal(method, 'POST');
      assert.equal(body?.source_profile_id, 'research');
      return jsonResponse({
        ...reordered,
        generation: 2,
        profiles: [...reordered.profiles, profileRecord('new-profile', 'New Profile', digest('9'))],
      });
    }) as typeof fetch;
    useAppStore.setState({
      isSetupDone: false,
      runtimeReady: false,
      runtimeStatus: 'starting',
      runtimeError: null,
      runtimeDisconnected: false,
      lastRuntimeHealthyAt: null,
      hostCatalogVerified: true,
      profileCeremonyAvailable: true,
      activeProfileReady: false,
      launchReady: false,
    });
    const {dom, container, root} = createDashboardDom();
    try {
      await act(async () => {
        root.render(<MemoryRouter><Dashboard /></MemoryRouter>);
      });
      await settle();
      await act(async () => {
        buttonByLabel(container, 'Add Profile').click();
      });

      const source = container.querySelector<HTMLSelectElement>('select[aria-label="Source Profile"]');
      assert.ok(source);
      assert.equal(source.value, '');
      assert.deepEqual(
        [...source.options].map((option) => option.value),
        ['', 'defaults', 'research'],
      );
      const createButton = [...container.querySelectorAll<HTMLButtonElement>('button')]
        .find((button) => button.textContent?.includes('Create') && button.form?.id === 'add-profile-form');
      assert.ok(createButton);

      const profileIdInput = container.querySelector<HTMLInputElement>('input[aria-label="New Profile ID"]');
      const profileNameInput = container.querySelector<HTMLInputElement>('input[aria-label="New Profile name"]');
      assert.ok(profileIdInput);
      assert.ok(profileNameInput);
      assert.equal(createButton.disabled, true, 'source selection is required before create');
      await act(async () => {
        changeControlValue(dom, source, 'research');
      });
      assert.equal(source.value, 'research');
      assert.equal(createButton.disabled, false);
      await act(async () => {
        changeControlValue(dom, profileIdInput, 'new-profile');
      });
      await act(async () => {
        changeControlValue(dom, profileNameInput, 'New Profile');
      });
      assert.equal(profileIdInput.value, 'new-profile');
      assert.equal(profileNameInput.value, 'New Profile');

      await act(async () => {
        createButton.click();
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      await settle();

      assert.ok(requests.some((request) => request.path === '/api/v4/profiles/create'));
      assert.ok(
        container.querySelector('[data-profile-card="new-profile"]'),
        `text=${container.textContent} requests=${JSON.stringify(requests)}`,
      );
      assert.equal(container.querySelector('#add-profile-form'), null);
    } finally {
      await act(async () => root.unmount());
      dom.window.close();
    }
  } finally {
    globalThis.fetch = previousFetch;
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
      localStorage: {value: previousLocalStorage, configurable: true},
      sessionStorage: {value: previousSessionStorage, configurable: true},
    });
    useAppStore.setState(previousState, true);
  }
});

test('Home keeps a verified catalog writable after a rejected Profile mutation', async () => {
  const previousState = useAppStore.getState();
  const previousFetch = globalThis.fetch;
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const previousLocalStorage = (globalThis as typeof globalThis & {localStorage?: unknown}).localStorage;
  const previousSessionStorage = (globalThis as typeof globalThis & {sessionStorage?: unknown}).sessionStorage;
  let updateRequests = 0;
  let updatePayload: Record<string, unknown> | null = null;

  try {
    globalThis.fetch = (async (input, init) => {
      const path = new URL(String(input), 'http://localhost').pathname;
      if (path === '/api/v4/profiles') return jsonResponse(profileRegistry());
      assert.equal(path, '/api/v4/profiles/update');
      assert.equal(init?.method, 'POST');
      updateRequests += 1;
      updatePayload = typeof init?.body === 'string'
        ? JSON.parse(init.body) as Record<string, unknown>
        : null;
      return new Response(JSON.stringify({success: false, error: 'revision conflict'}), {
        headers: {'Content-Type': 'application/json'},
        status: 409,
      });
    }) as typeof fetch;
    useAppStore.setState({
      isSetupDone: true,
      runtimeReady: false,
      runtimeStatus: 'starting',
      runtimeError: null,
      runtimeDisconnected: false,
      lastRuntimeHealthyAt: null,
      hostCatalogVerified: true,
      profileCeremonyAvailable: true,
      activeProfileReady: false,
      launchReady: false,
    });
    const {dom, container, root} = createDashboardDom();
    try {
      await act(async () => {
        root.render(<MemoryRouter><Dashboard /></MemoryRouter>);
      });
      await settle();
      await act(async () => {
        buttonByLabel(container, 'Open actions for Research Profile').click();
      });
      await act(async () => {
        menuItemByText('Edit').click();
      });

      const nameInput = container.querySelector<HTMLInputElement>('input[aria-label="Display name for research"]');
      assert.ok(nameInput);
      await act(async () => {
        changeControlValue(dom, nameInput, 'Research Renamed');
      });
      await act(async () => {
        nameInput.closest('form')?.dispatchEvent(
          new dom.window.Event('submit', {bubbles: true, cancelable: true}),
        );
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      await settle();

      assert.equal(updateRequests, 1);
      assert.equal(updatePayload?.display_name, 'Research Renamed');
      assert.match(container.textContent ?? '', /revision conflict/);
      assert.equal(buttonByLabel(container, 'Add Profile').disabled, false);
      assert.equal(
        linkByLabel(container, 'Browse and review Research Profile').getAttribute('href'),
        '/profile?profile_id=research',
      );
      await act(async () => {
        buttonByLabel(container, 'Open actions for Research Profile').click();
      });
      assert.equal((menuItemByText('Duplicate') as HTMLButtonElement).disabled, false);
      assert.equal((menuItemByText('Delete') as HTMLButtonElement).disabled, false);
    } finally {
      await act(async () => root.unmount());
      dom.window.close();
    }
  } finally {
    globalThis.fetch = previousFetch;
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
      localStorage: {value: previousLocalStorage, configurable: true},
      sessionStorage: {value: previousSessionStorage, configurable: true},
    });
    useAppStore.setState(previousState, true);
  }
});
