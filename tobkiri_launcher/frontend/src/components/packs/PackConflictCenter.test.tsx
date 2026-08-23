import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {renderToStaticMarkup} from 'react-dom/server';
import {JSDOM} from 'jsdom';

import type {PackConflictReport} from '@/src/lib/apiTypes';
import type {PackRepairAction} from '@/src/store';
import {PackConflictCenter} from './PackConflictCenter';

function conflict(state?: NonNullable<PackConflictReport['repair']>['state']): PackConflictReport {
  const repair = state ? {
    repair_id: 'rpr_1234567890abcdef12345678',
    artifact_hash: `sha256:${'c'.repeat(64)}`,
    state: state as NonNullable<PackConflictReport['repair']>['state'],
    capability_delta: [],
    validation_passed: ['validated', 'approved', 'installed', 'active'].includes(state),
    dry_run_resolved: ['validated', 'approved', 'installed', 'active'].includes(state),
    warnings: [],
    approval_actor_id: ['approved', 'installed', 'active'].includes(state) ? 'reviewer.one' : null,
  } : null;
  return {
    conflict_api_version: 'io.tobkiri.pack-conflict-report.v1',
    conflict_id: 'pcf_1234567890abcdef12345678',
    kind: 'ambiguous_one_provider',
    profile_id: 'fixture.profile',
    profile_fingerprint: `sha256:${'d'.repeat(64)}`,
    involved_packs: [
      {pack_id: 'fixture.alpha', version: '1.0.0', artifact_hash: `sha256:${'a'.repeat(64)}`},
      {pack_id: 'fixture.beta', version: '1.0.0', artifact_hash: `sha256:${'b'.repeat(64)}`},
    ],
    affected_contracts: ['rumi.action.fixture.v1'],
    affected_resources: [],
    schemas: [],
    constraints: ['>=1.0.0 <2.0.0'],
    safe_repair_kinds: ['provider_selection'],
    repairable: true,
    diagnostics: ['Two providers have equal priority.'],
    validation_requirements: ['dry_run_resolution'],
    repair,
  };
}

test('Pack conflict center exposes an inspectable staged repair ceremony', {concurrency: false}, async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  const root = createRoot(container);
  const actions: PackRepairAction[] = [];
  await act(async () => {
    root.render(<PackConflictCenter conflicts={[conflict('active')]} pending={{}} onAction={async (_id, action) => { actions.push(action); }} />);
  });

  try {
    assert.match(container.textContent ?? '', /1 blocked conflict · 1 potentially repairable/);
    assert.match(container.textContent ?? '', /Generated repair · active/);
    assert.match(container.textContent ?? '', /Validation passed · Conflict resolved in dry run/);
    assert.match(container.textContent ?? '', /Capability \/ permission deltaNone/);
    assert.match(container.textContent ?? '', /Reviewed by reviewer.one/);
    const buttons = [...container.querySelectorAll<HTMLButtonElement>('button')];
    assert.deepEqual(buttons.map((button) => button.textContent?.trim()), ['Disable', 'Regenerate', 'Remove']);
    assert.ok(buttons.every((button) => button.className.includes('min-h-11')));
    await act(async () => buttons[1].click());
    assert.deepEqual(actions, ['regenerate']);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
  }
});

test('Pack conflict center separates generation, approval, install and activation', () => {
  const cases = [
    [undefined, 'Generate repair pack'],
    ['generated', 'Review generated repair'],
    ['validated', 'Approve'],
    ['approved', 'Install'],
    ['installed', 'Activate'],
  ] as const;
  for (const [state, label] of cases) {
    const html = renderToStaticMarkup(
      <PackConflictCenter conflicts={[conflict(state)]} pending={{}} onAction={async () => {}} />,
    );
    assert.match(html, new RegExp(label));
  }
});
