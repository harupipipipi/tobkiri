import test from 'node:test';
import assert from 'node:assert/strict';

import {
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

test('viewer navigation groups use route metadata and i18n keys', () => {
  const navRoutes = new Set<string>(viewerNavGroups.flatMap((group) => group.routes));
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
  assert.ok(!navRoutes.has('startup'));

  for (const group of viewerNavGroups) {
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

test('stable route activity does not confuse Profile with Profile Wiring or Profile Files', () => {
  assert.equal(isPanelRouteActive('/profile', panelRoutes.profile), true);
  assert.equal(isPanelRouteActive('/profile-graph', panelRoutes.profile), false);
  assert.equal(isPanelRouteActive('/profile-workspace', panelRoutes.profile), false);
  assert.equal(isPanelRouteActive('/packs/provider-pack', panelRoutes.packs), true);
});
