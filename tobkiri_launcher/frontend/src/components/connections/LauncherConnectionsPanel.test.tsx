import assert from 'node:assert/strict';
import {act} from 'react';
import type {Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import test from 'node:test';

import {LauncherConnectionsPanel} from './LauncherConnectionsPanel';
import {ApiContractError} from '@/src/lib/apiTransport';
import type {
  LauncherActiveProfileContext,
  LauncherConnectionsSnapshot,
  LauncherModelProfileList,
  LauncherProviderConfigureRequest,
} from '@/src/lib/launcherConnections';
import type {LauncherConnectionsClient} from '@/src/lib/launcherConnectionsConfigure';

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

const scope: LauncherActiveProfileContext = {
  profile_id: 'defaults',
  profile_revision: digest('a'),
  catalog_revision: digest('b'),
  plan_digest: digest('c'),
  activation_id: 'activation:testactivation1',
};

const snapshotFixture: LauncherConnectionsSnapshot = {
  revision: 3,
  providers: [
    {
      provider_instance_id: 'provider.openai.default',
      display_name: 'openai.default',
      enabled: true,
      credential_status: 'configured',
      health_status: 'verified',
      reachability: 'available',
      observed_at: 1700000000,
    },
  ],
};

const modelListFixture: LauncherModelProfileList = {
  profiles: [
    {
      profile_id: 'main-chat',
      display_name: 'Main chat',
      model_id: 'gpt-4o-mini',
      provider_id: 'provider.openai.default',
      route_configured: true,
    },
  ],
  count: 1,
  registry_revision: 7,
};

async function createDom(): Promise<{dom: JSDOM; container: HTMLElement; root: Root}> {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/',
  });
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    sessionStorage: {value: dom.window.sessionStorage, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  // react-dom evaluates its DOM capability checks at module load, so it must
  // be imported only after the jsdom globals exist.
  const {createRoot} = await import('react-dom/client');
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {dom, container, root: createRoot(container)};
}

function fakeClient(overrides: Partial<LauncherConnectionsClient> = {}) {
  const calls: Array<{method: string; detail?: unknown}> = [];
  const client: LauncherConnectionsClient = {
    fetchConnections: async () => {
      calls.push({method: 'fetchConnections'});
      return snapshotFixture;
    },
    fetchModelProfiles: async () => {
      calls.push({method: 'fetchModelProfiles'});
      return modelListFixture;
    },
    saveModelProfile: async (input) => {
      calls.push({method: 'saveModelProfile', detail: input});
      return modelListFixture.profiles[0];
    },
    configureProviderConnection: async (request, _scope, _options) => {
      calls.push({method: 'configureProviderConnection', detail: request});
    },
    ...overrides,
  };
  return {client, calls};
}

function setInput(dom: JSDOM, input: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(
    dom.window.HTMLInputElement.prototype,
    'value',
  )!.set!;
  setter.call(input, value);
  input.dispatchEvent(new dom.window.Event('input', {bubbles: true}));
}

function setSelect(dom: JSDOM, select: HTMLSelectElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(
    dom.window.HTMLSelectElement.prototype,
    'value',
  )!.set!;
  setter.call(select, value);
  select.dispatchEvent(new dom.window.Event('change', {bubbles: true}));
}

function inputById(container: HTMLElement, id: string): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>(`#${id}`);
  assert.ok(input, `missing input ${id}`);
  return input;
}

function buttonContaining(container: HTMLElement, text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll('button')].find(
    (candidate) => candidate.textContent?.includes(text),
  );
  assert.ok(button, `missing button ${text}`);
  return button as HTMLButtonElement;
}

async function flush(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

test('the panel explains that a verified active Profile is required', async () => {
  const {container, root} = await createDom();
  const {client, calls} = fakeClient();
  await act(async () => {
    root.render(<LauncherConnectionsPanel profile={null} client={client} />);
  });
  await flush();
  assert.ok(container.textContent?.includes('verified active Profile'));
  assert.equal(calls.length, 0, 'no registry reads happen without a scope');
  await act(async () => root.unmount());
});

test('the panel lists shared connections and models for the active Profile', async () => {
  const {container, root} = await createDom();
  const {client, calls} = fakeClient();
  await act(async () => {
    root.render(<LauncherConnectionsPanel profile={scope} client={client} />);
  });
  await flush();
  assert.ok(container.textContent?.includes('openai.default'));
  assert.ok(container.textContent?.includes('Main chat'));
  assert.ok(
    calls.some((call) => call.method === 'fetchConnections')
      && calls.some((call) => call.method === 'fetchModelProfiles'),
  );
  const submit = buttonContaining(container, 'Register connection');
  assert.equal(submit.disabled, false);
  await act(async () => root.unmount());
});

test('the disabled flag renders the registry read-only', async () => {
  const {container, root} = await createDom();
  const {client} = fakeClient();
  await act(async () => {
    root.render(
      <LauncherConnectionsPanel profile={scope} client={client} disabled />,
    );
  });
  await flush();
  assert.ok(container.textContent?.includes('read-only'));
  assert.equal(buttonContaining(container, 'Register connection').disabled, true);
  assert.equal(buttonContaining(container, 'Register model').disabled, true);
  await act(async () => root.unmount());
});

test('a recorded pending attempt renders a resume hint without any call', async () => {
  const {dom, container, root} = await createDom();
  dom.window.sessionStorage.setItem(
    `tobkiri-launcher-provider-configuration-pending-v1:${scope.profile_id}:${scope.activation_id}`,
    JSON.stringify({
      v: 1,
      connection: 'openai.default',
      effect: 'effect-1',
      digest: 'a'.repeat(64),
      correlation: '0f8fad5b-d9cb-469f-a165-70867728950e',
    }),
  );
  const {client, calls} = fakeClient();
  await act(async () => {
    root.render(<LauncherConnectionsPanel profile={scope} client={client} />);
  });
  await flush();
  assert.ok(container.textContent?.includes('unfinished registration'));
  assert.equal(
    calls.filter((call) => call.method === 'configureProviderConnection').length,
    0,
    'the pending hint never auto-resumes',
  );
  await act(async () => root.unmount());
});

test('submitting a hosted connection hands the exact request to the client and clears the key', async () => {
  const {dom, container, root} = await createDom();
  const {client, calls} = fakeClient();
  await act(async () => {
    root.render(<LauncherConnectionsPanel profile={scope} client={client} />);
  });
  await flush();

  const providerSelect = container.querySelector<HTMLSelectElement>(
    '#launcher-connection-provider',
  );
  assert.ok(providerSelect);
  await act(async () => {
    setSelect(dom, providerSelect, 'openai');
  });
  const keyInput = inputById(container, 'launcher-connection-key');
  await act(async () => {
    setInput(dom, keyInput, 'sk-synthetic-test-key');
  });
  const audioBox = [...container.querySelectorAll('input[type="checkbox"]')];
  assert.equal(audioBox.length, 2, 'audio grants are offered for openai-compatible');
  await act(async () => {
    (audioBox[0] as HTMLInputElement).click();
  });
  await act(async () => {
    buttonContaining(container, 'Register connection').click();
  });
  await flush();

  const configureCalls = calls.filter(
    (call) => call.method === 'configureProviderConnection',
  );
  assert.equal(configureCalls.length, 1);
  assert.deepEqual(configureCalls[0].detail as LauncherProviderConfigureRequest, {
    connection_name: 'openai.default',
    protocol: 'openai-compatible',
    endpoint: 'https://api.openai.com/v1',
    key_value: 'sk-synthetic-test-key',
    capabilities: ['ai.generate', 'ai.stream', 'ai.audio.transcribe'],
  });
  assert.equal(keyInput.value, '', 'the key field is cleared after handoff');
  await act(async () => root.unmount());
});

test('without audio grants the request carries no capabilities key', async () => {
  const {dom, container, root} = await createDom();
  const {client, calls} = fakeClient();
  await act(async () => {
    root.render(<LauncherConnectionsPanel profile={scope} client={client} />);
  });
  await flush();
  const providerSelect = container.querySelector<HTMLSelectElement>(
    '#launcher-connection-provider',
  );
  assert.ok(providerSelect);
  await act(async () => {
    setSelect(dom, providerSelect, 'openai');
  });
  await act(async () => {
    setInput(dom, inputById(container, 'launcher-connection-key'), 'sk-synthetic');
  });
  await act(async () => {
    buttonContaining(container, 'Register connection').click();
  });
  await flush();
  const configureCalls = calls.filter(
    (call) => call.method === 'configureProviderConnection',
  );
  assert.equal(configureCalls.length, 1);
  const request = configureCalls[0].detail as Record<string, unknown>;
  assert.equal('capabilities' in request, false);
  await act(async () => root.unmount());
});

test('audio grants stay hidden for anthropic and local keyless modes', async () => {
  const {dom, container, root} = await createDom();
  const {client} = fakeClient();
  await act(async () => {
    root.render(<LauncherConnectionsPanel profile={scope} client={client} />);
  });
  await flush();
  const providerSelect = container.querySelector<HTMLSelectElement>(
    '#launcher-connection-provider',
  );
  assert.ok(providerSelect);
  await act(async () => {
    setSelect(dom, providerSelect, 'anthropic');
  });
  assert.equal(
    container.querySelectorAll('input[type="checkbox"]').length,
    0,
    'no audio grants for anthropic',
  );
  await act(async () => {
    setSelect(dom, providerSelect, 'local');
  });
  assert.equal(
    container.querySelectorAll('input[type="checkbox"]').length,
    0,
    'no audio grants for the keyless local mode',
  );
  assert.equal(
    container.querySelector('#launcher-connection-key'),
    null,
    'the key field is absent for local keyless mode',
  );
  await act(async () => root.unmount());
});

test('model registration submits the exact narrow input and refreshes the list', async () => {
  const {dom, container, root} = await createDom();
  const {client, calls} = fakeClient();
  await act(async () => {
    root.render(<LauncherConnectionsPanel profile={scope} client={client} />);
  });
  await flush();
  const providerSelect = container.querySelector<HTMLSelectElement>(
    '#launcher-model-provider',
  );
  assert.ok(providerSelect);
  await act(async () => {
    setSelect(dom, providerSelect, 'provider.openai.default');
    setInput(dom, inputById(container, 'launcher-model-profile-id'), 'alt-chat');
    setInput(dom, inputById(container, 'launcher-model-model-id'), 'gpt-4o');
    setInput(dom, inputById(container, 'launcher-model-display-name'), 'Alt chat');
  });
  await act(async () => {
    buttonContaining(container, 'Register model').click();
  });
  await flush();
  const saves = calls.filter((call) => call.method === 'saveModelProfile');
  assert.equal(saves.length, 1);
  assert.deepEqual(saves[0].detail, {
    model_profile_id: 'alt-chat',
    model_id: 'gpt-4o',
    provider_instance_id: 'provider.openai.default',
    display_name: 'Alt chat',
  });
  assert.ok(
    calls.filter((call) => call.method === 'fetchModelProfiles').length >= 2,
    'the model list is re-read after a save',
  );
  await act(async () => root.unmount());
});

test('a stale Host capture invalidates the rendered view and blocks writes', async () => {
  const {container, root} = await createDom();
  const {client} = fakeClient({
    fetchConnections: async () => {
      throw new ApiContractError('Contract activation identity is stale', {
        state: 'contract_dispatch_denied',
        code: 'CONTRACT_MAP_STALE',
      });
    },
  });
  await act(async () => {
    root.render(<LauncherConnectionsPanel profile={scope} client={client} />);
  });
  await flush();
  assert.ok(container.textContent?.includes('context changed'));
  assert.equal(
    buttonContaining(container, 'Register connection').disabled,
    true,
    'writes are blocked after a stale-context rejection',
  );
  await act(async () => root.unmount());
});

test('inspecting an inactive Profile aborts an in-flight confirmation', async () => {
  const {dom, container, root} = await createDom();
  let capturedSignal: AbortSignal | undefined;
  const {client} = fakeClient({
    configureProviderConnection: async (_request, _scope, options) => {
      capturedSignal = options?.signal;
      await new Promise(() => {});
    },
  });
  await act(async () => {
    root.render(<LauncherConnectionsPanel profile={scope} client={client} />);
  });
  await flush();
  const providerSelect = container.querySelector<HTMLSelectElement>(
    '#launcher-connection-provider',
  );
  assert.ok(providerSelect);
  await act(async () => {
    setSelect(dom, providerSelect, 'openai');
    setInput(dom, inputById(container, 'launcher-connection-key'), 'sk-x');
  });
  await act(async () => {
    buttonContaining(container, 'Register connection').click();
  });
  assert.ok(capturedSignal, 'the registration handoff began');
  await act(async () => {
    root.render(
      <LauncherConnectionsPanel profile={scope} client={client} disabled />,
    );
  });
  assert.equal(
    capturedSignal!.aborted,
    true,
    'the in-flight confirmation is aborted when inspection disables the panel',
  );
  await act(async () => root.unmount());
});

test('a synchronous double click dispatches only one provider handoff', async () => {
  const {dom, container, root} = await createDom();
  const {client, calls} = fakeClient({
    configureProviderConnection: async () => {
      calls.push({method: 'configureProviderConnection'});
      await new Promise(() => {});
    },
  });
  await act(async () => {
    root.render(<LauncherConnectionsPanel profile={scope} client={client} />);
  });
  await flush();
  const providerSelect = container.querySelector<HTMLSelectElement>(
    '#launcher-connection-provider',
  );
  assert.ok(providerSelect);
  await act(async () => {
    setSelect(dom, providerSelect, 'openai');
    setInput(dom, inputById(container, 'launcher-connection-key'), 'sk-x');
  });
  const button = buttonContaining(container, 'Register connection');
  await act(async () => {
    button.click();
    button.click();
  });
  assert.equal(
    calls.filter((call) => call.method === 'configureProviderConnection').length,
    1,
  );
  await act(async () => root.unmount());
});

test('a synchronous double click dispatches only one model save', async () => {
  const {dom, container, root} = await createDom();
  const {client, calls} = fakeClient({
    saveModelProfile: async () => {
      calls.push({method: 'saveModelProfile'});
      await new Promise(() => {});
      return modelListFixture.profiles[0];
    },
  });
  await act(async () => {
    root.render(<LauncherConnectionsPanel profile={scope} client={client} />);
  });
  await flush();
  const providerSelect = container.querySelector<HTMLSelectElement>(
    '#launcher-model-provider',
  );
  assert.ok(providerSelect);
  await act(async () => {
    setSelect(dom, providerSelect, 'provider.openai.default');
    setInput(dom, inputById(container, 'launcher-model-profile-id'), 'alt-chat');
    setInput(dom, inputById(container, 'launcher-model-model-id'), 'gpt-4o');
    setInput(dom, inputById(container, 'launcher-model-display-name'), 'Alt chat');
  });
  const button = buttonContaining(container, 'Register model');
  await act(async () => {
    button.click();
    button.click();
  });
  assert.equal(
    calls.filter((call) => call.method === 'saveModelProfile').length,
    1,
  );
  await act(async () => root.unmount());
});

test('a removed provider requires an explicit choice instead of retargeting', async () => {
  const {dom, container, root} = await createDom();
  const providerB = {
    provider_instance_id: 'provider.deepseek.default',
    display_name: 'deepseek.default',
    enabled: true,
    credential_status: 'configured' as const,
    health_status: 'verified' as const,
    reachability: 'available' as const,
    observed_at: 1700000001,
  };
  const {client, calls} = fakeClient();
  await act(async () => {
    root.render(<LauncherConnectionsPanel profile={scope} client={client} />);
  });
  await flush();
  const providerSelect = container.querySelector<HTMLSelectElement>(
    '#launcher-model-provider',
  );
  assert.ok(providerSelect);
  await act(async () => {
    setSelect(dom, providerSelect, 'provider.openai.default');
  });
  // The chosen connection disappears from the registry on the next read.
  const after = fakeClient({
    fetchConnections: async () => ({
      revision: 4,
      providers: [providerB],
    }),
  });
  await act(async () => {
    root.render(
      <LauncherConnectionsPanel profile={scope} client={after.client} />,
    );
  });
  await flush();
  assert.equal(
    providerSelect.value,
    '',
    'the stale selection is cleared to the explicit placeholder',
  );
  await act(async () => {
    setInput(dom, inputById(container, 'launcher-model-profile-id'), 'alt-chat');
    setInput(dom, inputById(container, 'launcher-model-model-id'), 'gpt-4o');
    setInput(dom, inputById(container, 'launcher-model-display-name'), 'Alt chat');
  });
  await act(async () => {
    buttonContaining(container, 'Register model').click();
  });
  await flush();
  assert.ok(container.textContent?.includes('Choose a registered connection'));
  const saves =
    calls.filter((call) => call.method === 'saveModelProfile').length
    + after.calls.filter((call) => call.method === 'saveModelProfile').length;
  assert.equal(saves, 0, 'no model save is attempted without an explicit connection choice');
  await act(async () => root.unmount());
});

test('a rejected registration surfaces the panel-safe error message', async () => {
  const {dom, container, root} = await createDom();
  const {client} = fakeClient({
    configureProviderConnection: async () => {
      throw new Error('The connection registration was not approved.');
    },
  });
  await act(async () => {
    root.render(<LauncherConnectionsPanel profile={scope} client={client} />);
  });
  await flush();
  const providerSelect = container.querySelector<HTMLSelectElement>(
    '#launcher-connection-provider',
  );
  assert.ok(providerSelect);
  await act(async () => {
    setSelect(dom, providerSelect, 'openai');
    setInput(dom, inputById(container, 'launcher-connection-key'), 'sk-x');
  });
  await act(async () => {
    buttonContaining(container, 'Register connection').click();
  });
  await flush();
  assert.ok(container.textContent?.includes('not approved'));
  await act(async () => root.unmount());
});
