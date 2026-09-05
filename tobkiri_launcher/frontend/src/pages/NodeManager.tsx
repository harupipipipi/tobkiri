import {useEffect} from 'react';
import {Link} from 'react-router';
import {Network, ShieldAlert, ShieldCheck} from 'lucide-react';

import {AdvancedSurfaceFrame, EmptySurfacePanel} from '@/src/components/advanced/AdvancedSurfaceFrame';
import {RuntimeEvidenceCard} from '@/src/components/advanced/RuntimeEvidenceCard';
import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {Card, CardContent, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {LAUNCHER_ADVANCED_VIEWS} from '@/src/lib/advancedSurfaces';
import {extractExactPackDescriptors, type RuntimePackDescriptor} from '@/src/lib/runtimeSurface';
import {panelRoutes} from '@/src/lib/routes';
import {useAppStore, type Pack} from '@/src/store';

/** Require the Pack control rows to stay bound to the accepted Profile snapshot. */
export function exactPackControlCatalogBinding(
  packs: readonly Pick<Pack, 'profileRevision' | 'planDigest'>[],
  surface: {profile_revision: string; plan_digest: string} | null,
): boolean {
  return Boolean(
    surface
    && packs.length > 0
    && packs.every((pack) => (
      pack.profileRevision === surface.profile_revision
      && pack.planDigest === surface.plan_digest
    )),
  );
}

/** Require an active Pack row to match the control catalog's exact artifact/state. */
export function exactActivePackJoin(
  pack: Pick<Pack, 'id' | 'version' | 'artifactDigest' | 'installed' | 'enabled' | 'approved' | 'required'>,
  activeRow: Pick<RuntimePackDescriptor, 'pack_id' | 'version' | 'artifact_digest' | 'installed' | 'enabled' | 'approved' | 'required'> | undefined,
): boolean {
  return Boolean(
    activeRow
    && activeRow.pack_id === pack.id
    && activeRow.version === pack.version
    && activeRow.artifact_digest === pack.artifactDigest
    && activeRow.installed === pack.installed
    && activeRow.enabled === pack.enabled
    && activeRow.approved === pack.approved
    && activeRow.required === Boolean(pack.required),
  );
}

export function NodeManager() {
  const surface = useRuntimeSurface<unknown>('packs');
  const descriptor = LAUNCHER_ADVANCED_VIEWS.nodeManager;
  const activeRows = surface.data ? extractExactPackDescriptors(surface.data.data) : [];
  const packs = useAppStore((state) => state.packs);
  const packsLoading = useAppStore((state) => state.packsLoading);
  const loadPacks = useAppStore((state) => state.loadPacks);
  const installPack = useAppStore((state) => state.installPack);
  const approvePack = useAppStore((state) => state.approvePack);
  const revokePackApproval = useAppStore((state) => state.revokePackApproval);
  const togglePack = useAppStore((state) => state.togglePack);
  const pendingInstall = useAppStore((state) => state.packInstallPending);
  const pendingApproval = useAppStore((state) => state.packApprovalPending);
  const pendingToggle = useAppStore((state) => state.packTogglePending);
  const packMutationUnknown = useAppStore((state) => state.packMutationUnknown);

  useEffect(() => {
    void loadPacks();
  }, [loadPacks]);

  const activeById = new Map(activeRows.map((row) => [row.pack_id, row]));
  const controlCatalogRevisions = new Set(packs.map((pack) => pack.catalogRevision).filter(Boolean));
  const controlCatalogStable = packs.length > 0 && controlCatalogRevisions.size === 1;
  const profilePlanBound = exactPackControlCatalogBinding(packs, surface.data);
  const runtimeReady = surface.status === 'ready' && !surface.stale;
  const canUseLifecycle = runtimeReady && controlCatalogStable && profilePlanBound;

  const refresh = async () => {
    await Promise.all([surface.refresh(true), loadPacks()]);
  };

  return (
    <AdvancedSurfaceFrame
      descriptor={descriptor}
      state={{status: surface.status, stale: surface.stale, error: surface.error}}
      onRetry={() => void refresh()}
    >
      {surface.data ? <RuntimeEvidenceCard envelope={surface.data} title="Pack lifecycle provenance" /> : null}
      {surface.status === 'ready' && packs.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><Network className="h-4 w-4" aria-hidden="true" />Pack control catalog</CardTitle>
            <p className="text-sm leading-6 text-text-muted">The canonical Pack control catalog is the complete list. Active v4 rows are joined as evidence; missing or stale joins lock actions that would change active runtime state.</p>
            {!canUseLifecycle ? <p className="mt-2 text-sm text-amber-700 dark:text-amber-300" role="alert">Pack lifecycle actions are locked until the control catalog is stable and bound to the accepted Profile revision and Plan digest.</p> : null}
          </CardHeader>
          <CardContent className="grid gap-3">
            {packs.map((pack) => {
              const activeRow = activeById.get(pack.id);
              const activeJoinRequired = Boolean(activeRow) || (pack.installed && pack.approved);
              const activeJoinValid = !activeJoinRequired || exactActivePackJoin(pack, activeRow);
              const canAct = canUseLifecycle && (!activeJoinRequired || activeJoinValid);
              const mutationResultUnknown = Object.values(packMutationUnknown).some((record) => record.metadata.pack_id === pack.id);
              const shownName = activeRow?.display_name ?? pack.name;
              const shownDigest = activeRow?.artifact_digest ?? pack.artifactDigest;
              const joinWarning = activeJoinRequired && !activeRow
                ? 'Active Pack evidence is not present in this snapshot; runtime actions remain locked.'
                : activeJoinRequired && !activeJoinValid
                  ? 'Active Pack evidence does not match this Pack artifact or lifecycle state; runtime actions remain locked.'
                  : !activeRow
                    ? 'Not in the active closure yet; install or approval can prepare it for Profile selection.'
                    : null;
              return (
                <article key={pack.id} className="grid gap-3 rounded-lg border border-border bg-bg-main p-4 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link to={panelRoutes.packDetail(pack.id)} className="break-all text-sm font-semibold text-text-main underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]">{shownName}</Link>
                      <Badge variant="outline">{pack.type}</Badge>
                      <Badge variant={pack.approved ? 'success' : 'warning'}>{pack.approved ? 'approved' : 'approval required'}</Badge>
                      {pack.required ? <Badge variant="secondary">required</Badge> : null}
                      {pack.installed ? <Badge variant={pack.enabled ? 'success' : 'secondary'}>{pack.enabled ? 'enabled' : 'disabled'}</Badge> : <Badge variant="outline">not installed</Badge>}
                    </div>
                    <p className="mt-1 break-all font-mono text-xs text-text-muted">{pack.id} · v{pack.version}</p>
                    <p className="mt-1 break-all font-mono text-xs text-text-muted">{shownDigest}</p>
                    {joinWarning ? <p className="mt-2 flex items-start gap-1 text-xs text-amber-700 dark:text-amber-300"><ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />{joinWarning}</p> : null}
                    {mutationResultUnknown ? <p className="mt-2 text-xs text-amber-700 dark:text-amber-300" role="alert">A mutation result is unknown. Refresh the authoritative catalog before trying again.</p> : null}
                    {activeRow ? <p className="mt-2 text-xs text-text-muted">{activeRow.invokable_operations.length} exact invokable operation binding(s) in the active snapshot.</p> : null}
                  </div>
                  <div className="flex flex-wrap items-center gap-2 lg:justify-end">
                    {!pack.installed ? (
                      <Button type="button" size="sm" onClick={() => void installPack(pack.id)} disabled={!canAct || Boolean(pendingInstall[pack.id]) || mutationResultUnknown} loading={Boolean(pendingInstall[pack.id])}>Install</Button>
                    ) : !pack.approved ? (
                      <Button type="button" size="sm" onClick={() => void approvePack(pack.id)} disabled={!canAct || Boolean(pendingApproval[pack.id]) || mutationResultUnknown} loading={Boolean(pendingApproval[pack.id])}>Approve</Button>
                    ) : (
                      <>
                        <Button type="button" size="sm" variant="outline" onClick={() => void togglePack(pack.id)} disabled={!canAct || pack.required || Boolean(pendingToggle[pack.id]) || mutationResultUnknown} loading={Boolean(pendingToggle[pack.id])}>{pack.enabled ? 'Disable' : 'Enable'}</Button>
                        <Button type="button" size="sm" variant="destructive" onClick={() => void revokePackApproval(pack.id)} disabled={!canAct || pack.required || pack.type === 'core' || Boolean(pendingApproval[pack.id]) || mutationResultUnknown} loading={Boolean(pendingApproval[pack.id])}>Revoke</Button>
                      </>
                    )}
                    {pack.approved ? <ShieldCheck className="h-4 w-4 text-emerald-600" aria-label="Pack approved" /> : null}
                  </div>
                </article>
              );
            })}
          </CardContent>
        </Card>
      ) : packsLoading ? (
        <div className="rounded-xl border border-border bg-bg-card px-4 py-5 text-sm text-text-muted" role="status">Loading the canonical Pack control catalog…</div>
      ) : (
        <EmptySurfacePanel
          icon={<Network className="size-6" />}
          title="Exact Pack catalog data is not available"
          message="Node Manager maps only the verified Packs projection and existing Pack lifecycle actions. It does not restore a node registry or synthesize nodes from principals."
        />
      )}
    </AdvancedSurfaceFrame>
  );
}
