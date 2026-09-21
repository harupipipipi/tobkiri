import assert from 'node:assert/strict';
import test from 'node:test';

import type {ApiProfileGraphEdge, ApiProfileGraphNode} from './apiTypes';
import {edgePath, layoutProfileGraph, profileGraphDisplayPorts} from './profileGraphLayout';

test('layoutProfileGraph keeps coordinates in a roomy fixed canvas', () => {
  const nodes: ApiProfileGraphNode[] = [
    {id: 'profile:research', kind: 'profile', label: 'Research', ref: 'research', metadata: {}},
    {id: 'tool:web', kind: 'tool', label: 'Web Search', ref: 'web', metadata: {}},
    {id: 'api:messages', kind: 'api', label: 'Messages', ref: 'messages', metadata: {}},
  ];

  const layout = layoutProfileGraph(nodes);

  assert.equal(layout.width >= 1000, true);
  assert.equal(layout.height >= 680, true);
  assert.equal(layout.nodes.some((node) => node.id === 'profile:research'), true);
});

test('edgePath connects node boundaries instead of center points', () => {
  const nodes = layoutProfileGraph([
    {id: 'profile:research', kind: 'profile', label: 'Research', ref: 'research', metadata: {}},
    {id: 'api:messages', kind: 'api', label: 'Messages', ref: 'messages', metadata: {}},
  ]).nodes;
  const positions = new Map(nodes.map((node) => [node.id, node]));
  const profile = positions.get('profile:research')!;
  const edge: ApiProfileGraphEdge = {
    id: 'edge',
    from_id: 'profile:research',
    to_id: 'api:messages',
    kind: 'allows_api',
    active: true,
    metadata: {},
  };

  const path = edgePath(edge, positions);

  assert.match(path, new RegExp(`^M ${profile.x + profile.width} ${profile.y + profile.height / 2} `));
});

test('profileGraphDisplayPorts keeps multiple declared port names and relationship ports', () => {
  const node: ApiProfileGraphNode = {
    id: 'node:router',
    kind: 'capability_node',
    label: 'Router',
    ref: 'router',
    metadata: {
      ports: [
        {id: 'request', label: 'Request', direction: 'input'},
        {id: 'primary', display_name: {en: 'Primary result'}, direction: 'output'},
        {id: 'fallback', label: 'Fallback result', direction: 'output'},
      ],
    },
  };
  const edges: ApiProfileGraphEdge[] = [{
    id: 'edge',
    from_id: 'node:router',
    to_id: 'node:consumer',
    kind: 'routes_to',
    active: true,
    from_port: 'primary',
    to_port: 'input',
    metadata: {},
  }];

  assert.deepEqual(profileGraphDisplayPorts(node, edges), [
    {id: 'request', label: 'Request', direction: 'input'},
    {id: 'primary', label: 'Primary result', direction: 'output'},
    {id: 'fallback', label: 'Fallback result', direction: 'output'},
  ]);
  assert.equal(layoutProfileGraph([node], edges).nodes[0]?.height, 98);
});
