import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';

import {FileInspectOperation} from './FileInspectOperation';
import type {Pack, PackOperation} from '@/src/store';

const operation: PackOperation = {
  operationId: 'rumi_file_inspect_pack.file-inspect',
  contractId: 'tobkiri.service.file.inspect.v1',
  providerId: 'rumi_file_inspect_pack.file-inspect.service',
  capabilities: ['file.inspect'],
  inputSchema: {},
  invokable: true,
};

const samplePack: Pack = {
  id: 'rumi_file_inspect_pack',
  name: 'Tobkiri File Inspect',
  version: '1.0.0',
  type: 'community',
  installed: true,
  enabled: true,
  description: 'Inspect selected workspace files.',
  artifactDigest: 'sha256:artifact',
  profileId: 'profile-a',
  workspaceId: 'workspace-a',
  profileRevision: 'sha256:profile',
  planDigest: 'sha256:plan',
  catalogRevision: 'catalog-a',
  approvalStatus: 'approved',
  approvalReason: null,
  approved: true,
  hashValid: true,
  criticalChanged: false,
  approvalIssues: [],
  capabilities: [{name: 'file.inspect', description: 'Inspect files.'}],
  operations: [operation],
  flows: [operation.operationId],
  dependencies: [],
};

function createSurface(
  pack: Pack = samplePack,
  onInvoke: (payload: Record<string, unknown>) => Promise<unknown> = async () => ({ok: true}),
  pending = false,
): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/packs/rumi_file_inspect_pack',
  });
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  const root = createRoot(container);
  act(() => {
    root.render(
      <FileInspectOperation
        operation={operation}
        pack={pack}
        contributionVerified
        pending={pending}
        onInvoke={onInvoke}
      />,
    );
  });
  return {dom, container, root};
}

async function setInput(dom: JSDOM, container: HTMLElement, id: string, value: string): Promise<void> {
  await act(async () => {
    const input = container.querySelector<HTMLInputElement>(`#${id}`);
    assert.ok(input);
    input.value = value;
    input.dispatchEvent(new dom.window.Event('input', {bubbles: true}));
    input.dispatchEvent(new dom.window.Event('change', {bubbles: true}));
  });
}

async function submitForm(dom: JSDOM, container: HTMLElement, count = 1): Promise<void> {
  await act(async () => {
    const form = container.querySelector<HTMLFormElement>('form');
    assert.ok(form);
    for (let index = 0; index < count; index += 1) {
      form.dispatchEvent(new dom.window.Event('submit', {bubbles: true, cancelable: true}));
    }
  });
}

test('file inspection sends only safe typed input and renders the result', async () => {
  const calls: Record<string, unknown>[] = [];
  const {dom, container, root} = createSurface(samplePack, async (payload) => {
    calls.push(payload);
    return {kind: 'stat', path: 'docs/example.txt', size: 12};
  });

  try {
    await setInput(dom, container, 'file-inspect-path', 'docs/example.txt');
    await submitForm(dom, container);

    assert.deepEqual(calls, [{
      name: 'stat',
      path: 'docs/example.txt',
      profile_id: 'profile-a',
      workspace_id: 'workspace-a',
      require_selected: true,
    }]);
    assert.equal(Object.keys(calls[0]).some((key) => /secret|approved|binding/i.test(key)), false);
    assert.match(container.textContent ?? '', /Inspection result/);
    assert.match(container.textContent ?? '', /docs\/example\.txt/);
  } finally {
    act(() => root.unmount());
    dom.window.close();
  }
});

test('empty or malicious workspace paths are rejected before invocation', async () => {
  const calls: Record<string, unknown>[] = [];
  const {dom, container, root} = createSurface(samplePack, async (payload) => {
    calls.push(payload);
    return {ok: true};
  });

  try {
    assert.equal(container.querySelector<HTMLButtonElement>('button[type="submit"]')?.disabled, true);
    await act(async () => {
      container.querySelector<HTMLButtonElement>('button[type="submit"]')?.click();
    });
    assert.equal(calls.length, 0);
    assert.equal(
      container.querySelector<HTMLInputElement>('#file-inspect-path')?.validity.valid,
      false,
    );

    await setInput(dom, container, 'file-inspect-path', '../secrets.txt');
    await submitForm(dom, container);
    assert.equal(calls.length, 0);
    assert.match(container.textContent ?? '', /without absolute prefixes/);

    await setInput(dom, container, 'file-inspect-path', '/etc/passwd');
    await submitForm(dom, container);
    assert.equal(calls.length, 0);
  } finally {
    act(() => root.unmount());
    dom.window.close();
  }
});

test('typed capability failure is shown without a false success result', async () => {
  const {dom, container, root} = createSurface(samplePack, async () => {
    throw new Error('HTTP 409 capability_denied');
  });

  try {
    await setInput(dom, container, 'file-inspect-path', 'docs/example.txt');
    await submitForm(dom, container);
    assert.match(container.querySelector('[role="alert"]')?.textContent ?? '', /could not inspect that file/i);
    assert.match(container.querySelector('[role="alert"]')?.textContent ?? '', /UNEXPECTED_ERROR/);
    assert.doesNotMatch(container.querySelector('[role="alert"]')?.textContent ?? '', /capability_denied/);
    assert.equal(container.querySelector('pre[aria-live="polite"]'), null);
  } finally {
    act(() => root.unmount());
    dom.window.close();
  }
});

test('double submission is blocked while the Broker invocation is pending', async () => {
  let calls = 0;
  let release: (() => void) | undefined;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  const {dom, container, root} = createSurface(samplePack, async () => {
    calls += 1;
    await pending;
    return {ok: true};
  });

  try {
    await setInput(dom, container, 'file-inspect-path', 'docs/example.txt');
    await submitForm(dom, container, 2);
    assert.equal(calls, 1);
    assert.equal(container.querySelector<HTMLButtonElement>('button[type="submit"]')?.disabled, true);

    release?.();
    await act(async () => pending);
    assert.match(container.textContent ?? '', /Inspection result/);
  } finally {
    release?.();
    act(() => root.unmount());
    dom.window.close();
  }
});

test('revoked or disabled Packs cannot invoke the operation', async () => {
  let calls = 0;
  const revokedPack: Pack = {
    ...samplePack,
    enabled: false,
    approved: false,
    approvalStatus: 'revoked',
    approvalReason: 'approval_revoked',
    approvalIssues: ['approval_revoked'],
  };
  const {dom, container, root} = createSurface(revokedPack, async () => {
    calls += 1;
    return {ok: true};
  });

  try {
    const button = container.querySelector<HTMLButtonElement>('button[type="submit"]');
    assert.ok(button);
    assert.equal(button.disabled, true);
    assert.match(container.textContent ?? '', /approval is revoked/i);
    await act(async () => button.click());
    assert.equal(calls, 0);
  } finally {
    act(() => root.unmount());
    dom.window.close();
  }
});
