import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';

import type {ApiDynamicFrontendCatalog, ApiPresentationState} from '@/src/lib/apiTypes';
import {clearApiPrefetchCache} from '@/src/lib/api';
import {setRuntimeDispatchStatus} from '@/src/lib/runtimeDispatchGate';
import {useAppStore} from '@/src/store';
import {ShellLaunchCard} from './ShellLaunchCard';

const state = (status: 'materialized' | 'blocked'): ApiPresentationState => ({
  catalog: {
    schema: 'io.tobkiri.launcher.presentation-catalog.v1',
    generator: 'test',
    generator_version: '1.0.0',
    default_profile_id: 'defaults-modern',
    default_profile_source: 'profiles/defaults-modern.profile.yaml',
    default_profile_digest: 'sha256:profile',
    default_selection: {
      base_pack_id: 'defaults-basepack',
      shell_provider_id: 'shell.tauri.default',
    },
    contract_revisions: [],
    source_manifest_digests: {},
    base_packs: [],
    shell_providers: [{
      provider_id: 'shell.tauri.default',
      display_name: 'Tobkiri Desktop Shell',
      contract_id: 'app.shell.v1',
      contract_revision_digest: 'sha256:shell',
      experience_role: 'shell',
      presentation_kind: 'packaged_process',
      presentation_family: 'graphical',
      technology: 'tauri',
      capabilities: ['navigation', 'commands'],
      consumes_contracts: ['conversation.turn.v1'],
      contributions: [],
      artifact_variants: [],
      artifact: null,
      approval: {
        state: 'verified',
        provider_trust: 'verified',
        grant_state: 'available',
        authority_mode: 'lease_only',
        execution_domain: 'shell',
        effect_scope: ['conversation.turn.v1'],
        blast_radius: 'selected shell',
      },
    }],
    generated_at: 1,
  },
  selection: {
    base_pack_id: 'defaults-basepack',
    shell_provider_id: 'shell.tauri.default',
  },
  materialization: {
    status,
    base_pack_id: 'defaults-basepack',
    shell_provider_id: 'shell.tauri.default',
    selected_contributions: [],
    artifact: null,
    reason: status === 'blocked' ? 'surface disabled by test state' : null,
  },
});

const frontendCatalog = (conversationReady: boolean): ApiDynamicFrontendCatalog => ({
  version: 'rumi.ui.contribution.v1',
  profile_id: 'defaults-modern',
  profile_revision: `sha256:${'a'.repeat(64)}`,
  plan_hash: `sha256:${'a'.repeat(64)}`,
  contributions: conversationReady ? [{
    contribution_id: 'defaults.conversation.complete',
    owner_pack_id: 'defaultspack',
    label: 'Tobkiri Conversation',
    action_contract: 'conversation.turn.v1',
    operation_id: 'complete',
    provider_id: 'defaultspack.conversation',
    function_id: 'defaultspack.conversation',
    kind: 'route',
    mode: 'declarative',
    route: '/chat',
    owner_pack_hash: `sha256:${'b'.repeat(64)}`,
    build_identity: 'defaultspack.conversation',
    resolved_profile_revision: `sha256:${'a'.repeat(64)}`,
    resolved_plan_hash: `sha256:${'a'.repeat(64)}`,
    descriptor_hash: `sha256:${'c'.repeat(64)}`,
    view: {type: 'conversation_v4'},
  }] : [],
  diagnostics: [],
  quarantined_pack_ids: [],
  catalog_hash: `sha256:${'d'.repeat(64)}`,
});

function installCatalogFetch(
  catalogs: ApiDynamicFrontendCatalog[],
): {routes: string[]; restore: () => void} {
  clearApiPrefetchCache();
  setRuntimeDispatchStatus('runtime_ready');
  const originalFetch = globalThis.fetch;
  const routes: string[] = [];
  let index = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    routes.push(decodeURIComponent(url.replace('/api/contracts/defaultspack/', '')));
    const catalog = catalogs[Math.min(index, catalogs.length - 1)];
    index += 1;
    return new Response(JSON.stringify({
      success: true,
      data: {dynamic_host: catalog},
      error: null,
    }), {headers: {'Content-Type': 'application/json'}});
  }) as typeof fetch;
  return {
    routes,
    restore: () => {
      clearApiPrefetchCache();
      setRuntimeDispatchStatus('unknown');
      globalThis.fetch = originalFetch;
    },
  };
}

function createSurface(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/panel/',
  });
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
    sessionStorage: {value: dom.window.sessionStorage, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const invokeCalls: string[] = [];
  Object.defineProperty(dom.window, '__TAURI__', {
    configurable: true,
    value: {
      core: {
        invoke: async (command: string) => {
          invokeCalls.push(command);
          if (command === 'get_presentation_catalog') return state('materialized');
          return {
            status: 'launched',
            provider_id: 'shell.tauri.default',
            artifact_id: 'shell-arm64',
            message: 'Tobkiri Conversation launched',
          };
        },
      },
    },
  });
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  const root = createRoot(container);
  (dom.window as unknown as {__invokeCalls?: string[]}).__invokeCalls = invokeCalls;
  return {dom, container, root};
}

async function renderCard(root: Root): Promise<void> {
  await act(async () => {
    root.render(<ShellLaunchCard runtimeReady />);
    await Promise.resolve();
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

test('selected Shell exposes the typed Conversation launch action', async () => {
  const previousState = useAppStore.getState();
  const {dom, container, root} = createSurface();
  const catalogFetch = installCatalogFetch([frontendCatalog(true), frontendCatalog(true)]);
  const toasts: string[] = [];
  useAppStore.setState({
    addToast: (message, type) => {
      if (type === 'success') toasts.push(message);
    },
  });

  try {
    await renderCard(root);
    const button = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
      (candidate) => candidate.textContent?.includes('Open Tobkiri Conversation'),
    );
    assert.ok(button);
    assert.equal(button.disabled, false);
    await act(async () => button.click());
    assert.match(container.textContent ?? '', /Tobkiri Desktop Shell/);
    assert.deepEqual(
      (dom.window as unknown as {__invokeCalls?: string[]}).__invokeCalls,
      ['get_presentation_catalog', 'launch_selected_presentation'],
    );
    assert.deepEqual(toasts, ['Tobkiri Conversation launched']);
    assert.deepEqual(catalogFetch.routes, [
      'GET /api/ui/catalog',
      'GET /api/ui/catalog',
    ]);
  } finally {
    act(() => root.unmount());
    useAppStore.setState(previousState, true);
    catalogFetch.restore();
    dom.window.close();
  }
});

test('disabled selected Shell keeps Conversation unavailable and does not launch', async () => {
  const previousState = useAppStore.getState();
  const {dom, container, root} = createSurface();
  const catalogFetch = installCatalogFetch([frontendCatalog(true)]);
  Object.defineProperty(dom.window, '__TAURI__', {
    configurable: true,
    value: {
      core: {
        invoke: async (command: string) => {
          if (command === 'get_presentation_catalog') return state('blocked');
          throw new Error('launch must not be called');
        },
      },
    },
  });

  try {
    await renderCard(root);
    const button = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
      (candidate) => candidate.textContent?.includes('Open Tobkiri Conversation'),
    );
    assert.ok(button);
    assert.equal(button.disabled, true);
    assert.match(container.textContent ?? '', /surface disabled by test state/);
    await act(async () => button.click());
  } finally {
    act(() => root.unmount());
    useAppStore.setState(previousState, true);
    catalogFetch.restore();
    dom.window.close();
  }
});

test('unready Conversation capability keeps the selected Shell fail-closed', async () => {
  const previousState = useAppStore.getState();
  const {dom, container, root} = createSurface();
  const catalogFetch = installCatalogFetch([frontendCatalog(false)]);

  try {
    await renderCard(root);
    const button = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
      (candidate) => candidate.textContent?.includes('Open Tobkiri Conversation'),
    );
    assert.ok(button);
    assert.equal(button.disabled, true);
    assert.match(container.textContent ?? '', /verified Conversation capability is not ready/i);
    assert.deepEqual(
      (dom.window as unknown as {__invokeCalls?: string[]}).__invokeCalls,
      ['get_presentation_catalog'],
    );
  } finally {
    act(() => root.unmount());
    useAppStore.setState(previousState, true);
    catalogFetch.restore();
    dom.window.close();
  }
});

test('fresh capability loss prevents the typed Shell launch command', async () => {
  const previousState = useAppStore.getState();
  const {dom, container, root} = createSurface();
  const catalogFetch = installCatalogFetch([
    frontendCatalog(true),
    frontendCatalog(false),
  ]);

  try {
    await renderCard(root);
    const button = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
      (candidate) => candidate.textContent?.includes('Open Tobkiri Conversation'),
    );
    assert.ok(button);
    assert.equal(button.disabled, false);
    await act(async () => button.click());
    assert.match(container.textContent ?? '', /changed and is no longer ready/i);
    assert.deepEqual(
      (dom.window as unknown as {__invokeCalls?: string[]}).__invokeCalls,
      ['get_presentation_catalog'],
    );
    assert.deepEqual(catalogFetch.routes, [
      'GET /api/ui/catalog',
      'GET /api/ui/catalog',
    ]);
  } finally {
    act(() => root.unmount());
    useAppStore.setState(previousState, true);
    catalogFetch.restore();
    dom.window.close();
  }
});
