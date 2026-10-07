import test from 'node:test';
import assert from 'node:assert/strict';
import { parseFileEditReceipt, buildFileEditTimelineEntries } from './fileEditTimeline';
const receipt = (overrides: Record<string, unknown> = {}) => ({
  schema_version: 1, receipt_id: 'file-edit:a', file_id: 'file-1',
  status: 'committed', operation: 'patch', profile_id: 'profile-1',
  workspace_id: 'workspace-1', root_id: 'opaque-root-1', frame_id: null,
  path: 'src/main.ts', previous_path: null, occurred_at_ms: 100,
  sequence: 1, version: 'sha256:synthetic',
  stats: { status: 'available', lines_added: 2, lines_deleted: 1 }, ...overrides,
});
test('accepts committed receipts and exposes exact metadata without source objects', () => {
  const source = receipt();
  const parsed = parseFileEditReceipt(source)!;
  assert.deepEqual(parsed, source);
  assert.notEqual(parsed, source);
  assert.notEqual(parsed.stats, source.stats);
  assert.deepEqual(buildFileEditTimelineEntries([source])[0], {
    eventId: 'file-edit:a', fileId: 'file-1', path: 'src/main.ts', timestamp: 100,
    order: 1, addedLines: 2, deletedLines: 1, operation: 'patch', status: 'committed',
    profileId: 'profile-1', workspaceId: 'workspace-1', rootId: 'opaque-root-1',
    frameId: null, version: 'sha256:synthetic', previousPath: null,
  });
});
test('rejects missing, additional and invalid public fields and unconfirmed tools', () => {
  const valid = receipt();
  for (const key of Object.keys(valid)) {
    const missing: Record<string, unknown> = { ...valid }; delete missing[key];
    assert.equal(parseFileEditReceipt(missing), null, key);
  }
  for (const overrides of [
    { content: 'private' }, { credential_handle: 'private' }, { status: 'failed' },
    { status: 'denied' }, { status: 'pending' }, { operation: 'read' }, { operation: 'list' },
    { operation: ['patch'] }, { schema_version: 2 }, { receipt_id: 'arbitrary' },
    { receipt_id: 'file-edit:' }, { file_id: '' }, { workspace_id: null },
    { profile_id: true }, { root_id: '' }, { frame_id: 1 }, { version: '' },
    { path: '../secret' }, { path: '/absolute' }, { path: 'C:/secret' },
    { path: 'a\\b' }, { path: 'a/./b' }, { path: 'a//b' }, { path: 'a\nfile' },
    { previous_path: 'old.ts' }, { operation: 'move', previous_path: null },
    { operation: 'move', previous_path: 'src/main.ts' },
    ...[-1, true, null, NaN, Infinity, 2 ** 53, 1.5].flatMap(value => [
      { sequence: value }, { occurred_at_ms: value },
    ]),
  ]) assert.equal(parseFileEditReceipt(receipt(overrides)), null, JSON.stringify(overrides));
  for (const value of [null, [], 'tool output', false]) assert.equal(parseFileEditReceipt(value), null);
});
test('counts are strict and unavailable stats never invent zero', () => {
  for (const value of [-1, true, null, NaN, Infinity, 2 ** 53, 0.1]) {
    for (const field of ['lines_added', 'lines_deleted']) {
      assert.equal(parseFileEditReceipt(receipt({ stats: {
        status: 'available', lines_added: 0, lines_deleted: 0, [field]: value,
      } })), null);
    }
  }
  for (const reason of ['unknown', 'binary', 'too_large', 'sensitive', 'unsupported_encoding']) {
    const [entry] = buildFileEditTimelineEntries([receipt({ stats: { status: 'unavailable', reason } })]);
    assert.equal(entry.addedLines, null); assert.equal(entry.deletedLines, null);
    assert.equal(entry.statsReason, reason);
  }
  for (const stats of [
    { status: 'available', lines_added: 0 },
    { status: 'available', lines_added: 0, lines_deleted: 0, content: 'private' },
    { status: 'unavailable', reason: 'other' },
    { status: 'unavailable', reason: ['binary'] },
    { status: 'unavailable', reason: 'binary', lines_added: 0 },
  ]) assert.equal(parseFileEditReceipt(receipt({ stats })), null);
});
test('replayed receipts dedupe independently of property order; contradictory ids remain discarded', () => {
  const a = receipt();
  const reversed = Object.fromEntries(Object.entries(a).reverse());
  assert.equal(buildFileEditTimelineEntries([a, reversed, a]).length, 1);
  for (const conflict of [receipt({ version: 'changed' }), receipt({ workspace_id: 'other' }),
    receipt({ stats: { status: 'available', lines_added: 3, lines_deleted: 1 } })]) {
    assert.deepEqual(buildFileEditTimelineEntries([a, conflict, a]), []);
    assert.deepEqual(buildFileEditTimelineEntries([conflict, a]), []);
  }
});
test('same path saves, create delete rename and foreign scopes remain independent in stable order', () => {
  const rows = [
    receipt({ receipt_id: 'file-edit:d', operation: 'delete', sequence: 4 }),
    receipt({ receipt_id: 'file-edit:c', operation: 'move', previous_path: 'old.ts', sequence: 3 }),
    receipt({ receipt_id: 'file-edit:b', operation: 'write', sequence: 2 }),
    receipt({ receipt_id: 'file-edit:a', operation: 'create', sequence: 1 }),
    receipt({ receipt_id: 'file-edit:e', workspace_id: 'other', file_id: 'other', sequence: 4 }),
    receipt({ receipt_id: 'file-edit:f', root_id: 'other', profile_id: 'other', frame_id: 'frame', sequence: 4, occurred_at_ms: 99 }),
  ];
  const expected = ['file-edit:a', 'file-edit:b', 'file-edit:c', 'file-edit:f', 'file-edit:d', 'file-edit:e'];
  assert.deepEqual(buildFileEditTimelineEntries(rows).map(entry => entry.eventId), expected);
  assert.deepEqual(buildFileEditTimelineEntries([...rows].reverse()).map(entry => entry.eventId), expected);
  assert.equal(buildFileEditTimelineEntries(rows)[2].previousPath, 'old.ts');
});

test('identity and jailed path boundaries agree with backend 1024 character maximum', () => {
  for (const key of ['file_id', 'profile_id', 'workspace_id', 'root_id', 'frame_id', 'version', 'path']) {
    assert.ok(parseFileEditReceipt(receipt({ [key]: 'a'.repeat(1024) })), key);
    assert.equal(parseFileEditReceipt(receipt({ [key]: 'a'.repeat(1025) })), null, key);
  }
  assert.ok(parseFileEditReceipt(receipt({ receipt_id: 'file-edit:' + 'a'.repeat(1014) })));
  assert.equal(parseFileEditReceipt(receipt({ receipt_id: 'file-edit:' + 'a'.repeat(1015) })), null);
  assert.ok(parseFileEditReceipt(receipt({ operation: 'move', previous_path: 'b'.repeat(1024) })));
  assert.equal(parseFileEditReceipt(receipt({ operation: 'move', previous_path: 'b'.repeat(1025) })), null);
});

import { parseFileEditReceiptFromToolResult } from './fileEditTimeline';
const createdResult = () => {
  const confirmed = receipt({ operation: 'create' });
  return { created: true, path: confirmed.path, workspace_id: confirmed.workspace_id,
    file_edit_receipt: confirmed };
};
test('finite create hook accepts actual owner JSON in canonical progress and nested wrappers', () => {
  const owner = createdResult();
  for (const value of [owner, { data: owner }, { result: { data: owner } },
    { status: 'success', result: JSON.stringify(owner), error: null }]) {
    assert.deepEqual(parseFileEditReceiptFromToolResult('coding_file_create', value), owner.file_edit_receipt);
    assert.deepEqual(parseFileEditReceiptFromToolResult('rumi_default_tools_pack:coding_file_create', value), owner.file_edit_receipt);
  }
});
test('hook refuses unknown read list tools and inference from generic artifacts', () => {
  for (const tool of ['unknown', 'coding_file_read', 'coding_file_list', 'coding_file_write']) {
    assert.equal(parseFileEditReceiptFromToolResult(tool, createdResult()), null);
  }
  for (const value of [{ created: true, path: 'src/main.ts', size: 10, diff: '+line' },
    { content: JSON.stringify(createdResult()) }, ['created', createdResult()],
    Object.create(createdResult())]) {
    assert.equal(parseFileEditReceiptFromToolResult('coding_file_create', value), null);
  }
});
test('hook rejects failed pending and approval state at every known wrapper layer', () => {
  for (const state of ['failed', 'error', 'failure', 'denied', 'rejected', 'cancelled',
    'canceled', 'pending', 'running', 'queued', 'started', 'approval_required',
    'pending_approval', 'requires_approval', 'ambiguous', 'stale']) {
    for (const key of ['status', 'state', 'phase', 'outcome']) {
      assert.equal(parseFileEditReceiptFromToolResult('coding_file_create', {
        [key]: state, result: createdResult(),
      }), null);
      assert.equal(parseFileEditReceiptFromToolResult('coding_file_create', {
        data: { ...createdResult(), [key]: state },
      }), null);
    }
  }
  for (const flags of [{ is_error: true }, { success: false }, { ok: false },
    { approval_required: true }, { requires_approval: true }, { cancelled: true },
    { error: 'failure' }]) {
    assert.equal(parseFileEditReceiptFromToolResult('coding_file_create', { ...flags, result: createdResult() }), null);
  }
});
test('hook refuses owner identity mismatches invalid receipt ambiguous branches and bounds', () => {
  for (const overrides of [{ created: false }, { path: 'different' },
    { workspace_id: 'different' }, { file_edit_receipt: receipt() },
    { file_edit_receipt: { ...receipt({ operation: 'create' }), content: 'private' } }]) {
    assert.equal(parseFileEditReceiptFromToolResult('coding_file_create', { ...createdResult(), ...overrides }), null);
  }
  let nested: unknown = createdResult();
  for (let i = 0; i < 5; i++) nested = { data: nested };
  assert.ok(parseFileEditReceiptFromToolResult('coding_file_create', nested));
  assert.equal(parseFileEditReceiptFromToolResult('coding_file_create', { data: nested }), null);
  assert.equal(parseFileEditReceiptFromToolResult('coding_file_create', ' '.repeat(16385)), null);
  assert.equal(parseFileEditReceiptFromToolResult('coding_file_create', '{broken'), null);
  assert.equal(parseFileEditReceiptFromToolResult('coding_file_create', {
    data: createdResult(), result: { ...createdResult(), file_edit_receipt: receipt({ operation: 'create', receipt_id: 'file-edit:other' }) },
  }), null);
});
