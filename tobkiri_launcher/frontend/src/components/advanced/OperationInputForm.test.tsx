import assert from 'node:assert/strict';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import test from 'node:test';

import {OperationInputForm} from './OperationInputForm';
import {LAUNCHER_ADVANCED_VIEWS} from '@/src/lib/advancedSurfaces';
import type {RuntimeOperationDescriptor} from '@/src/lib/runtimeSurface';

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

function operation(invokable = true): RuntimeOperationDescriptor {
  return {
    action: 'contract_invoke',
    operation_id: 'conversation.turn',
    contract_id: 'conversation.v1',
    owner_pack_id: 'conversation-pack',
    contribution_id: 'conversation-contribution',
    target_provider_id: 'tobkiri.provider',
    artifact_digest: digest('a'),
    invocation_contribution_id: invokable ? 'conversation-invocation' : null,
    invocation_owner_pack_id: invokable ? 'conversation-pack' : null,
    invocation_catalog_hash: invokable ? digest('c') : null,
    invocation_reason: invokable ? null : 'Host readiness attestation is stale.',
    invokable,
    catalog_digest: digest('c'),
    function_id: 'conversation.turn',
    function_principal_id: 'principal.conversation.turn',
    caller_function_id: 'caller.conversation.turn',
    authority_reference: 'authority://conversation/turn',
    route: {
      contract_id: 'conversation.v1',
      operation_id: 'conversation.turn',
      function_id: 'conversation.turn',
      provider_pack_id: 'conversation-pack',
    },
    schema: {
      input_schema: {
        type: 'object',
        required: ['prompt'],
        properties: {
          prompt: {type: 'string', title: 'Prompt', default: 'hello'},
          temperature: {type: 'number', default: 0.2, title: 'Temperature'},
        },
      },
    },
    input_schema: {
      type: 'object',
      required: ['prompt'],
      properties: {
        prompt: {type: 'string', title: 'Prompt', default: 'hello'},
        temperature: {type: 'number', default: 0.2, title: 'Temperature'},
      },
    },
  };
}

function createDom(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {dom, container, root: createRoot(container)};
}

test('OperationInputForm renders the declared schema and invokes the exact payload', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  let received: Record<string, unknown> | null = null;
  try {
    await act(async () => {
      root.render(
        <OperationInputForm
          operation={operation()}
          descriptor={LAUNCHER_ADVANCED_VIEWS.aiInput}
          busy={false}
          canInvoke
          onInvoke={async (payload) => { received = payload; }}
        />,
      );
    });
    assert.match(container.textContent ?? '', /Schema-driven input/);
    const submit = [...container.querySelectorAll('button')].find((button) => button.type === 'submit');
    assert.ok(submit);
    const form = container.querySelector<HTMLFormElement>('form');
    assert.ok(form);

    await act(async () => {
      form.dispatchEvent(new dom.window.Event('submit', {bubbles: true, cancelable: true}));
    });
    assert.deepEqual(received, {prompt: 'hello', temperature: 0.2});
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('OperationInputForm disables invocation when Host readiness is not authoritative', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  try {
    await act(async () => {
      root.render(
        <OperationInputForm operation={operation(false)} descriptor={LAUNCHER_ADVANCED_VIEWS.aiInput} busy={false} canInvoke={false} onInvoke={async () => {}} />,
      );
    });
    const submit = [...container.querySelectorAll('button')].find((button) => button.type === 'submit');
    assert.ok(submit);
    assert.equal(submit.disabled, true);
    assert.match(container.textContent ?? '', /Invoke declared operation/);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('OperationInputForm obeys the parent descriptor action gate even for an invokable row', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  let invokes = 0;
  try {
    await act(async () => {
      root.render(
        <OperationInputForm
          operation={operation()}
          descriptor={LAUNCHER_ADVANCED_VIEWS.aiInput}
          busy={false}
          canInvoke={false}
          onInvoke={async () => { invokes += 1; }}
        />,
      );
    });
    const submit = [...container.querySelectorAll('button')].find((button) => button.type === 'submit');
    assert.ok(submit);
    assert.equal(submit.disabled, true);
    assert.match(submit.className, /min-h-11/);
    assert.equal(submit.getAttribute('aria-label'), 'Invoke declared contract operation');
    const form = container.querySelector<HTMLFormElement>('form');
    assert.ok(form);
    await act(async () => {
      form.dispatchEvent(new dom.window.Event('submit', {bubbles: true, cancelable: true}));
    });
    assert.equal(invokes, 0);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('OperationInputForm does not expose invoke UI for a read-only descriptor', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  let invokes = 0;
  try {
    await act(async () => {
      root.render(
        <OperationInputForm
          operation={operation()}
          descriptor={LAUNCHER_ADVANCED_VIEWS.graph}
          busy={false}
          canInvoke
          onInvoke={async () => { invokes += 1; }}
        />,
      );
    });
    const submit = [...container.querySelectorAll('button')].find((button) => button.type === 'submit');
    assert.equal(submit, undefined);
    assert.doesNotMatch(container.textContent ?? '', /Invoke declared operation/);
    assert.equal(invokes, 0);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('OperationInputForm keeps the invoke button focusable and rejects a double submit', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  let release: (() => void) | undefined;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  let invokes = 0;
  try {
    await act(async () => {
      root.render(
        <OperationInputForm
          operation={operation()}
          descriptor={LAUNCHER_ADVANCED_VIEWS.aiInput}
          busy={false}
          canInvoke
          onInvoke={async () => {
            invokes += 1;
            await pending;
          }}
        />,
      );
    });
    const submit = [...container.querySelectorAll('button')].find((button) => button.type === 'submit');
    assert.ok(submit);
    assert.match(submit.className, /focus-visible:ring/);
    const form = container.querySelector<HTMLFormElement>('form');
    assert.ok(form);
    await act(async () => {
      form.dispatchEvent(new dom.window.Event('submit', {bubbles: true, cancelable: true}));
      form.dispatchEvent(new dom.window.Event('submit', {bubbles: true, cancelable: true}));
    });
    assert.equal(invokes, 1);
    assert.equal(submit.disabled, true);
    release?.();
    await act(async () => { await pending; });
    assert.equal(submit.disabled, false);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('OperationInputForm omits blank optional values and preserves JSON enum types', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  let received: Record<string, unknown> | null = null;
  const typedOperation: RuntimeOperationDescriptor = {
    ...operation(),
    operation_id: 'typed.operation',
    input_schema: {
      type: 'object',
      required: ['required_text'],
      properties: {
        required_text: {type: 'string', default: 'ready'},
        optional_number: {type: 'number'},
        optional_object: {type: 'object'},
        optional_array: {type: 'array'},
        numeric_mode: {enum: [7, 8]},
        boolean_mode: {enum: [true, false]},
        null_mode: {enum: [null, 'fallback']},
      },
    },
  };
  try {
    await act(async () => {
      root.render(
        <OperationInputForm
          operation={typedOperation}
          descriptor={LAUNCHER_ADVANCED_VIEWS.aiInput}
          busy={false}
          canInvoke
          onInvoke={async (payload) => { received = payload; }}
        />,
      );
    });
    const setSelect = (label: string, value: string) => {
      const select = container.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`);
      assert.ok(select);
      select.value = value;
      select.dispatchEvent(new dom.window.Event('change', {bubbles: true}));
    };
    await act(async () => {
      setSelect('numeric_mode', '0');
      setSelect('boolean_mode', '0');
      setSelect('null_mode', '0');
    });
    const form = container.querySelector<HTMLFormElement>('form');
    assert.ok(form);
    await act(async () => {
      form.dispatchEvent(new dom.window.Event('submit', {bubbles: true, cancelable: true}));
    });
    assert.deepEqual(received, {
      required_text: 'ready',
      numeric_mode: 7,
      boolean_mode: true,
      null_mode: null,
    });
    assert.equal(Object.prototype.hasOwnProperty.call(received ?? {}, 'optional_number'), false);
    assert.equal(Object.prototype.hasOwnProperty.call(received ?? {}, 'optional_object'), false);
    assert.equal(Object.prototype.hasOwnProperty.call(received ?? {}, 'optional_array'), false);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('OperationInputForm omits an untouched optional boolean while keeping it visually unchecked', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  let received: Record<string, unknown> | null = null;
  const booleanOperation: RuntimeOperationDescriptor = {
    ...operation(),
    operation_id: 'optional.boolean',
    input_schema: {
      type: 'object',
      properties: {
        enabled: {type: 'boolean', title: 'Enabled'},
      },
    },
  };
  try {
    await act(async () => {
      root.render(
        <OperationInputForm
          operation={booleanOperation}
          descriptor={LAUNCHER_ADVANCED_VIEWS.aiInput}
          busy={false}
          canInvoke
          onInvoke={async (payload) => { received = payload; }}
        />,
      );
    });
    const checkbox = container.querySelector<HTMLInputElement>('input[aria-label="Enabled"]');
    assert.ok(checkbox);
    assert.equal(checkbox.checked, false);
    const form = container.querySelector<HTMLFormElement>('form');
    assert.ok(form);
    await act(async () => {
      form.dispatchEvent(new dom.window.Event('submit', {bubbles: true, cancelable: true}));
    });
    assert.deepEqual(received, {});
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('OperationInputForm submits explicit false and required boolean values', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  let received: Record<string, unknown> | null = null;
  const booleanOperation: RuntimeOperationDescriptor = {
    ...operation(),
    operation_id: 'explicit.boolean',
    input_schema: {
      type: 'object',
      required: ['required_enabled'],
      properties: {
        optional_enabled: {type: 'boolean', title: 'Optional enabled'},
        required_enabled: {type: 'boolean', title: 'Required enabled'},
      },
    },
  };
  try {
    await act(async () => {
      root.render(
        <OperationInputForm
          operation={booleanOperation}
          descriptor={LAUNCHER_ADVANCED_VIEWS.aiInput}
          busy={false}
          canInvoke
          onInvoke={async (payload) => { received = payload; }}
        />,
      );
    });
    const optionalCheckbox = container.querySelector<HTMLInputElement>('input[aria-label="Optional enabled"]');
    const requiredCheckbox = container.querySelector<HTMLInputElement>('input[aria-label="Required enabled"]');
    assert.ok(optionalCheckbox);
    assert.ok(requiredCheckbox);
    assert.equal(optionalCheckbox.checked, false);
    assert.equal(requiredCheckbox.checked, false);
    await act(async () => {
      optionalCheckbox.click();
      optionalCheckbox.click();
    });
    assert.equal(optionalCheckbox.checked, false);
    const form = container.querySelector<HTMLFormElement>('form');
    assert.ok(form);
    await act(async () => {
      form.dispatchEvent(new dom.window.Event('submit', {bubbles: true, cancelable: true}));
    });
    assert.deepEqual(received, {optional_enabled: false, required_enabled: false});
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('OperationInputForm visibly rejects missing required values and malformed JSON', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  let invokes = 0;
  const invalidOperation: RuntimeOperationDescriptor = {
    ...operation(),
    operation_id: 'invalid.operation',
    input_schema: {
      type: 'object',
      required: ['required_text', 'settings'],
      properties: {
        required_text: {type: 'string', title: 'Required text'},
        settings: {type: 'object'},
      },
    },
  };
  try {
    await act(async () => {
      root.render(
        <OperationInputForm
          operation={invalidOperation}
          descriptor={LAUNCHER_ADVANCED_VIEWS.aiInput}
          busy={false}
          canInvoke
          onInvoke={async () => { invokes += 1; }}
        />,
      );
    });
    const form = container.querySelector<HTMLFormElement>('form');
    assert.ok(form);
    await act(async () => {
      form.dispatchEvent(new dom.window.Event('submit', {bubbles: true, cancelable: true}));
    });
    assert.match(container.querySelector('[role="alert"]')?.textContent ?? '', /required_text/);

    const jsonOperation: RuntimeOperationDescriptor = {
      ...invalidOperation,
      input_schema: {
        type: 'object',
        required: ['required_text'],
        properties: {
          required_text: {type: 'string', default: 'ok', title: 'Required text'},
          settings: {type: 'object', default: '{not-json'},
        },
      },
    };
    await act(async () => {
      root.render(
        <OperationInputForm
          operation={jsonOperation}
          descriptor={LAUNCHER_ADVANCED_VIEWS.aiInput}
          busy={false}
          canInvoke
          onInvoke={async () => { invokes += 1; }}
        />,
      );
    });
    assert.ok(container.querySelector<HTMLTextAreaElement>('[aria-label="settings"]'));
    const jsonForm = container.querySelector<HTMLFormElement>('form');
    assert.ok(jsonForm);
    await act(async () => {
      jsonForm.dispatchEvent(new dom.window.Event('submit', {bubbles: true, cancelable: true}));
    });
    assert.match(container.querySelector('[role="alert"]')?.textContent ?? '', /valid JSON/);
    assert.equal(invokes, 0);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});
