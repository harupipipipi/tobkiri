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
    await writeFile(join(root, 'assets/shared.js'), 'export const shared = true');
    await writeFile(join(root, 'assets/Flows.js'), 'console.log("flows")');
    await writeFile(join(root, 'assets/main.css'), 'body{}');
    await writeFile(join(root, 'manifest.json'), JSON.stringify({
      'src/main.tsx': {file: 'assets/main.js', css: ['assets/main.css'], imports: ['shared'], isEntry: true},
      shared: {file: 'assets/shared.js'},
      'src/pages/Flows.tsx': {file: 'assets/Flows.js', imports: ['shared']},
    }));

    const {report, outputPath} = await measureBuild({distDir: root});
    assert.equal(report.initial_javascript.files.some((item) => item.file === 'assets/Flows.js'), false);
    assert.equal(report.initial_javascript.files.some((item) => item.file === 'assets/main.js'), true);
    assert.deepEqual(report.initial_css.files.map((item) => item.file), ['assets/main.css']);
    assert.equal(report.routes['src/pages/Flows.tsx'].present, true);
    assert.equal(JSON.parse(await readFile(outputPath, 'utf8')).entry, 'src/main.tsx');
  } finally {
    await rm(root, {recursive: true, force: true});
  }
});
