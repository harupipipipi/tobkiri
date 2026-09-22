import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router';
import { useAppStore } from '@/src/store';
import { useT } from '@/src/lib/i18n';
import { Button } from '@/src/components/ui/Button';
import { Badge } from '@/src/components/ui/Badge';
import { Switch } from '@/src/components/ui/Switch';
import { Card, CardHeader, CardTitle, CardContent } from '@/src/components/ui/Card';
import { panelRoutes } from '@/src/lib/routes';
import { ArrowLeft } from 'lucide-react';
import { InlineLoadError } from '@/src/components/ui/InlineLoadError';
import { FileInspectOperation } from '@/src/components/packs/FileInspectOperation';
import { PackDiagnostics } from '@/src/components/packs/PackDiagnostics';
import { PackVMLifecyclePanel } from '@/src/components/packs/PackVMLifecyclePanel';
import { userSafePackVMError } from '@/src/lib/packvmLifecycle';

export function PackDetail() {
  const t = useT();
  const { id } = useParams();
  const navigate = useNavigate();
  const packs = useAppStore(state => state.packs);
  const packsLoading = useAppStore(state => state.packsLoading);
  const packsError = useAppStore(state => state.packsError);
  const packTogglePending = useAppStore(state => state.packTogglePending);
  const packInstallPending = useAppStore(state => state.packInstallPending);
  const packApprovalPending = useAppStore(state => state.packApprovalPending);
  const packMutationUnknown = useAppStore(state => state.packMutationUnknown);
  const packOperationUnknown = useAppStore(state => state.packOperationUnknown);
  const frontendCatalog = useAppStore(state => state.frontendCatalog);
  const frontendCatalogLoading = useAppStore(state => state.frontendCatalogLoading);
  const frontendCatalogError = useAppStore(state => state.frontendCatalogError);
  const packVmDoctor = useAppStore(state => state.packVmDoctor);
  const packOperationPending = useAppStore(state => state.packOperationPending);
  const loadPacks = useAppStore(state => state.loadPacks);
  const loadFrontendCatalog = useAppStore(state => state.loadFrontendCatalog);
  const invokePackOperation = useAppStore(state => state.invokePackOperation);
  const installPack = useAppStore(state => state.installPack);
  const approvePack = useAppStore(state => state.approvePack);
  const revokePackApproval = useAppStore(state => state.revokePackApproval);
  const showDialog = useAppStore(state => state.showDialog);
  const togglePack = useAppStore(state => state.togglePack);
  const addToast = useAppStore(state => state.addToast);
  const [installing, setInstalling] = useState(false);
  const [approving, setApproving] = useState(false);

  const pack = packs.find(p => p.id === id);
  const mutationResultUnknown = Boolean(
    pack && (
      Object.values(packMutationUnknown).some((record) => record.metadata.pack_id === pack.id)
      || Object.values(packOperationUnknown).some((record) => record.metadata.pack_id === pack.id)
    ),
  );

  useEffect(() => {
    if (packs.length === 0) void loadPacks();
  }, [packs.length, loadPacks]);

  useEffect(() => {
    if (packVmDoctor?.ready) {
      void loadFrontendCatalog();
    }
  }, [
    loadFrontendCatalog,
    packVmDoctor,
    pack?.id,
    pack?.installed,
    pack?.approved,
    pack?.enabled,
    pack?.approvalStatus,
  ]);

  if (packsLoading && packs.length === 0) {
    return (
      <div className="flex flex-1 flex-col gap-5 p-6" role="status" aria-label={t('pack.loading')}>
        <div className="h-8 w-64 animate-pulse rounded bg-bg-hover" />
        <div className="grid gap-6 lg:grid-cols-2">
          {[0, 1, 2].map((item) => <div key={item} className="h-48 animate-pulse rounded-xl border border-border bg-bg-card" />)}
        </div>
      </div>
    );
  }

  if (packsError && !pack) {
    return (
      <div className="flex flex-1 items-center justify-center p-6">
        <div className="w-full max-w-xl">
          <InlineLoadError
            title="Pack details could not be loaded"
            message={packsError}
            onRetry={() => void loadPacks()}
            retrying={packsLoading}
          />
        </div>
      </div>
    );
  }

  if (!pack) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center">
          <p className="text-sm font-medium text-text-main">Pack not found</p>
          <Button className="mt-3" variant="outline" onClick={() => navigate(panelRoutes.packs)}>Back to packs</Button>
        </div>
      </div>
    );
  }

  const handleToggle = async () => {
    const key = pack.enabled ? 'packs.toggle_off' : 'packs.toggle_on';
    if (await togglePack(pack.id)) addToast(t(key, { name: pack.name }), 'success');
  };

  const handleInstall = async () => {
    setInstalling(true);
    try {
      await installPack(pack.id);
    } finally {
      setInstalling(false);
    }
  };

  const handleApprove = async () => {
    setApproving(true);
    try {
      await approvePack(pack.id);
    } finally {
      setApproving(false);
    }
  };

  const approvalRevoked = pack.approvalStatus === 'revoked'
    || pack.approvalReason === 'approval_revoked'
    || pack.approvalIssues.includes('approval_revoked');

  const operations = packVmDoctor?.ready ? (pack.operations ?? []) : [];
  const diagnostics = (frontendCatalog?.diagnostics ?? []).filter((diagnostic) => (
    diagnostic.owner_pack_id === pack.id || diagnostic.pack_id === pack.id
  ));
  const backendUnavailableForOperation = (operationId: string) => diagnostics.some((diagnostic) => (
    diagnostic.code === 'production_backend_unavailable'
    && diagnostic.operation_id === operationId
  ));
  const contributionForOperation = (operationId: string, contractId: string) => (
    backendUnavailableForOperation(operationId)
      ? null
      : frontendCatalog?.contributions.find((contribution) => (
        contribution.owner_pack_id === pack.id
        && contribution.action_contract === contractId
        && (
          contribution.operation_id === operationId
          || contribution.contribution_id === operationId
          || contribution.label === operationId
        )
      )) ?? null
  );

  const handleRevoke = () => {
    if (!pack.installed || !pack.approved || pack.type === 'core' || pack.required) return;
    showDialog({
      title: `Revoke ${pack.name} approval?`,
      message: `This will revoke Tobkiri approval and access for ${pack.name}. The Pack will be disabled, and its capabilities will be unavailable until a new approval succeeds.`,
      confirmText: 'Revoke approval',
      confirmPendingText: 'Revoking approval…',
      cancelText: 'Keep approval',
      onConfirm: () => revokePackApproval(pack.id),
    });
  };

  return (
    <div className="flex-1 overflow-y-auto page-enter">
      <div className="mx-auto max-w-4xl px-6 py-8 flex flex-col gap-6">
        {/* Header */}
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <Button variant="ghost" size="icon" onClick={() => navigate(panelRoutes.packs)} aria-label="Back to packs">
              <ArrowLeft className="h-4 w-4" />
            </Button>
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <h1 className="text-xl font-semibold tracking-tight text-text-main">{pack.name}</h1>
                <Badge variant="outline">{pack.version}</Badge>
                <Badge variant={pack.type === 'core' ? 'default' : 'secondary'}>{pack.type}</Badge>
                <Badge variant={pack.installed ? 'success' : 'outline'}>
                  {pack.installed ? 'Installed' : 'Available'}
                </Badge>
                <Badge variant={pack.approved ? 'success' : approvalRevoked ? 'destructive' : 'warning'}>
                  {pack.approved ? 'Approved' : approvalRevoked ? 'Approval revoked' : 'Needs approval'}
                </Badge>
              </div>
              <p className="mt-0.5 text-sm text-text-muted">{pack.description}</p>
            </div>
          </div>
          <div className="flex items-center gap-3 shrink-0">
            {!pack.installed ? (
              <Button
                size="sm"
                onClick={() => void handleInstall()}
                loading={installing || Boolean(packInstallPending[pack.id])}
                disabled={mutationResultUnknown}
              >
                Install
              </Button>
            ) : !pack.approved ? (
              <div className="flex items-center gap-3">
                {approvalRevoked ? (
                  <span className="max-w-56 text-right text-xs text-text-muted" role="status">
                    Tobkiri approval revoked. Approve again before enabling this Pack.
                  </span>
                ) : null}
                <Button size="sm" onClick={() => void handleApprove()} loading={approving} disabled={mutationResultUnknown}>
                  Approve
                </Button>
              </div>
            ) : pack.required ? (
              <Badge variant="secondary">Required by Defaults Profile</Badge>
            ) : (
              <>
                <Button
                  variant="destructive"
                  size="sm"
                  className="min-h-11"
                  onClick={handleRevoke}
                  loading={Boolean(packApprovalPending[pack.id])}
                  aria-busy={Boolean(packApprovalPending[pack.id])}
                  disabled={pack.type === 'core' || Boolean(packApprovalPending[pack.id]) || mutationResultUnknown}
                  aria-label={`Revoke approval for ${pack.name}`}
                  title={pack.type === 'core' ? 'Core Packs cannot have approval revoked.' : undefined}
                >
                  {packApprovalPending[pack.id] ? 'Revoking approval…' : 'Revoke approval'}
                </Button>
                <span className="text-sm text-text-muted">{pack.enabled ? t('packs.enabled') : t('packs.disabled')}</span>
                <Switch
                  checked={pack.enabled}
                  disabled={
                    pack.type === 'core'
                    || Boolean(packTogglePending[pack.id])
                    || Boolean(packApprovalPending[pack.id])
                    || mutationResultUnknown
                  }
                  onCheckedChange={() => { void handleToggle(); }}
                  aria-label={`Toggle ${pack.name}`}
                  aria-busy={Boolean(packTogglePending[pack.id])}
                  title={pack.type === 'core' ? 'Core Packs cannot be disabled.' : undefined}
                />
              </>
            )}
          </div>
        </div>
        {mutationResultUnknown ? (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-300/70 bg-amber-50/70 px-4 py-3 text-sm text-amber-800 dark:border-amber-800/60 dark:bg-amber-950/20 dark:text-amber-200" role="alert">
            <span>The result of a Pack mutation is unknown. Refresh the authoritative catalog before trying again.</span>
            <Button type="button" variant="outline" size="sm" onClick={() => void loadPacks(true)}>Refresh catalog</Button>
          </div>
        ) : null}

        <Card>
          <CardHeader>
            <CardTitle>v4 artifact binding</CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="grid gap-3 text-xs text-text-muted sm:grid-cols-2">
              <div>
                <dt className="font-medium text-text-main">Artifact digest</dt>
                <dd className="mt-1 break-all font-mono">{pack.artifactDigest || 'Unavailable'}</dd>
              </div>
              <div>
                <dt className="font-medium text-text-main">Catalog revision</dt>
                <dd className="mt-1 break-all font-mono">{pack.catalogRevision || 'Unavailable'}</dd>
              </div>
              <div>
                <dt className="font-medium text-text-main">Profile revision</dt>
                <dd className="mt-1 break-all font-mono">{pack.profileRevision || 'Unavailable'}</dd>
              </div>
              <div>
                <dt className="font-medium text-text-main">Plan digest</dt>
                <dd className="mt-1 break-all font-mono">{pack.planDigest || 'Unavailable'}</dd>
              </div>
            </dl>
          </CardContent>
        </Card>

        <PackVMLifecyclePanel />

        {/* Content grid */}
        <div className="grid gap-6 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>{t('pack.capabilities')}</CardTitle>
            </CardHeader>
            <CardContent>
              {pack.capabilities.length === 0 ? (
                <p className="text-sm text-text-muted">No capabilities registered.</p>
              ) : (
                <ul className="space-y-3">
                  {pack.capabilities.map((cap, i) => (
                    <li key={i} className="flex flex-col gap-0.5">
                      <span className="text-sm font-medium text-text-main">{cap.name}</span>
                      <span className="text-xs text-text-muted">{cap.description}</span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t('pack.flows')}</CardTitle>
            </CardHeader>
            <CardContent>
              {pack.flows.length === 0 ? (
                <p className="text-sm text-text-muted">No flows available.</p>
              ) : (
                <ul className="space-y-2">
                  {pack.flows.map((flow, i) => (
                    <li key={i} className="rounded-lg border border-border p-3">
                      <span className="text-sm font-medium text-text-main">{flow}</span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t('pack.dependencies')}</CardTitle>
            </CardHeader>
            <CardContent>
              {pack.dependencies.length === 0 ? (
                <p className="text-sm text-text-muted">No dependencies.</p>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {pack.dependencies.map((dep, i) => (
                    <Badge key={i} variant="secondary">{dep}</Badge>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        <PackDiagnostics diagnostics={diagnostics} />

        <Card>
          <CardHeader>
            <CardTitle>Declared operations</CardTitle>
            <p className="text-sm leading-relaxed text-text-muted">
              Operations are callable only when Tobkiri exposes the Pack contribution in the current verified v4 catalog.
            </p>
          </CardHeader>
          <CardContent>
            {!packVmDoctor?.ready ? (
              <p className="text-sm text-text-muted" role="status">
                Pack operations are hidden until PackVM doctor reports a healthy attestation.
              </p>
            ) : frontendCatalogLoading ? (
              <p className="text-sm text-text-muted" role="status">Loading the verified capability catalog…</p>
            ) : frontendCatalogError ? (
              <p className="text-sm text-destructive" role="alert">
                {userSafePackVMError(frontendCatalogError)}
              </p>
            ) : operations.length === 0 ? (
              <p className="text-sm text-text-muted">No operations declared by this Pack.</p>
            ) : (
              <ul className="space-y-3">
                {operations.map((operation) => {
                  const contribution = contributionForOperation(operation.operationId, operation.contractId);
                  const callable = operation.invokable
                    && Boolean(contribution)
                    && !approvalRevoked
                    && !backendUnavailableForOperation(operation.operationId);
                  return (
                    <li className="rounded-lg border border-border p-3" key={operation.operationId}>
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div className="min-w-0">
                          <p className="break-all text-sm font-medium text-text-main">{operation.operationId}</p>
                          <p className="mt-1 break-all text-xs text-text-muted">Contract: {operation.contractId}</p>
                          <p className="mt-1 break-all text-xs text-text-muted">Provider: {operation.providerId}</p>
                        </div>
                        <Badge variant={callable ? 'success' : 'secondary'}>
                          {callable ? 'Callable' : 'Not callable'}
                        </Badge>
                      </div>
                      {operation.capabilities.length > 0 ? (
                        <div className="mt-3 flex flex-wrap gap-2">
                          {operation.capabilities.map((capability) => (
                            <Badge key={capability} variant="outline">{capability}</Badge>
                          ))}
                        </div>
                      ) : null}
                      {!callable ? (
                        <p className="mt-3 text-xs text-text-muted">
                          {approvalRevoked
                            ? 'Approval is revoked; invocation is unavailable.'
                            : operation.invokable
                              ? 'Waiting for a verified Pack contribution from Tobkiri.'
                              : 'Tobkiri has not exposed a verified capability route.'}
                        </p>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            )}
          </CardContent>
        </Card>

        {operations.map((operation) => operation.operationId === 'rumi_file_inspect_pack.file-inspect' ? (
          <FileInspectOperation
            key={`${operation.operationId}-surface`}
            operation={operation}
            pack={pack}
            contributionVerified={Boolean(contributionForOperation(operation.operationId, operation.contractId))
              && !backendUnavailableForOperation(operation.operationId)
              && !frontendCatalog?.quarantined_pack_ids.includes(pack.id)}
            pending={Boolean(packOperationPending[`${pack.id}:${operation.operationId}`])
              || Object.values(packOperationUnknown).some((record) => (
                record.metadata.pack_id === pack.id
                && record.metadata.operation_id === operation.operationId
              ))}
            onInvoke={(payload) => invokePackOperation(pack.id, operation.operationId, payload)}
          />
        ) : null)}
      </div>
    </div>
  );
}
