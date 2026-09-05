import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtemp, mkdir, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

import {measureBuild} from './measure-build.mjs';

test('measureBuild separates initial JavaScript, CSS, and lazy route chunks', async () => {
  const root = await mkdtemp(join(tmpdir(), 'tobkiri-build-metrics-'));
  try {
    await mkdir(join(root, 'assets'), {recursive: true});
    await writeFile(join(root, 'assets/main.js'), 'console.log("main")');
    await writeFile(join(root, 'assets/extra.js'), 'console.log("extra")');
    await writeFile(join(root, 'assets/shared.js'), 'export const shared = true');
    await writeFile(join(root, 'assets/Packs.js'), 'console.log("packs")');
    await writeFile(join(root, 'assets/main.css'), 'body{}');
    await writeFile(join(root, 'manifest.json'), JSON.stringify({
      'src/main.tsx': {file: 'assets/main.js', css: ['assets/main.css'], imports: ['shared', 'extra'], isEntry: true},
      extra: {file: 'assets/extra.js'},
      shared: {file: 'assets/shared.js'},
      'src/pages/Packs.tsx': {file: 'assets/Packs.js', imports: ['shared']},
    }));

    const {report, outputPath} = await measureBuild({distDir: root});
    assert.equal(report.initial_javascript.files.some((item) => item.file === 'assets/Packs.js'), false);
    assert.equal(report.initial_javascript.files.some((item) => item.file === 'assets/main.js'), true);
    assert.deepEqual(report.initial_css.files.map((item) => item.file), ['assets/main.css']);
    assert.equal(report.routes['src/pages/Packs.tsx'].present, true);
    assert.deepEqual(Object.keys(report.routes), [
      'src/pages/Setup.tsx',
      'src/pages/Dashboard.tsx',
      'src/pages/Packs.tsx',
      'src/pages/PackDetail.tsx',
      'src/pages/Profile.tsx',
      'src/pages/Settings.tsx',
      'src/pages/ProfileWiring.tsx',
      'src/pages/ProfileFiles.tsx',
      'src/pages/Flow.tsx',
      'src/pages/Graph.tsx',
      'src/pages/AiInput.tsx',
      'src/pages/ApiMap.tsx',
      'src/pages/NodeManager.tsx',
    ]);
    const firstReport = await readFile(outputPath, 'utf8');
    assert.equal(JSON.parse(firstReport).entry, 'src/main.tsx');
    assert.equal('generated_at' in report, false);

    const manifest = JSON.parse(await readFile(join(root, 'manifest.json'), 'utf8'));
    const reorderedManifest = Object.fromEntries(
      Object.entries(manifest)
        .reverse()
        .map(([key, entry]) => [key, {
          ...entry,
          imports: entry.imports ? [...entry.imports].reverse() : entry.imports,
        }]),
    );
    await writeFile(join(root, 'manifest.json'), JSON.stringify(reorderedManifest));
    await measureBuild({distDir: root});
    assert.equal(await readFile(outputPath, 'utf8'), firstReport);
  } finally {
    await rm(root, {recursive: true, force: true});
  }
});
