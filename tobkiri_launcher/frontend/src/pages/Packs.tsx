import { useState, useEffect } from 'react';
import { Link } from 'react-router';
import { useAppStore, type Pack } from '@/src/store';
import { useT } from '@/src/lib/i18n';
import { Input } from '@/src/components/ui/Input';
import { Badge } from '@/src/components/ui/Badge';
import { Switch } from '@/src/components/ui/Switch';
import { Card } from '@/src/components/ui/Card';
import { panelRoutes } from '@/src/lib/routes';
import { AlertTriangle, CircleHelp, Search, Package, ShieldCheck } from 'lucide-react';
import { Button } from '@/src/components/ui/Button';
import { CopyErrorButton } from '@/src/components/ui/CopyErrorButton';
import { InlineLoadError } from '@/src/components/ui/InlineLoadError';
import { PackScopeSummary } from '@/src/components/packs/PackScopeSummary';
import { isPackInCatalogScope } from '@/src/lib/packScope';
import {PackConflictCenter} from '@/src/components/packs/PackConflictCenter';

type BadgeVariant = 'default' | 'secondary' | 'destructive' | 'outline' | 'success' | 'warning';

function approvalBadgeVariant(pack: Pack): BadgeVariant {
  if (!pack.installed) return 'outline';
  if (isApprovalRevoked(pack)) return 'destructive';
  if (pack.approved) return 'success';
  if (pack.approvalStatus === 'pending' || pack.approvalStatus === 'installed') return 'warning';
  if (pack.criticalChanged || ['blocked', 'error', 'modified'].includes(pack.approvalStatus)) return 'destructive';
  return 'warning';
}

function approvalBadgeLabel(pack: Pack): string {
  if (!pack.installed) return 'Install required';
  if (isApprovalRevoked(pack)) return 'Approval revoked';
  if (pack.approved) return 'Approved';
  if (pack.approvalStatus === 'pending' || pack.approvalStatus === 'installed') return 'Needs approval';
  if (pack.approvalStatus === 'blocked') return 'Blocked';
  if (pack.criticalChanged || pack.approvalStatus === 'modified') return 'Modified';
  return 'Approval unknown';
}

function approvalIssueText(pack: Pack): string {
  if (!pack.installed) return 'Install this Pack before requesting approval.';
  if (isApprovalRevoked(pack)) {
    return 'Tobkiri approval has been revoked. Approve again before enabling this Pack.';
  }
  return pack.approvalReason || pack.approvalIssues[0] || 'Pack approval needs attention.';
}

function isApprovalRevoked(pack: Pack): boolean {
  return pack.approvalStatus === 'revoked'
    || pack.approvalReason === 'approval_revoked'
    || pack.approvalIssues.includes('approval_revoked');
}

function PackListSkeleton() {
  return (
    <div className="grid gap-3" role="status" aria-label="Loading packs">
      {[0, 1, 2].map((item) => (
        <div key={item} className="h-28 animate-pulse rounded-xl border border-border bg-bg-card" />
      ))}
    </div>
  );
}

export function Packs() {
  const t = useT();
  const packs = useAppStore(state => state.packs);
  const packCatalogBinding = useAppStore(state => state.packCatalogBinding);
  const packConflicts = useAppStore(state => state.packConflicts);
  const packRepairPending = useAppStore(state => state.packRepairPending);
  const runPackRepairAction = useAppStore(state => state.runPackRepairAction);
  const packsLoading = useAppStore(state => state.packsLoading);
  const packsError = useAppStore(state => state.packsError);
  const packInstallPending = useAppStore(state => state.packInstallPending);
  const packTogglePending = useAppStore(state => state.packTogglePending);
  const packApprovalPending = useAppStore(state => state.packApprovalPending);
  const packMutationUnknown = useAppStore(state => state.packMutationUnknown);
  const packLegacyRecovery = useAppStore(state => state.packLegacyRecovery);
  const loadPacks = useAppStore(state => state.loadPacks);
  const installPack = useAppStore(state => state.installPack);
  const approvePack = useAppStore(state => state.approvePack);
  const revokePackApproval = useAppStore(state => state.revokePackApproval);
  const addToast = useAppStore(state => state.addToast);
  const showDialog = useAppStore(state => state.showDialog);
  const togglePack = useAppStore(state => state.togglePack);
  const clearAbsentLegacyPackMutation = useAppStore(
    state => state.clearAbsentLegacyPackMutation,
  );
  const verifyPackMutationStatus = useAppStore(state => state.verifyPackMutationStatus);
  const [search, setSearch] = useState('');
  const [installingPackId, setInstallingPackId] = useState<string | null>(null);
  const [approvingPackId, setApprovingPackId] = useState<string | null>(null);
  const [verifyingMutationKey, setVerifyingMutationKey] = useState<string | null>(null);

  useEffect(() => {
    void loadPacks();
  }, [loadPacks]);

  const filteredPacks = packs.filter(pack => pack.name.toLowerCase().includes(search.toLowerCase()));

  const handleApprove = async (packId: string) => {
    setApprovingPackId(packId);
    try {
      await approvePack(packId);
    } finally {
      setApprovingPackId(null);
    }
  };

  const handleInstall = async (packId: string) => {
    setInstallingPackId(packId);
    try {
      await installPack(packId);
    } finally {
      setInstallingPackId(null);
    }
  };

  const handleVerifyMutation = async (key: string) => {
    setVerifyingMutationKey(key);
    try {
      await verifyPackMutationStatus(key);
    } finally {
      setVerifyingMutationKey(null);
    }
  };

  const handleRevoke = (pack: Pack) => {
    if (
      !isPackInCatalogScope(pack, packCatalogBinding)
      || !pack.installed
      || !pack.approved
      || pack.type === 'core'
      || pack.required
    ) return;
    showDialog({
      title: `Revoke ${pack.name} approval?`,
      message: `This will revoke Tobkiri approval and access for ${pack.name}. The Pack will be disabled, and its capabilities will be unavailable until a new approval succeeds.`,
      confirmText: 'Revoke approval',
      confirmPendingText: 'Revoking approval…',
      cancelText: 'Keep approval',
      onConfirm: () => revokePackApproval(pack.id),
    });
  };

  const handleToggle = async (pack: Pack) => {
    if (!isPackInCatalogScope(pack, packCatalogBinding)) return;
    if (await togglePack(pack.id)) {
      addToast(t(pack.enabled ? 'packs.toggle_off' : 'packs.toggle_on', {name: pack.name}), 'success');
    }
  };

  return (
    <div className="flex-1 overflow-y-auto page-enter">
      <div className="w-full py-8 pr-6 flex flex-col gap-6">
        {/* Header */}
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-text-main">{t('packs.title')}</h1>
          <p className="mt-1 text-sm text-text-muted">Manage installed packs and their capabilities.</p>
        </div>

        <PackScopeSummary
          binding={packCatalogBinding}
          packRows={packs}
          stale={Boolean(packsError && packs.length > 0)}
        />

        {packsError ? (
          <InlineLoadError
            title="Packs could not be loaded"
            message={packsError}
            onRetry={() => void loadPacks()}
            retrying={packsLoading}
            stale={packs.length > 0}
          />
        ) : null}

        <PackConflictCenter
          conflicts={packConflicts}
          pending={packRepairPending}
          onAction={runPackRepairAction}
        />

        {/* Search */}
        <div className="relative">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-text-muted" />
          <Input
            placeholder={t('packs.search')}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-10"
            aria-label="Search packs"
          />
        </div>

        {/* Pack list */}
        {packsLoading && packs.length === 0 ? (
          <PackListSkeleton />
        ) : packsError && packs.length === 0 ? null : filteredPacks.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-bg-hover">
              <Package className="h-5 w-5 text-text-muted" />
            </div>
            <h3 className="mt-4 text-base font-medium text-text-main">
              {search.trim() ? t('packs.not_found') : 'No packs available'}
            </h3>
            <p className="mt-1 text-sm text-text-muted">
              {search.trim() ? t('packs.try_different') : 'Catalog packs will appear here.'}
            </p>
          </div>
        ) : (
          <div className="grid gap-3">
            {filteredPacks.map(pack => {
              const packScopeAuthoritative = isPackInCatalogScope(pack, packCatalogBinding);
              const scopedProfileId = packCatalogBinding?.profile_id ?? 'unavailable';
              const unknownMutation = Object.values(packMutationUnknown).find(
                (record) => record.metadata.pack_id === pack.id,
              );
              const legacyRecovery = unknownMutation
                ? packLegacyRecovery[unknownMutation.key]
                : undefined;
              return (
              <Card key={pack.id} className="transition-all hover:shadow-[var(--shadow-md)] focus-within:shadow-[var(--shadow-md)]">
                {unknownMutation ? (
                  <div className="flex flex-wrap items-center justify-between gap-3 border-b border-amber-300/60 bg-amber-50/60 px-5 py-3 text-xs text-amber-800 dark:border-amber-800/60 dark:bg-amber-950/20 dark:text-amber-200" role="alert">
                    <CircleHelp className="h-4 w-4 shrink-0 text-amber-600" aria-hidden="true" data-error-icon="pack-mutation-unknown" />
                    <span className="min-w-0 flex-1">
                      {legacyRecovery
                        ? 'This legacy recovery lock is absent from the current data root and contradicts the authoritative catalog.'
                        : 'The result of a Pack mutation is unknown. Refresh the authoritative catalog before trying again.'}
                    </span>
                    <CopyErrorButton label="Copy unknown Pack mutation result" text="The result of a Pack mutation is unknown. Refresh the authoritative catalog before trying again." />
                    {legacyRecovery ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        onClick={() => showDialog({
                          title: 'Clear stale recovery lock?',
                          message: 'Tobkiri verified that this legacy request does not exist in the current data root and that the authoritative catalog does not show its expected result. Clearing removes only this exact local recovery lock. It does not install, approve, enable, disable, or send any Pack request.',
                          confirmText: 'Clear local lock',
                          cancelText: 'Keep lock',
                          onConfirm: () => clearAbsentLegacyPackMutation(
                            legacyRecovery.key,
                            legacyRecovery.requestId,
                          ),
                        })}
                      >
                        Review recovery
                      </Button>
                    ) : null}
                    {!legacyRecovery ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        loading={verifyingMutationKey === unknownMutation.key}
                        disabled={verifyingMutationKey !== null}
                        onClick={() => void handleVerifyMutation(unknownMutation.key)}
                      >
                        Verify status
                      </Button>
                    ) : null}
                    <Button type="button" size="sm" variant="outline" onClick={() => void loadPacks(true)}>Refresh catalog</Button>
                  </div>
                ) : null}
                <div className="flex items-center justify-between">
                  <Link
                    to={panelRoutes.packDetail(pack.id)}
                    aria-label={`Open ${pack.name} details`}
                    className="flex min-h-11 min-w-0 flex-1 cursor-pointer flex-col gap-1.5 rounded-l-xl p-5 text-inherit focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--ring-color)]"
                  >
                    <div className="flex items-center gap-2 flex-wrap">
                      <h3 className="text-sm font-semibold text-text-main">{pack.name}</h3>
                      <Badge variant="outline">{pack.version}</Badge>
                      <Badge variant={pack.type === 'core' ? 'default' : 'secondary'}>{pack.type}</Badge>
                      <Badge variant={pack.installed ? 'success' : 'outline'}>
                        {pack.installed ? 'Installed' : 'Available'}
                      </Badge>
                      {pack.installed ? (
                        <Badge variant={packScopeAuthoritative ? (pack.enabled ? 'success' : 'secondary') : 'warning'}>
                          {packScopeAuthoritative ? (pack.enabled ? 'Enabled' : 'Disabled') : 'Profile state unavailable'}
                        </Badge>
                      ) : null}
                      <Badge
                        variant={packScopeAuthoritative ? approvalBadgeVariant(pack) : 'warning'}
                        className="inline-flex items-center gap-1"
                      >
                        {packScopeAuthoritative && pack.installed && pack.approved ? (
                          <ShieldCheck className="h-3 w-3" />
                        ) : (
                          <AlertTriangle className="h-3 w-3" />
                        )}
                        {packScopeAuthoritative ? approvalBadgeLabel(pack) : 'Profile state unavailable'}
                      </Badge>
                    </div>
                    <p className="text-sm text-text-muted truncate">{pack.description}</p>
                    {packScopeAuthoritative && (!pack.installed || !pack.approved || pack.approvalIssues.length > 0) && (
                      <div className="flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400">
                        <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                        <span className="truncate">{approvalIssueText(pack)}</span>
                      </div>
                    )}
                  </Link>
                  <div className="mx-2 flex min-h-11 shrink-0 items-center gap-2">
                    {!pack.installed ? (
                      <Button
                        size="sm"
                        onClick={() => void handleInstall(pack.id)}
                        loading={installingPackId === pack.id || Boolean(packInstallPending[pack.id])}
                        disabled={!packScopeAuthoritative || installingPackId !== null || Object.values(packMutationUnknown).some((record) => record.metadata.pack_id === pack.id)}
                      >
                        Install
                      </Button>
                    ) : !pack.approved ? (
                      <Button
                        size="sm"
                        onClick={() => void handleApprove(pack.id)}
                        loading={approvingPackId === pack.id}
                        disabled={!packScopeAuthoritative || approvingPackId !== null || Object.values(packMutationUnknown).some((record) => record.metadata.pack_id === pack.id)}
                      >
                        Approve
                      </Button>
                    ) : null}
                    {pack.installed && pack.approved && pack.required ? (
                      <Badge variant={packScopeAuthoritative ? 'secondary' : 'warning'}>
                        {packScopeAuthoritative
                          ? `Required by active execution Profile · ${scopedProfileId}`
                          : 'Profile-scoped requirement unavailable'}
                      </Badge>
                    ) : pack.installed && pack.approved && !packScopeAuthoritative ? (
                      <Badge variant="warning">Profile-scoped Pack actions unavailable</Badge>
                    ) : pack.installed && pack.approved ? (
                      <>
                        <Button
                          variant="destructive"
                          size="sm"
                          className="min-h-11"
                          onClick={() => handleRevoke(pack)}
                          loading={Boolean(packApprovalPending[pack.id])}
                          aria-busy={Boolean(packApprovalPending[pack.id])}
                          disabled={!packScopeAuthoritative || pack.type === 'core' || Boolean(packApprovalPending[pack.id]) || Object.values(packMutationUnknown).some((record) => record.metadata.pack_id === pack.id)}
                          aria-label={`Revoke approval for ${pack.name}`}
                          title={pack.type === 'core' ? 'Core Packs cannot have approval revoked.' : undefined}
                        >
                          {packApprovalPending[pack.id] ? 'Revoking approval…' : 'Revoke approval'}
                        </Button>
                        <Switch
                          checked={pack.enabled}
                          disabled={
                            !packScopeAuthoritative
                            ||
                            pack.type === 'core'
                            || Boolean(packTogglePending[pack.id])
                            || Boolean(packApprovalPending[pack.id])
                            || Object.values(packMutationUnknown).some((record) => record.metadata.pack_id === pack.id)
                          }
                          aria-busy={Boolean(packTogglePending[pack.id])}
                          onCheckedChange={() => { void handleToggle(pack); }}
                          aria-label={`Toggle ${pack.name}`}
                          title={pack.type === 'core' ? 'Core Packs cannot be disabled.' : undefined}
                          className="relative after:absolute after:-inset-2.5"
                        />
                      </>
                    ) : null}
                  </div>
                </div>
              </Card>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
