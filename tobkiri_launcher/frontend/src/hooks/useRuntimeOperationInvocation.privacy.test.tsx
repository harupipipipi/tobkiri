import assert from 'node:assert/strict';
import React, {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import test from 'node:test';

import {
  useRuntimeOperationInvocation,
  type RuntimeOperationInvoker,
  runtimeOperationIdentity,
} from './useRuntimeOperationInvocation';
import type {
  RuntimeOperationDescriptor,
  RuntimeSurfaceEnvelope,
} from '@/src/lib/runtimeSurface';
import {listMutationJournal, completeMutation, beginMutation, markMutationUnknown} from '@/src/lib/mutationJournal';
import {getRuntimeDispatchStatus, setRuntimeDispatchStatus} from '@/src/lib/runtimeDispatchGate';
import {PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST} from '@/src/lib/generatedFrontendContractMap';

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

function envelope(): RuntimeSurfaceEnvelope<unknown> {
  return {
    runtime_surface_api_version: 'io.tobkiri.launcher.runtime-surface.v4',
    surface: 'operations',
    state: 'ready',
    profile_id: 'profile-a',
    profile_revision: digest('a'),
    catalog_revision: digest('b'),
    plan_digest: digest('c'),
    records: {
      profile_lock: {digest: digest('d'), source_ref: 'profile-lock-v4://a'},
      resolved_plan: {digest: digest('e'), source_ref: 'resolved-plan-v1://a'},
      activation_record: {digest: digest('f'), source_ref: 'activation-record-v1://a'},
      authority_snapshot: {digest: digest('1'), source_ref: 'authority-snapshot-v4://a'},
    },
    data: {},
  };
}

function operation(id: string): RuntimeOperationDescriptor {
  return {
    action: 'contract_invoke',
    operation_id: id,
    contract_id: `${id}.contract`,
    owner_pack_id: 'pack-a',
    contribution_id: `${id}.contribution`,
    target_provider_id: 'provider-a',
    artifact_digest: digest('2'),
    invocation_contribution_id: `${id}.invoke`,
    invocation_owner_pack_id: 'pack-a',
    invocation_catalog_hash: digest('3'),
    invocation_reason: null,
    invokable: true,
    catalog_digest: digest('4'),
    activation_id: 'activation:profile-a',
    function_id: `${id}.function`,
    function_principal_id: `${id}.principal`,
    caller_function_id: `${id}.caller`,
    authority_reference: `authority://${id}`,
    schema: {input_schema: {type: 'object', properties: {}}},
    input_schema: {type: 'object', properties: {}},
    route: {
      contract_id: `${id}.contract`,
      operation_id: id,
      function_id: `${id}.function`,
      provider_pack_id: 'pack-a',
    },
  };
}

const testEnvelope = envelope();

function Probe({
  currentOperation,
  invoker,
  payload = {},
  capture,
}: {
  currentOperation: RuntimeOperationDescriptor;
  invoker: RuntimeOperationInvoker;
  payload?: Record<string, unknown>;
  capture?: (invocation: ReturnType<typeof useRuntimeOperationInvocation>) => void;
}) {
  const invocation = useRuntimeOperationInvocation(testEnvelope, currentOperation, invoker);
  capture?.(invocation);
  return (
    <div>
      <button type="button" disabled={invocation.busy} onClick={() => void invocation.invoke(payload)}>Invoke</button>
      <span data-state="state">{invocation.state}</span>
      {invocation.error ? <span data-state="error">{invocation.error.code}</span> : null}
    </div>
  );
}

function createDom(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {url: 'http://synthetic.local'});
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {dom, container, root: createRoot(container)};
}


function setupStorage(writeFails = false) {
  const previous = {localStorage: globalThis.localStorage, sessionStorage: globalThis.sessionStorage};
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      if (writeFails) throw new DOMException('Synthetic quota exhausted', 'QuotaExceededError');
      values.set(key, value);
    },
    removeItem: (key: string) => values.delete(key),
  };
  Object.defineProperty(globalThis, 'localStorage', {value: storage, configurable: true});
  Object.defineProperty(globalThis, 'sessionStorage', {value: undefined, configurable: true});
  return {values, restore: () => {
    Object.defineProperty(globalThis, 'localStorage', {value: previous.localStorage, configurable: true});
    Object.defineProperty(globalThis, 'sessionStorage', {value: previous.sessionStorage, configurable: true});
  }};
}

async function withProbe(callback: (context: ReturnType<typeof createDom>) => Promise<void>) {
  const previous = {
    window: globalThis.window, document: globalThis.document, fetch: globalThis.fetch,
    dispatch: getRuntimeDispatchStatus(), actEnvironment: globalThis.IS_REACT_ACT_ENVIRONMENT,
  };
  const context = createDom();
  setRuntimeDispatchStatus('runtime_ready');
  globalThis.fetch = async () => {throw new Error('Offline synthetic harness: unexpected fetch');};
  try {await callback(context);} finally {
    act(() => context.root.unmount()); context.dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previous.window, configurable: true},
      document: {value: previous.document, configurable: true},
    });
    globalThis.fetch = previous.fetch;
    globalThis.IS_REACT_ACT_ENVIRONMENT = previous.actEnvironment;
    setRuntimeDispatchStatus(previous.dispatch);
  }
}

function statusFor(requestId: string, selected: RuntimeOperationDescriptor, state = 'failed') {
  return {
    runtime_surface_api_version: 'io.tobkiri.launcher.runtime-surface.v4',
    operation_status_api_version: 'io.tobkiri.control-operation-status.v1',
    request_id: requestId, operation_id: selected.operation_id, contract_id: selected.contract_id,
    request_digest: digest('a'), state, result: null, result_digest: null, record_refs: [],
    safe_error_code: state === 'failed' ? 'SYNTHETIC_DENIED' : null, created_at: 1, updated_at: 2,
  };
}

function unknownFor(selected: RuntimeOperationDescriptor, requestId: string) {
  const key = `runtime:invoke:${runtimeOperationIdentity(testEnvelope, selected)}:request:${requestId}`;
  const record = beginMutation(key, {
    kind: 'runtime-operation-invocation', operation_id: selected.operation_id,
    contract_id: selected.contract_id, contract_map_digest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
  }, {primary: requestId});
  return markMutationUnknown(key, record.requestId);
}

function mockStatus(selected: RuntimeOperationDescriptor, reads: string[]) {
  globalThis.fetch = async (input) => {
    const requestId = new URL(String(input), 'http://synthetic.local').searchParams.get('request_id')!;
    assert.ok(requestId); reads.push(requestId);
    return new Response(JSON.stringify({success: true, data: statusFor(requestId, selected)}), {
      headers: {'Content-Type': 'application/json'},
    });
  };
}

// All requests and credential-shaped payloads in these regressions are synthetic and offline.
test('hydrated terminal failure must reach failed UI state', async () => {
  const storage = setupStorage();
  try {await withProbe(async ({root, container}) => {
    const selected = operation('repro-hydrated-terminal');
    const record = unknownFor(selected, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa');
    const reads: string[] = []; mockStatus(selected, reads);
    await act(async () => root.render(<Probe currentOperation={selected} invoker={async () => {}} />));
    assert.equal(reads.length, 1);
    assert.equal(listMutationJournal().some((entry) => entry.key === record.key), false);
    assert.equal(container.querySelector('[data-state="state"]')?.textContent, 'failed');
  });} finally {storage.restore();}
});

test('completion of first legacy request must retain blocker for second', async () => {
  const storage = setupStorage();
  try {await withProbe(async ({root, container}) => {
    const selected = operation('repro-multiple-legacy');
    const identity = runtimeOperationIdentity(testEnvelope, selected);
    const ids = ['bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'];
    storage.values.set('tobkiri-launcher-mutation-journal-v1', JSON.stringify(ids.map((requestId, index) => ({
      key: `runtime:invoke:${identity}:${JSON.stringify({api_key: 'SYNTHETIC_SENTINEL', index})}`,
      requestId, state: 'unknown', createdAt: index,
      metadata: {kind: 'runtime-operation-invocation', operation_id: selected.operation_id,
        contract_id: selected.contract_id, contract_map_digest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST},
    }))));
    const reads: string[] = [];
    globalThis.fetch = async (input) => {
      const requestId = new URL(String(input), 'http://synthetic.local').searchParams.get('request_id')!;
      reads.push(requestId);
      const status = statusFor(requestId, selected, requestId === ids[0] ? 'failed' : 'pending');
      return new Response(JSON.stringify({success: true, data: status}), {headers: {'Content-Type':'application/json'}});
    };
    await act(async () => root.render(<Probe currentOperation={selected} invoker={async () => {}} />));
    assert.deepEqual(reads, ids);
    assert.deepEqual(listMutationJournal().map((entry) => entry.requestId), [ids[1]]);
    assert.equal([...storage.values.values()].some((value) => value.includes('SYNTHETIC_SENTINEL')), false);
    assert.equal(container.querySelector<HTMLButtonElement>('button')?.disabled, true);
  });} finally {storage.restore();}
});

test('late timeout from A must not overwrite selection B', async () => {
  const storage = setupStorage();
  try {await withProbe(async ({root, container}) => {
    let reject: (error: Error) => void = () => {};
    const pending = new Promise<void>((_resolve, fail) => {reject = fail;});
    const invoker: RuntimeOperationInvoker = () => pending;
    const a = operation('repro-stale-a'); const b = operation('repro-stale-b');
    await act(async () => root.render(<Probe currentOperation={a} invoker={invoker} />));
    await act(async () => {container.querySelector<HTMLButtonElement>('button')!.click();});
    await act(async () => root.render(<Probe currentOperation={b} invoker={invoker} />));
    assert.equal(container.querySelector('[data-state="state"]')?.textContent, 'idle');
    await act(async () => {reject(new Error('Synthetic POST timeout')); await pending.catch(() => {});});
    assert.equal(listMutationJournal()[0]?.state, 'unknown');
    assert.equal(container.querySelector('[data-state="state"]')?.textContent, 'idle');
  });} finally {storage.restore();}
});

test('storage quota error must still block concurrent same-operation submit', async () => {
  const storage = setupStorage(true);
  try {await withProbe(async ({root, container}) => {
    const selected = operation('repro-quota-error');
    let release = () => {};
    const pending = new Promise<void>((resolve) => {release = resolve;});
    let calls = 0;
    const invoker: RuntimeOperationInvoker = async () => {calls += 1; await pending;};
    try {
      await act(async () => root.render(<>
        <Probe currentOperation={selected} invoker={invoker} payload={{value:'one'}} />
        <Probe currentOperation={selected} invoker={invoker} payload={{value:'two'}} />
      </>));
      const buttons = container.querySelectorAll<HTMLButtonElement>('button');
      await act(async () => {buttons[0].click(); buttons[1].click();});
      assert.equal(calls, 1);
    } finally {release(); await act(async () => pending);}
  });} finally {storage.restore();}
});

test('peer blocker retains actual request ID for manual reconciliation', async () => {
  const storage = setupStorage();
  try {await withProbe(async ({root, container}) => {
    const selected = operation('positive-peer-status');
    let reject = (_error: Error) => {};
    const pending = new Promise<void>((_resolve, fail) => {reject = fail;});
    let calls = 0; let submittedRequest: string | undefined;
    const invoker: RuntimeOperationInvoker = (request) => {calls += 1; submittedRequest = request.requestId; return pending;};
    let latest: ReturnType<typeof useRuntimeOperationInvocation> | undefined;
    function CapturedProbe() {
      const invocation = useRuntimeOperationInvocation(testEnvelope, selected, invoker);
      latest = invocation;
      return <button disabled={invocation.busy} onClick={() => void invocation.invoke({synthetic:'second'})}>second</button>;
    }
    await act(async () => root.render(<><Probe currentOperation={selected} invoker={invoker} /><CapturedProbe /></>));
    const buttons = container.querySelectorAll<HTMLButtonElement>('button');
    await act(async () => {buttons[0].click(); buttons[1].click();});
    assert.equal(calls, 1);
    assert.equal(latest?.state, 'unknown');
    const oldRecord = listMutationJournal()[0];
    assert.equal(oldRecord.requestId, submittedRequest);
    await act(async () => {reject(new Error('Synthetic timeout')); await pending.catch(() => {});});
    const reads: string[] = []; mockStatus(selected, reads);
    await act(async () => {await latest?.reconcileUnknown();});
    assert.deepEqual(reads, [submittedRequest]);
    assert.equal(calls, 1);
    assert.equal(listMutationJournal().length, 0);
    assert.equal(latest?.state, 'idle');
  });} finally {storage.restore();}
});

test('different operation identity can run while first operation remains pending', async () => {
  const storage = setupStorage();
  try {await withProbe(async ({root, container}) => {
    let release = () => {};
    const pending = new Promise<void>((resolve) => {release = resolve;});
    const calls: string[] = [];
    const invoker: RuntimeOperationInvoker = async (request) => {calls.push(request.operation.operation_id); await pending;};
    try {
      await act(async () => root.render(<>
        <Probe currentOperation={operation('positive-identity-a')} invoker={invoker} />
        <Probe currentOperation={operation('positive-identity-b')} invoker={invoker} />
      </>));
      const buttons = container.querySelectorAll<HTMLButtonElement>('button');
      await act(async () => {buttons[0].click(); buttons[1].click();});
      assert.deepEqual(calls, ['positive-identity-a', 'positive-identity-b']);
      assert.equal(listMutationJournal().length, 2);
      assert.ok(listMutationJournal().every((record) => record.key.endsWith(`:request:${record.requestId}`)));
      assert.notEqual(listMutationJournal()[0].requestId, listMutationJournal()[1].requestId);
    } finally {release(); await act(async () => pending);}
    assert.equal(listMutationJournal().length, 0);
  });} finally {storage.restore();}
});

test('hydrated terminal success remains succeeded without replay', async () => {
  const storage = setupStorage();
  try {await withProbe(async ({root, container}) => {
    const selected = operation('positive-hydrated-success');
    const record = unknownFor(selected, 'dddddddd-dddd-4ddd-8ddd-dddddddddddd');
    let calls = 0;
    let latest: ReturnType<typeof useRuntimeOperationInvocation> | undefined;
    globalThis.fetch = async () => {
      const result = {state:'synthetic-complete'};
      const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(result)));
      const resultDigest = `sha256:${Array.from(new Uint8Array(hash)).map((value) => value.toString(16).padStart(2,'0')).join('')}`;
      const status = {...statusFor(record.requestId, selected, 'succeeded'), result, result_digest:resultDigest};
      return new Response(JSON.stringify({success:true,data:status}), {headers:{'Content-Type':'application/json'}});
    };
    await act(async () => root.render(<Probe currentOperation={selected} invoker={async () => {calls += 1;}} capture={(invocation) => {latest = invocation;}} />));
    // Await the status promise explicitly: Web Crypto finishes outside React's act queue.
    await act(async () => {await latest?.reconcileUnknown();});
    assert.equal(container.querySelector('[data-state="state"]')?.textContent, 'succeeded');
    assert.equal(container.querySelector('[data-state="error"]'), null);
    assert.equal(calls,0);
    assert.equal(listMutationJournal().length,0);
  });} finally {storage.restore();}
});

test('selection during hydrated status read keeps terminal result away from new operation', async () => {
  const storage = setupStorage();
  try {await withProbe(async ({root, container}) => {
    const a = operation('positive-reconcile-select-a'); const b = operation('positive-reconcile-select-b');
    const record = unknownFor(a, 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee');
    let release = () => {};
    const pending = new Promise<void>((resolve) => {release = resolve;});
    globalThis.fetch = async () => {
      await pending;
      return new Response(JSON.stringify({success:true,data:statusFor(record.requestId,a)}), {headers:{'Content-Type':'application/json'}});
    };
    const invoker: RuntimeOperationInvoker = async () => {};
    await act(async () => root.render(<Probe currentOperation={a} invoker={invoker} />));
    assert.equal(container.querySelector('[data-state="state"]')?.textContent, 'unknown');
    await act(async () => root.render(<Probe currentOperation={b} invoker={invoker} />));
    assert.equal(container.querySelector('[data-state="state"]')?.textContent, 'idle');
    await act(async () => {release(); await pending;});
    assert.equal(container.querySelector('[data-state="state"]')?.textContent, 'idle');
    assert.equal(container.querySelector('[data-state="error"]'), null);
    assert.equal(listMutationJournal().length,0);
  });} finally {storage.restore();}
});

test('failed-write memory snapshot flushes exact unknown record when storage recovers', () => {
  const previous = {localStorage:globalThis.localStorage,sessionStorage:globalThis.sessionStorage};
  const values = new Map<string,string>(); let failed = true;
  const storage = {
    getItem:(key:string) => values.get(key) ?? null,
    setItem:(key:string,value:string) => {if(failed) throw new DOMException('Synthetic quota','QuotaExceededError'); values.set(key,value);},
    removeItem:(key:string) => values.delete(key),
  };
  Object.defineProperty(globalThis,'localStorage',{value:storage,configurable:true});
  Object.defineProperty(globalThis,'sessionStorage',{value:undefined,configurable:true});
  try {
    const selected=operation('positive-fallback-recovery');
    const record=unknownFor(selected,'ffffffff-ffff-4fff-8fff-ffffffffffff');
    assert.equal(listMutationJournal()[0]?.state,'unknown');
    assert.equal(listMutationJournal()[0]?.requestId,record.requestId);
    failed=false;
    assert.equal(listMutationJournal()[0]?.requestId,record.requestId);
    assert.ok([...values.values()].some((value) => value.includes(record.requestId)));
    const secondStorage={...storage};
    Object.defineProperty(globalThis,'localStorage',{value:secondStorage,configurable:true});
    assert.equal(listMutationJournal()[0]?.state,'unknown');
    completeMutation(record.key,record.requestId);
    assert.equal(listMutationJournal().length,0);
    assert.ok([...values.values()].every((value) => value==='[]'));
  } finally {
    Object.defineProperty(globalThis,'localStorage',{value:previous.localStorage,configurable:true});
    Object.defineProperty(globalThis,'sessionStorage',{value:previous.sessionStorage,configurable:true});
  }
});
