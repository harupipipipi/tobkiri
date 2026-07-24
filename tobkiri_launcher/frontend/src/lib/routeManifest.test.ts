import assert from 'node:assert/strict';
import test from 'node:test';

import {collectManifestAssets, type ViteManifest} from './routeManifest.ts';

test('collectManifestAssets includes shared static imports without following unrelated dynamic imports', () => {
  const manifest: ViteManifest = {
    'src/pages/Flows.tsx': {
      file: 'assets/Flows-a.js',
      css: ['assets/flows.css'],
      imports: ['_shared.js'],
      dynamicImports: ['src/pages/Settings.tsx'],
    },
    '_shared.js': {file: 'assets/shared.js', css: ['assets/shared.css']},
    'src/pages/Settings.tsx': {file: 'assets/Settings-b.js'},
  };

  assert.deepEqual(
    collectManifestAssets(manifest, ['src/pages/Flows.tsx']),
    {
      scripts: ['assets/Flows-a.js', 'assets/shared.js'],
      styles: ['assets/flows.css', 'assets/shared.css'],
    },
  );
});

test('collectManifestAssets de-duplicates shared assets across routes', () => {
  const manifest: ViteManifest = {
    '/workspace/src/pages/Packs.tsx': {file: 'assets/Packs.js', imports: ['shared']},
    '/workspace/src/pages/Flows.tsx': {file: 'assets/Flows.js', imports: ['shared']},
    shared: {file: 'assets/shared.js'},
  };

  assert.deepEqual(
    collectManifestAssets(manifest, ['src/pages/Packs.tsx', 'src/pages/Flows.tsx']).scripts,
    ['assets/Packs.js', 'assets/shared.js', 'assets/Flows.js'],
  );
});
