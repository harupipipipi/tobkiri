import test from 'node:test';
import assert from 'node:assert/strict';

import {
  aiInputRoute,
  apiMapRoute,
  panelRouteMeta,
  panelRoutes,
  panelRouteTitleKey,
  profileGraphRoute,
  viewerNavGroups,
} from './routes';

test('panel routes stay basename-relative', () => {
  assert.equal(panelRoutes.home, '/');
  assert.equal(panelRoutes.setup, '/setup');
  assert.equal(panelRoutes.packs, '/packs');
  assert.equal(panelRoutes.nodes, '/nodes');
  assert.equal(panelRoutes.graphEditor, '/graphs');
  assert.equal(panelRoutes.profileGraph, '/profile-graph');
  assert.equal(panelRoutes.aiInput, '/ai-input');
  assert.equal(panelRoutes.apiMap, '/api-map');
  assert.equal(panelRoutes.profileWorkspace, '/profile-workspace');
  assert.equal(panelRoutes.startup, '/startup');
  assert.equal(panelRoutes.flows, '/flows');
  assert.equal(panelRoutes.settings, '/settings');
  assert.equal(panelRoutes.packDetail('defaultspack'), '/packs/defaultspack');
});

test('profile routes can carry focused profile context', () => {
  assert.equal(profileGraphRoute('default-profile'), '/profile-graph?profile=default-profile');
  assert.equal(aiInputRoute('default-profile'), '/ai-input?profile=default-profile');
  assert.equal(apiMapRoute({ profileId: 'default-profile', focus: 'profile:default-profile' }), '/api-map?profile_id=default-profile&focus=profile%3Adefault-profile');
  assert.equal(apiMapRoute({ profileId: 'default-profile' }), '/api-map?profile_id=default-profile');
});

test('registered panel routes expose stable header title metadata', () => {
  const registeredRoutes = [
    'home',
    'setup',
    'packs',
    'nodes',
    'graphEditor',
    'profileGraph',
    'aiInput',
    'apiMap',
    'profileWorkspace',
    'startup',
    'flows',
    'settings',
  ] as const;

  for (const route of registeredRoutes) {
    assert.equal(panelRouteTitleKey(panelRouteMeta[route].path), panelRouteMeta[route].titleKey);
    assert.match(panelRouteMeta[route].titleKey, /^nav\./);
  }

  assert.equal(panelRouteTitleKey('/packs/defaultspack'), panelRouteMeta.packs.titleKey);
  assert.equal(panelRouteTitleKey('/unknown-route'), 'nav.unknown');
});

test('viewer navigation groups use route metadata and i18n keys', () => {
  const navRoutes = new Set<string>(viewerNavGroups.flatMap((group) => group.routes));
  assert.ok(navRoutes.has('aiInput'));
  assert.ok(!navRoutes.has('startup'));

  for (const group of viewerNavGroups) {
    assert.match(group.labelKey, /^nav\./);
    for (const route of group.routes) {
      assert.match(panelRouteMeta[route].navKey || '', /^nav\./);
    }
  }
});
