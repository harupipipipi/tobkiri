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
}: {
  currentOperation: RuntimeOperationDescriptor;
  invoker: RuntimeOperationInvoker;
}) {
  const invocation = useRuntimeOperationInvocation(testEnvelope, currentOperation, invoker);
  return (
    <div>
      <button type="button" disabled={invocation.busy} onClick={() => void invocation.invoke({})}>Invoke</button>
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
