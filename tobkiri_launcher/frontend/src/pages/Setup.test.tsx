import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import test from 'node:test';

const source = readFileSync(resolve(import.meta.dirname, 'Setup.tsx'), 'utf8');
const appSource = readFileSync(resolve(import.meta.dirname, '..', 'App.tsx'), 'utf8');

test('panel setup does not bypass setup-pack installation', () => {
  assert.match(source, /hasSelectedSetupPack\(\)/);
  assert.match(source, /window\.location\.assign\(setupPackSelectionUrl\(undefined, colorMode\)\)/);
  assert.match(source, /tobkiri-launcher-icon\.png/);
  assert.doesNotMatch(
    source,
    /const handleSkip = \(\) => \{\s*setSetupDone\(true\);\s*navigate\(panelRoutes\.home\);/,
  );
});

test('panel entry shows Home first and verifies the setup pack in the background', () => {
  assert.match(appSource, /import \{ hasSelectedSetupPack \} from '@\/src\/lib\/setupPacks'/);
  assert.match(appSource, /requestIdleCallback/);
  assert.match(appSource, /void hasSelectedSetupPack\(\)[\s\S]*setSetupDone\(false\)/);
  assert.match(appSource, /element=\{isSetupDone \? <Layout \/> : <Navigate to=\{panelRoutes\.setup\} replace \/>\}/);
  assert.doesNotMatch(appSource, /function SetupVerificationGate\(\)/);
});
