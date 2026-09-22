import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {MemoryRouter, Route, Routes} from 'react-router';

import {DialogContainer} from '@/src/components/ui/DialogContainer';
import {ApiContractError} from '@/src/lib/api';
import {type Pack, useAppStore} from '@/src/store';
import {Packs} from './Packs';

const samplePack: Pack = {
  id: 'research-pack',
  name: 'Research Pack',
  version: '1.2.3',
  type: 'community',
  installed: true,
  enabled: true,
  description: 'Research tools',
  artifactDigest: 'sha256:research-artifact',
  profileId: 'profile-a',
  workspaceId: 'workspace-a',
  profileRevision: 'sha256:profile-a',
  planDigest: 'sha256:plan-a',
  catalogRevision: 'catalog-a',
  approvalStatus: 'approved',
  approvalReason: null,
  approved: true,
  hashValid: true,
  criticalChanged: false,
  approvalIssues: [],
  capabilities: [],
  flows: [],
  dependencies: [],
};

const revokedPack: Pack = {
  ...samplePack,
  enabled: false,
  approvalStatus: 'revoked',
  approvalReason: 'approval_revoked',
  approved: false,
  approvalIssues: ['approval_revoked'],
};

function createSurface(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/packs',
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

async function renderSurface(root: Root): Promise<void> {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/packs']}>
        <Routes>
          <Route path="/packs" element={<><Packs /><DialogContainer /></>} />
        </Routes>
      </MemoryRouter>,
    );
  });
}

function buttonWithText(container: ParentNode, text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === text,
  );
  assert.ok(button, `button ${text} should be present`);
  return button;
}

const serialTestOptions = {concurrency: false};

test('Pack approval revocation opens an accessible confirmation and can be cancelled', serialTestOptions, async () => {
  const previousState = useAppStore.getState();
  const {dom, container, root} = createSurface();
  let revokeCount = 0;
  useAppStore.setState({
    packs: [samplePack],
    dialog: null,
    packApprovalPending: {},
    loadPacks: async () => {},
    revokePackApproval: async () => {
      revokeCount += 1;
    },
  });
  await renderSurface(root);

  try {
    const revokeButton = container.querySelector<HTMLButtonElement>(
      '[aria-label="Revoke approval for Research Pack"]',
    );
    assert.ok(revokeButton);
    assert.match(revokeButton.className, /min-h-11/);
    assert.equal(revokeButton.disabled, false);

    act(() => revokeButton.click());
    await new Promise((resolve) => setTimeout(resolve, 0));
    const dialog = container.querySelector<HTMLElement>('[role="alertdialog"]');
    assert.ok(dialog);
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(document.activeElement, dialog);
    assert.match(dialog.textContent ?? '', /revoke Tobkiri approval and access/);
    assert.equal(dialog.getAttribute('aria-modal'), 'true');
    assert.equal(buttonWithText(dialog, 'Keep approval').disabled, false);

    await act(async () => buttonWithText(dialog, 'Keep approval').click());
    assert.equal(revokeCount, 0);
    assert.equal(container.querySelector('[role="alertdialog"]'), null);
    assert.ok(container.querySelector('[role="switch"]'));
  } finally {
    await act(async () => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
  }
});

test('successful Pack approval revocation refreshes state and removes enablement', serialTestOptions, async () => {
  const previousState = useAppStore.getState();
  const {dom, container, root} = createSurface();
  let revokeCount = 0;
  useAppStore.setState({
    packs: [samplePack],
    dialog: null,
    packApprovalPending: {},
    loadPacks: async () => {},
    revokePackApproval: async (id) => {
      revokeCount += 1;
      assert.equal(id, samplePack.id);
      useAppStore.setState({packs: [revokedPack]});
    },
  });
  await renderSurface(root);

  try {
    await act(async () => {
      container.querySelector<HTMLButtonElement>(
        '[aria-label="Revoke approval for Research Pack"]',
      )?.click();
    });
    const dialog = container.querySelector<HTMLElement>('[role="alertdialog"]');
    assert.ok(dialog);
    await act(async () => buttonWithText(dialog, 'Revoke approval').click());

    assert.equal(revokeCount, 1);
    assert.equal(container.querySelector('[role="alertdialog"]'), null);
    assert.match(container.textContent ?? '', /Approval revoked/);
    assert.match(container.textContent ?? '', /Approve again before enabling this Pack/);
    assert.equal(container.querySelector('[role="switch"]'), null);
    assert.equal(
      container.querySelector('[aria-label="Revoke approval for Research Pack"]'),
      null,
    );
    assert.ok(buttonWithText(container, 'Approve'));
  } finally {
    await act(async () => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
  }
});

test('failed Pack approval revocation stays approved and surfaces the typed failure', serialTestOptions, async () => {
  const previousState = useAppStore.getState();
  const {dom, container, root} = createSurface();
  const errors: string[] = [];
  useAppStore.setState({
    packs: [samplePack],
    dialog: null,
    packApprovalPending: {},
    loadPacks: async () => {},
    addToast: (message, type) => {
      if (type === 'error') errors.push(message);
    },
    revokePackApproval: async () => {
      const error = new ApiContractError('HTTP 409 approval_revocation_denied', {
        code: 'approval_revocation_denied',
      });
      useAppStore.getState().addToast(error.message, 'error');
      throw error;
    },
  });
  await renderSurface(root);

  const previousConsoleError = console.error;
  console.error = () => {};
  try {
    await act(async () => {
      container.querySelector<HTMLButtonElement>(
        '[aria-label="Revoke approval for Research Pack"]',
      )?.click();
    });
    const dialog = container.querySelector<HTMLElement>('[role="alertdialog"]');
    assert.ok(dialog);
    await act(async () => buttonWithText(dialog, 'Revoke approval').click());

    assert.deepEqual(errors, ['HTTP 409 approval_revocation_denied']);
    assert.ok(container.querySelector('[role="alertdialog"]'));
    assert.ok(container.querySelector('[role="switch"]'));
    assert.match(container.textContent ?? '', /Approved/);
    assert.match(container.textContent ?? '', /The confirmation could not be completed/);
    assert.match(container.textContent ?? '', /API_CONTRACT_REJECTED/);
    assert.match(container.textContent ?? '', /diagnostic diag-/);
    assert.doesNotMatch(container.textContent ?? '', /HTTP 409 approval_revocation_denied/);
    assert.doesNotMatch(container.textContent ?? '', /Approval revoked/);
  } finally {
    console.error = previousConsoleError;
    await act(async () => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
  }
});

test('approval confirmation prevents double submission while the revoke is pending', serialTestOptions, async () => {
  const previousState = useAppStore.getState();
  const {dom, container, root} = createSurface();
  let revokeCount = 0;
  let release: (() => void) | undefined;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  useAppStore.setState({
    packs: [samplePack],
    dialog: null,
    packApprovalPending: {},
    loadPacks: async () => {},
    revokePackApproval: async () => {
      revokeCount += 1;
      await pending;
    },
  });
  await renderSurface(root);

  try {
    await act(async () => {
      container.querySelector<HTMLButtonElement>(
        '[aria-label="Revoke approval for Research Pack"]',
      )?.click();
    });
    const dialog = container.querySelector<HTMLElement>('[role="alertdialog"]');
    assert.ok(dialog);
    const confirmButton = buttonWithText(dialog, 'Revoke approval');
    await act(async () => {
      confirmButton.click();
      confirmButton.click();
    });
    assert.equal(revokeCount, 1);
    assert.equal(confirmButton.disabled, true);
    assert.match(confirmButton.textContent ?? '', /Revoking approval/);

    release?.();
    await act(async () => pending);
    assert.equal(container.querySelector('[role="alertdialog"]'), null);
  } finally {
    release?.();
    await act(async () => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
  }
});
