import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { saveModelAccessEffect, type ModelAccessEffectPorts, type ModelAccessSaveRequest } from './modelAccessEffect';
import type { ProviderConfigurationStatus } from './providerConfiguration';
if (!globalThis.crypto) Object.defineProperty(globalThis, 'crypto', { value: webcrypto });
const request: ModelAccessSaveRequest = { profile_id: 'defaults', provider_instance_id: 'connection/main',
  expected_revision: 1, model_access: { version: 'tobkiri.connection-model-access.v1', mode: 'explicit', model_ids: ['model-a'] } };
const state = (value: string, id = 'effect-1'): ProviderConfigurationStatus => ({
  effect_id: id, state: value, approval_request_id: 'approval-1',
});
function harness() {
  const values = new Map<string, string>();
  const calls = { prepare: 0, lookup: 0, status: 0, resume: 0, cancel: 0, approval: 0, open: 0 };
  const ports: ModelAccessEffectPorts = {
    storage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => { values.set(key, value); }, removeItem: key => { values.delete(key); } },
    prepare: async () => { calls.prepare++; return state('prepared'); },
    lookup: async () => { calls.lookup++; return state('prepared'); },
    status: async () => { calls.status++; return state('succeeded'); },
    resume: async () => { calls.resume++; return state('succeeded'); },
    cancel: async () => { calls.cancel++; return state('cancelled'); },
    approval: async id => { calls.approval++; return { request_id: id, state: 'approved' }; },
    openApproval: async () => { calls.open++; return true; }, pause: async () => {},
  };
  return { values, calls, ports };
}
test('successful approved save prepares and resumes exact effect once, then clears pending', async () => {
  const h = harness();
  await saveModelAccessEffect(request, h.ports);
  assert.equal(h.calls.prepare, 1); assert.equal(h.calls.resume, 1);
  assert.equal(h.values.size, 0);
});
test('denied approval cancels effect and never resumes', async () => {
  const h = harness();
  h.ports.approval = async id => ({ request_id: id, state: 'denied' });
  await assert.rejects(saveModelAccessEffect(request, h.ports), /承認されていません/);
  assert.equal(h.calls.cancel, 1); assert.equal(h.calls.resume, 0);
  assert.equal(h.values.size, 0);
});
test('lost prepare response reconciles by correlation without duplicate prepare or raw error', async () => {
  const h = harness();
  h.ports.prepare = async () => { h.calls.prepare++; throw new Error('synthetic-private-endpoint/token'); };
  await assert.rejects(saveModelAccessEffect(request, h.ports), error => {
    assert.match(String(error), /保存結果を確認できません/);
    assert.doesNotMatch(String(error), /synthetic-private/); return true;
  });
  assert.equal(h.values.size, 1);
  await saveModelAccessEffect(request, h.ports);
  assert.equal(h.calls.prepare, 1); assert.equal(h.calls.lookup, 1);
  assert.equal(h.calls.resume, 1); assert.equal(h.values.size, 0);
});
test('pending scope or request changes reject without preparing looking up or resuming new work', async () => {
  for (const change of [{ profile_id: 'other' }, { provider_instance_id: 'other' },
    { expected_revision: 2 }, { model_access: { version: 'tobkiri.connection-model-access.v1' as const, mode: 'explicit' as const, model_ids: [] } }]) {
    const h = harness();
    h.ports.prepare = async () => { h.calls.prepare++; throw new Error('lost'); };
    await assert.rejects(saveModelAccessEffect(request, h.ports));
    await assert.rejects(saveModelAccessEffect({ ...request, ...change }, h.ports), /前のモデル許可設定/);
    assert.equal(h.calls.prepare, 1); assert.equal(h.calls.lookup, 0); assert.equal(h.calls.resume, 0);
  }
});
test('lost resume response polls uncertain effect without another resume even when status remains approved', async () => {
  const h = harness();
  h.ports.resume = async () => { h.calls.resume++; throw new Error('lost-private-response'); };
  await assert.rejects(saveModelAccessEffect(request, h.ports), /保存結果を確認できません/);
  let statusCalls = 0;
  h.ports.status = async () => { h.calls.status++; return state(++statusCalls === 1 ? 'approved' : 'succeeded'); };
  await saveModelAccessEffect(request, h.ports);
  assert.equal(h.calls.prepare, 1); assert.equal(h.calls.resume, 1);
  assert.equal(h.values.size, 0);
});
test('approval and effect identity mismatches stop before unauthorized continuation', async () => {
  const approval = harness();
  approval.ports.approval = async () => ({ request_id: 'foreign-approval', state: 'approved' });
  await assert.rejects(saveModelAccessEffect(request, approval.ports), /承認IDが一致しません/);
  assert.equal(approval.calls.resume, 0); assert.equal(approval.calls.cancel, 0);
  const effect = harness();
  effect.ports.resume = async () => { effect.calls.resume++; return state('succeeded', 'foreign-effect'); };
  await assert.rejects(saveModelAccessEffect(request, effect.ports), /操作IDが一致しません/);
  assert.equal(effect.values.size, 1);
});

test('changed request after lost resume reconciles terminal old effect without sending new input and permits future explicit save', async () => {
  for (const terminal of ['succeeded', 'cancelled']) {
    const h = harness();
    h.ports.resume = async () => { h.calls.resume++; throw new Error('lost'); };
    await assert.rejects(saveModelAccessEffect(request, h.ports));
    h.ports.status = async effect => { h.calls.status++; assert.equal(effect, 'effect-1'); return state(terminal); };
    const changed = { ...request, expected_revision: 2,
      model_access: { ...request.model_access, model_ids: ['model-b'] } };
    await assert.rejects(saveModelAccessEffect(changed, h.ports), /前の保存.*(完了|取消済み).*新しい入力は送信していません/);
    assert.equal(h.calls.status, 1); assert.equal(h.calls.prepare, 1); assert.equal(h.calls.resume, 1);
    assert.equal(h.values.size, 0);
    h.ports.resume = async () => { h.calls.resume++; return state('succeeded'); };
    await saveModelAccessEffect(changed, h.ports);
    assert.equal(h.calls.prepare, 2); assert.equal(h.calls.resume, 2);
  }
});
test('changed request reconciliation preserves uncertain pending and never reads foreign scope', async () => {
  for (const outcome of ['approved', 'claimed', 'dispatched', 'failed', 'foreign', 'error']) {
    const h = harness();
    h.ports.resume = async () => { h.calls.resume++; throw new Error('lost'); };
    await assert.rejects(saveModelAccessEffect(request, h.ports));
    const pending = [...h.values.values()][0];
    h.ports.status = async () => { h.calls.status++;
      if (outcome === 'error') throw new Error('synthetic-private');
      return state(outcome === 'foreign' ? 'succeeded' : outcome, outcome === 'foreign' ? 'foreign' : 'effect-1');
    };
    await assert.rejects(saveModelAccessEffect({ ...request, expected_revision: 2 }, h.ports), error => {
      assert.doesNotMatch(String(error), /synthetic-private/); return true;
    });
    assert.equal(h.calls.status, 1); assert.equal([...h.values.values()][0], pending);
    assert.equal(h.calls.prepare, 1); assert.equal(h.calls.resume, 1);
    await assert.rejects(saveModelAccessEffect({ ...request, profile_id: 'other', expected_revision: 2 }, h.ports));
    await assert.rejects(saveModelAccessEffect({ ...request, provider_instance_id: 'other', expected_revision: 2 }, h.ports));
    assert.equal(h.calls.status, 1);
  }
});
