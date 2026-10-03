import assert from 'node:assert/strict';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import test from 'node:test';

import {
  useRuntimeOperationInvocation,
  type RuntimeOperationInvoker,
} from './useRuntimeOperationInvocation';
import type {
  RuntimeOperationDescriptor,
  RuntimeSurfaceEnvelope,
} from '@/src/lib/runtimeSurface';
import {listMutationJournal} from '@/src/lib/mutationJournal';

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
}: {
  currentOperation: RuntimeOperationDescriptor;
  invoker: RuntimeOperationInvoker;
  payload?: Record<string, unknown>;
}) {
  const invocation = useRuntimeOperationInvocation(testEnvelope, currentOperation, invoker);
  return (
    <div>
      <button type="button" disabled={invocation.busy} onClick={() => void invocation.invoke(payload)}>Invoke</button>
      <span data-state="state">{invocation.state}</span>
      {invocation.error ? <span data-state="error">{invocation.error.code}</span> : null}
    </div>
  );
}

function createDom(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {dom, container, root: createRoot(container)};
}

test('runtime invocation binds completion to token and operation identity', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  let release: (() => void) | undefined;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  const calls: string[] = [];
  const invoker: RuntimeOperationInvoker = async ({operation: selected}) => {
    calls.push(selected.operation_id);
    await pending;
  };

  try {
    await act(async () => {
      root.render(<Probe currentOperation={operation('operation-a')} invoker={invoker} />);
    });
    const invokeButton = container.querySelector<HTMLButtonElement>('button');
    assert.ok(invokeButton);
    await act(async () => {
      invokeButton.click();
      invokeButton.click();
    });
    assert.deepEqual(calls, ['operation-a']);
    assert.equal(invokeButton.disabled, true);

    await act(async () => {
      root.render(<Probe currentOperation={operation('operation-b')} invoker={invoker} />);
    });
    release?.();
    await act(async () => pending);
    assert.equal(container.querySelector('[data-state="state"]')?.textContent, 'idle');
    assert.equal(container.querySelector('[data-state="error"]'), null);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
    });
  }
});

test('credential-bearing invocation persists only opaque identity through timeout and unmount', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousStorage = globalThis.localStorage;
  const values = new Map<string, string>();
  const writes: string[] = [];
  Object.defineProperty(globalThis, 'localStorage', {configurable: true, value: {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { writes.push(value); values.set(key, value); },
    removeItem: (key: string) => values.delete(key),
  }});
  const {dom, root, container} = createDom();
  const secret = 'synthetic-secret-do-not-persist';
  let requestId: string | undefined;
  const invoker: RuntimeOperationInvoker = async (request) => {
    assert.equal(request.payload.api_key, secret);
    requestId = request.requestId;
    throw new Error('POST request timed out after 10000ms');
  };
  try {
    await act(async () => {
      root.render(<Probe currentOperation={operation('credential-test')} invoker={invoker} payload={{api_key: secret}} />);
    });
    await act(async () => { container.querySelector<HTMLButtonElement>('button')!.click(); });
    assert.equal(container.querySelector('[data-state="state"]')?.textContent, 'unknown');
    const record = listMutationJournal().find((item) => item.requestId === requestId);
    assert.ok(record);
    assert.ok(record.key.endsWith(`:request:${requestId}`));
    assert.equal(record.state, 'unknown');
    assert.equal(writes.some((value) => value.includes(secret) || value.includes('api_key')), false);
    await act(async () => root.unmount());
    assert.equal([...values.values()].some((value) => value.includes(secret)), false);
    assert.equal(listMutationJournal().find((item) => item.requestId === requestId)?.state, 'unknown');
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      localStorage: {value: previousStorage, configurable: true},
    });
  }
});

test('another mounted caller cannot replace an unresolved operation with different input', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, root, container} = createDom();
  let release: (() => void) | undefined;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  let calls = 0;
  const invoker: RuntimeOperationInvoker = async () => { calls += 1; await pending; };
  const selected = operation(`same-operation-${Date.now()}`);
  try {
    await act(async () => root.render(<>
      <Probe currentOperation={selected} invoker={invoker} payload={{value: 'first'}} />
      <Probe currentOperation={selected} invoker={invoker} payload={{value: 'second'}} />
    </>));
    const buttons = container.querySelectorAll<HTMLButtonElement>('button');
    await act(async () => { buttons[0].click(); buttons[1].click(); });
    assert.equal(calls, 1);
    assert.equal(container.querySelectorAll('[data-state="state"]')[1].textContent, 'unknown');
    release?.();
    await act(async () => pending);
    assert.equal(container.querySelectorAll('[data-state="state"]')[1].textContent, 'idle');
    assert.equal(buttons[1].disabled, false);
    await act(async () => { buttons[1].click(); });
    assert.equal(calls, 2);
  } finally {
    release?.();
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
    });
  }
});

test('runtime invocation clears a completed result when the selected operation changes', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  const invoker: RuntimeOperationInvoker = async () => {};

  try {
    await act(async () => {
      root.render(<Probe currentOperation={operation('operation-a')} invoker={invoker} />);
    });
    const invokeButton = container.querySelector<HTMLButtonElement>('button');
    assert.ok(invokeButton);
    await act(async () => {
      invokeButton.click();
    });
    assert.equal(container.querySelector('[data-state="state"]')?.textContent, 'succeeded');

    await act(async () => {
      root.render(<Probe currentOperation={operation('operation-b')} invoker={invoker} />);
    });
    assert.equal(container.querySelector('[data-state="state"]')?.textContent, 'idle');
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
    });
  }
});

test('runtime invocation treats a lost response as unknown and rejects a replacement submit', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  let calls = 0;
  const timeoutOperation = operation(`operation-timeout-${Date.now()}`);
  const invoker: RuntimeOperationInvoker = async () => {
    calls += 1;
    throw new Error('POST request timed out after 10000ms');
  };

  try {
    await act(async () => {
      root.render(<Probe currentOperation={timeoutOperation} invoker={invoker} />);
    });
    const invokeButton = container.querySelector<HTMLButtonElement>('button');
    assert.ok(invokeButton);
    await act(async () => { invokeButton.click(); await Promise.resolve(); });
    assert.equal(calls, 1);
    assert.equal(container.querySelector('[data-state="state"]')?.textContent, 'unknown');
    assert.equal(invokeButton.disabled, true);

    await act(async () => { invokeButton.click(); await Promise.resolve(); });
    assert.equal(calls, 1);
    assert.equal(container.querySelector('[data-state="state"]')?.textContent, 'unknown');
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
    });
  }
});
