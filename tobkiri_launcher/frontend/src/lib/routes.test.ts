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
    'profile',
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
      '/flows',
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
