import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DEVTOOLS_PANEL_ROUTE_KEYS,
  isDevtoolsPanelRouteKey,
  isPanelRouteActive,
  panelRouteMeta,
  panelRoutes,
  panelRouteTitleKey,
  viewerNavGroups,
} from './routes';

import { resolveSetupVerificationState } from './setupVerification';
import {
  assertRuntimeDispatchAllowed,
  RuntimeDispatchBlockedError,
  setRuntimeDispatchStatus,
} from './runtimeDispatchGate';

test('panel routes stay basename-relative', () => {
  assert.equal(panelRoutes.home, '/');
  assert.equal(panelRoutes.setup, '/setup');
  assert.equal(panelRoutes.packs, '/packs');
  assert.equal(panelRoutes.packDetail('defaultspack'), '/packs/defaultspack');
});

test('registered panel routes expose stable header title metadata', () => {
  const registeredRoutes = ['home', 'setup', 'packs'] as const;

  for (const route of registeredRoutes) {
    assert.equal(panelRouteTitleKey(panelRouteMeta[route].path), panelRouteMeta[route].titleKey);
    assert.match(panelRouteMeta[route].titleKey, /^nav\./);
  }

  assert.equal(panelRouteTitleKey('/packs/defaultspack'), panelRouteMeta.packs.titleKey);
  assert.equal(panelRouteTitleKey('/unknown-route'), 'nav.unknown');
});

test('viewer navigation keeps preferences separate and feature-gates one Devtools group', () => {
  const defaultGroups = viewerNavGroups(false);
  const enabledGroups = viewerNavGroups(true);
  const navRoutes = new Set<string>(enabledGroups.flatMap((group) => group.routes));
  const defaultRoutes = new Set<string>(defaultGroups.flatMap((group) => group.routes));
  assert.ok(navRoutes.has('packs'));
  for (const route of [
    'account',
    'settings',
    'profileWiring',
    'profileFiles',
    'flow',
    'graph',
    'aiInput',
    'apiMap',
    'nodeManager',
  ]) {
    assert.ok(navRoutes.has(route));
  }
  assert.deepEqual(
    enabledGroups.map((group) => group.id),
    ['workspace', 'preferences', 'devtools'],
  );
  assert.deepEqual(
    defaultGroups.map((group) => group.id),
    ['workspace', 'preferences'],
  );
  assert.deepEqual(enabledGroups.at(-1)?.routes, DEVTOOLS_PANEL_ROUTE_KEYS);
  for (const route of DEVTOOLS_PANEL_ROUTE_KEYS) {
    assert.equal(defaultRoutes.has(route), false);
    assert.equal(isDevtoolsPanelRouteKey(route), true);
  }
  // Flow is a primary task surface: reachable in default navigation and not
  // listed as a Devtools route even though its URL is stable.
  assert.equal(defaultRoutes.has('flow'), true);
  assert.equal(isDevtoolsPanelRouteKey('flow'), false);
  assert.equal(isDevtoolsPanelRouteKey('profile'), false);
  assert.ok(!navRoutes.has('startup'));

  for (const group of enabledGroups) {
    assert.match(group.labelKey, /^nav\./);
    for (const route of group.routes) {
      assert.match(panelRouteMeta[route].navKey || '', /^nav\./);
    }
  }
});

test('stable advanced panel paths map to rebuilt v4 surfaces', () => {
  for (const path of [
    '/nodes',
    '/graphs',
    '/profile-graph',
    '/ai-input',
    '/api-map',
    '/profile-workspace',
    '/flows',
    '/settings',
  ]) {
    assert.match(panelRouteTitleKey(path), /^nav\./);
  }

  assert.deepEqual(Object.keys(panelRouteMeta), [
    'home',
    'setup',
    'packs',
    'profile',
    'account',
    'settings',
    'profileWiring',
    'profileFiles',
    'flow',
    'graph',
    'aiInput',
    'apiMap',
    'nodeManager',
  ]);
  assert.equal(panelRouteTitleKey('/profile-unknown'), 'nav.unknown');
});

test('legacy Devtools deep links retain their stable route metadata', () => {
  assert.deepEqual(
    DEVTOOLS_PANEL_ROUTE_KEYS.map((route) => panelRouteMeta[route].path),
    [
      '/graphs',
      '/api-map',
      '/ai-input',
      '/nodes',
      '/profile-workspace',
      '/profile-graph',
    ],
  );
  for (const route of DEVTOOLS_PANEL_ROUTE_KEYS) {
    assert.equal(
      panelRouteTitleKey(panelRouteMeta[route].path),
      panelRouteMeta[route].titleKey,
    );
  }
});

test('stable route activity does not confuse Profile with Profile Wiring or Profile Files', () => {
  assert.equal(isPanelRouteActive('/profile', panelRoutes.profile), true);
  assert.equal(isPanelRouteActive('/profile-graph', panelRoutes.profile), false);
  assert.equal(isPanelRouteActive('/profile-workspace', panelRoutes.profile), false);
  assert.equal(isPanelRouteActive('/packs/provider-pack', panelRoutes.packs), true);
});

/** Check the shared navigation rendered by desktop and mobile menus. */
function assertOrdinarySetupEntry(): void {
  for (const devtoolsEnabled of [false, true]) {
    const groups = viewerNavGroups(devtoolsEnabled);
    const preferences = groups.find((group) => group.id === 'preferences');
    assert.ok(preferences);
    assert.ok(new Set<string>(preferences.routes).has('setup'));
    const allRoutes = groups.flatMap((group) => group.routes);
    assert.equal(allRoutes.filter((route) => route === 'setup').length, 1);
  }
  assert.equal(panelRouteMeta.setup.path, '/setup');
  assert.equal(panelRouteMeta.setup.navKey, 'nav.setup');
  assert.equal(isDevtoolsPanelRouteKey('setup'), false);
}

test('inactive handover catalog retains Setup when its verification banner is hidden', () => {
  // Successful imports retain definitions without activating a Profile.
  // This real verification branch hides the recovery banner at panel_ready.
  for (const isSetupDone of [false, true]) {
    assert.equal(resolveSetupVerificationState({
      isSetupDone,
      runtimeReady: false,
      runtimeStatus: 'panel_ready',
      runtimeDisconnected: false,
      hostCatalogVerified: true,
      profileCeremonyAvailable: true,
      defaultsBootstrapRequired: false,
    }), 'verified');
  }
  setRuntimeDispatchStatus('panel_ready');
  try {
    assert.throws(
      () => assertRuntimeDispatchAllowed('GET', '/api/runtime-surface/profile'),
      (error: unknown) => error instanceof RuntimeDispatchBlockedError
        && error.code === 'RUNTIME_DISPATCH_UNAVAILABLE'
        && error.status === 'panel_ready',
    );
    assertOrdinarySetupEntry();
  } finally {
    setRuntimeDispatchStatus('unknown');
  }
});

test('verified runtime keeps ordinary Setup without changing dispatch admission', () => {
  assert.equal(resolveSetupVerificationState({
    isSetupDone: true,
    runtimeReady: true,
    runtimeStatus: 'runtime_ready',
    runtimeDisconnected: false,
    hostCatalogVerified: true,
    profileCeremonyAvailable: true,
    defaultsBootstrapRequired: false,
  }), 'verified');
  setRuntimeDispatchStatus('runtime_ready');
  try {
    assert.doesNotThrow(
      () => assertRuntimeDispatchAllowed('GET', '/api/runtime-surface/profile'),
    );
    assertOrdinarySetupEntry();
  } finally {
    setRuntimeDispatchStatus('unknown');
  }
});

test('denied health leaves Setup reachable while dispatch remains closed', () => {
  assert.equal(resolveSetupVerificationState({
    isSetupDone: true,
    runtimeReady: false,
    runtimeStatus: 'error',
    runtimeDisconnected: false,
    hostCatalogVerified: false,
    profileCeremonyAvailable: false,
    defaultsBootstrapRequired: false,
  }), 'denied');
  setRuntimeDispatchStatus('error');
  try {
    assert.throws(
      () => assertRuntimeDispatchAllowed('GET', '/api/runtime-surface/profile'),
      RuntimeDispatchBlockedError,
    );
    assertOrdinarySetupEntry();
  } finally {
    setRuntimeDispatchStatus('unknown');
  }
});

test('execution Profile navigation belongs to Home, separate from personal settings', () => {
  assert.equal(isPanelRouteActive('/profile', panelRoutes.home), true);
  assert.equal(isPanelRouteActive('/account', panelRoutes.home), false);
  assert.equal(isPanelRouteActive('/account', panelRoutes.profile), false);
});
