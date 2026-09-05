import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {MemoryRouter, Route, Routes} from 'react-router';

import {type Pack, useAppStore} from '@/src/store';
import type {ApiDynamicFrontendCatalog, ApiPackVMDoctor, PackControlBinding} from '@/src/lib/apiTypes';
import {PackDetail} from './PackDetail';

const operation = {
  operationId: 'rumi_file_inspect_pack.file-inspect',
  contractId: 'tobkiri.service.file.inspect.v1',
  providerId: 'rumi_file_inspect_pack.file-inspect.service',
  capabilities: ['file.inspect'],
  inputSchema: {},
  invokable: true,
};

const pack: Pack = {
  id: 'rumi_file_inspect_pack',
  name: 'Tobkiri File Inspect',
  version: '1.0.0',
  type: 'community',
  installed: true,
  enabled: true,
  description: 'Inspect workspace files.',
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

const catalog: ApiDynamicFrontendCatalog = {
  version: 'rumi.ui.contribution.v1',
  profile_id: 'profile-a',
  profile_revision: 'sha256:profile',
  activation_id: 'activation:profile-a',
  plan_hash: 'sha256:plan',
  contributions: [{
    contribution_id: 'file-inspect',
    owner_pack_id: pack.id,
    label: operation.operationId,
    operation_id: operation.operationId,
    action_contract: operation.contractId,
  }],
  diagnostics: [],
  quarantined_pack_ids: [],
  catalog_hash: 'sha256:catalog',
};

const activePackBinding: PackControlBinding = {
  profile_id: pack.profileId,
  workspace_id: pack.workspaceId,
  profile_revision: pack.profileRevision,
  plan_digest: pack.planDigest,
  catalog_revision: pack.catalogRevision,
};

const healthyDoctor: ApiPackVMDoctor = {
  ready: true,
  backend_id: 'tobkiri.python-pack-v4',
  platform: 'macos',
  instance: 'tobkiri-packvm-v4',
  reason: null,
  attestation_digest: `sha256:${'a'.repeat(64)}`,
};

function createSurface(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: `http://localhost/packs/${pack.id}`,
  });
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {dom, container, root: createRoot(container)};
}

async function renderDetail(root: Root): Promise<void> {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[`/packs/${pack.id}`]}>
        <Routes>
          <Route path="/packs/:id" element={<PackDetail />} />
        </Routes>
      </MemoryRouter>,
    );
  });
}

function configureStore(currentPack: Pack, currentCatalog = catalog): void {
  useAppStore.setState({
    packs: [currentPack],
    packCatalogBinding: activePackBinding,
    packsLoading: false,
    packsError: null,
    frontendCatalog: currentCatalog,
    frontendCatalogLoading: false,
    frontendCatalogError: null,
    packVmDoctor: healthyDoctor,
    packVmDoctorLoading: false,
    refreshPackVMDoctor: async () => healthyDoctor,
    packOperationPending: {},
    loadPacks: async () => {},
    loadFrontendCatalog: async () => {},
    invokePackOperation: async () => ({ok: true}),
    installPack: async () => {},
    approvePack: async () => {},
    revokePackApproval: async () => {},
    togglePack: async () => false,
  });
}

test('PackDetail displays declared capabilities and the verified file operation surface', async () => {
  const previousState = useAppStore.getState();
  const {dom, container, root} = createSurface();
  configureStore(pack);

  try {
    await renderDetail(root);
    assert.match(container.textContent ?? '', /file\.inspect/);
    assert.match(container.textContent ?? '', /rumi_file_inspect_pack\.file-inspect/);
    assert.match(container.textContent ?? '', /tobkiri\.service\.file\.inspect\.v1/);
    assert.ok(container.querySelector('#file-inspect-path'));
    assert.equal(container.querySelector<HTMLButtonElement>('button[type="submit"]')?.disabled, true);
  } finally {
    act(() => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
  }
});

test('PackDetail keeps the file operation unavailable after approval revocation', async () => {
  const previousState = useAppStore.getState();
  const {dom, container, root} = createSurface();
  configureStore({
    ...pack,
    enabled: false,
    approved: false,
    approvalStatus: 'revoked',
    approvalReason: 'approval_revoked',
    approvalIssues: ['approval_revoked'],
  });

  try {
    await renderDetail(root);
    assert.match(container.textContent ?? '', /Approval revoked/);
    assert.match(container.textContent ?? '', /approval is revoked/i);
    assert.equal(container.querySelector<HTMLButtonElement>('button[type="submit"]')?.disabled, true);
  } finally {
    act(() => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
  }
});

test('PackDetail renders typed backend-unavailable diagnostics without exposing invocation', async () => {
  const previousState = useAppStore.getState();
  const {dom, container, root} = createSurface();
  configureStore(pack, {
    ...catalog,
    diagnostics: [{
      code: 'production_backend_unavailable',
      pack_id: pack.id,
      operation_id: operation.operationId,
      message: 'Authenticated production backend is unavailable.',
    }],
  });

  try {
    await renderDetail(root);
    assert.match(container.textContent ?? '', /Capability diagnostics/);
    assert.match(container.textContent ?? '', /Authenticated production backend is unavailable/);
    assert.match(container.textContent ?? '', /Invocation remains unavailable/);
    assert.equal(container.querySelector('[role="alert"]')?.textContent?.includes(
      'production_backend_unavailable',
    ), true);
    assert.equal(container.querySelector<HTMLButtonElement>('button[type="submit"]')?.disabled, true);
  } finally {
    act(() => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
  }
});

test('PackDetail exposes required Profile Packs without revoke or toggle actions', async () => {
  const previousState = useAppStore.getState();
  const {dom, container, root} = createSurface();
  configureStore({...pack, required: true});

  try {
    await renderDetail(root);
    assert.match(container.textContent ?? '', /Required by active execution Profile · profile-a/);
    assert.match(container.textContent ?? '', /Host-global artifact inventory and install state/);
    assert.equal(container.querySelector('[role="switch"]'), null);
    assert.equal(container.querySelector('[aria-label^="Revoke approval"]'), null);
  } finally {
    act(() => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
  }
});
