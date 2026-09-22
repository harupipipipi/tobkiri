import {useCallback, useEffect, useMemo, useRef, useState, type ReactNode} from 'react';
import {AlertTriangle, CheckCircle2, Database, FileKey2, PackageCheck, RefreshCw, ShieldCheck} from 'lucide-react';

import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {Card, CardContent, CardDescription, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {ProfileCeremonyPanel} from '@/src/components/advanced/ProfileCeremonyPanel';
import type {RuntimeSurfaceState} from '@/src/hooks/useRuntimeSurface';
import {
  extractExactProfileCatalog,
  type RuntimeProfileCatalogEntry,
  type RuntimeProfileCatalogProjection,
} from '@/src/lib/runtimeSurface';
import type {ProfileActivateResult, ProfileCeremonyClient} from '@/src/lib/profileCeremony';
import type {Pack} from '@/src/store';

type CeremonyMode = 'catalog' | 'defaults';

function published(value: string | null | undefined): string {
  return value ?? 'not published';
}

function BindingField({label, value}: {label: string; value: string | null | undefined}) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd className="mt-1 break-all font-mono text-xs text-text-main">{published(value)}</dd>
    </div>
  );
}

function BindingCard({
  title,
  icon,
  children,
}: {
  title: string;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-border bg-bg-main p-4">
      <h4 className="flex items-center gap-2 text-sm font-semibold text-text-main">
        {icon}
        {title}
      </h4>
      <dl className="mt-3 grid gap-3 sm:grid-cols-2">{children}</dl>
    </section>
  );
}

function ProfileDefinitionDetails({entry}: {entry: RuntimeProfileCatalogEntry}) {
  const {base, shell, application} = entry.bindings;
  return (
    <div className="mt-4 flex flex-col gap-4" aria-label={`Details for Profile ${entry.profile_id}`}>
      <section className="rounded-lg border border-border bg-bg-main p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h3 className="text-base font-semibold text-text-main">{entry.display_name}</h3>
            <p className="mt-1 break-all font-mono text-xs text-text-muted">{entry.profile_id}</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {entry.active ? (
              <Badge variant="success"><CheckCircle2 className="mr-1 h-3 w-3" aria-hidden="true" />Active</Badge>
            ) : <Badge variant="outline">Available candidate</Badge>}
            <Badge variant={entry.available ? 'success' : 'destructive'}>{entry.available ? 'Verified' : 'Unavailable'}</Badge>
          </div>
        </div>
        <dl className="mt-4 grid gap-3 sm:grid-cols-2">
          <BindingField label="Profile definition digest" value={entry.definition.digest} />
          <BindingField label="Definition catalog revision" value={entry.definition.catalog_revision} />
          <BindingField label="Definition reference" value={entry.definition.ref} />
          <BindingField label="Source path" value={entry.definition.source_path} />
        </dl>
        {entry.diagnostics.length > 0 ? (
          <div className="mt-4 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm" role="alert">
            <p className="flex items-center gap-2 font-medium text-text-main"><AlertTriangle className="h-4 w-4 text-destructive" aria-hidden="true" />Profile is unavailable in the verified catalog.</p>
            <ul className="mt-2 list-disc space-y-1 pl-5 text-text-muted">
              {entry.diagnostics.map((diagnostic) => <li key={`${diagnostic.code}:${diagnostic.subject}`}>{diagnostic.code}: {diagnostic.subject}</li>)}
            </ul>
          </div>
        ) : null}
      </section>

      <div className="grid gap-4 lg:grid-cols-3">
        <BindingCard title="Base binding" icon={<Database className="h-4 w-4" aria-hidden="true" />}>
          <BindingField label="Pack ID" value={base.pack_id} />
          <BindingField label="Definition revision" value={base.definition_revision} />
          <BindingField label="Definition digest" value={base.definition_digest} />
          <BindingField label="Artifact digest" value={base.artifact_digest} />
        </BindingCard>
        <BindingCard title="Shell binding" icon={<ShieldCheck className="h-4 w-4" aria-hidden="true" />}>
          <BindingField label="Provider ID" value={shell.provider_id} />
          <BindingField label="Pack ID" value={shell.pack_id} />
          <BindingField label="Definition revision" value={shell.definition_revision} />
          <BindingField label="Definition digest" value={shell.definition_digest} />
          <BindingField label="Artifact digest" value={shell.artifact_digest} />
        </BindingCard>
        <BindingCard title="Application binding" icon={<FileKey2 className="h-4 w-4" aria-hidden="true" />}>
          {application ? (
            <>
              <BindingField label="Pack ID" value={application.pack_id} />
              <BindingField label="Artifact digest" value={application.artifact_digest} />
              <BindingField label="Artifact reference" value={application.artifact_ref} />
            </>
          ) : <p className="text-sm text-text-muted">No application binding was published.</p>}
        </BindingCard>
      </div>

      <section className="rounded-lg border border-border bg-bg-main p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h4 className="flex items-center gap-2 text-sm font-semibold text-text-main"><PackageCheck className="h-4 w-4" aria-hidden="true" />Authoritative Pack closure</h4>
          <Badge variant="outline">{entry.pack_closure.length} exact rows</Badge>
        </div>
        <div className="mt-3 flex flex-col gap-2">
          {entry.pack_closure.map((pack) => (
            <div key={pack.pack_id} className="grid gap-2 rounded-md border border-border/70 px-3 py-2 text-xs sm:grid-cols-[minmax(0,1.2fr)_8rem_minmax(0,1.5fr)] sm:items-center">
              <div className="min-w-0">
                <p className="truncate font-medium text-text-main">{pack.pack_id}</p>
                <p className="truncate text-text-muted">role: {pack.role} · version: {pack.version}</p>
              </div>
              <span className="font-mono text-text-muted">{pack.artifact_digest}</span>
              <span className="break-all font-mono text-text-muted">{pack.artifact_ref}</span>
            </div>
          ))}
        </div>
      </section>

      <dl className="grid gap-3 rounded-lg border border-border bg-bg-main p-4 sm:grid-cols-3">
        <BindingField label="Profile revision" value={entry.records.profile_revision} />
        <BindingField label="Profile lock digest" value={entry.records.profile_lock_digest} />
        <BindingField label="Resolved Plan digest" value={entry.records.plan_digest} />
        <BindingField label="Authority snapshot" value={entry.authority_snapshot.digest} />
        <BindingField label="Authority snapshot reference" value={entry.authority_snapshot.ref} />
        <BindingField label="Candidate state" value={entry.candidate.state} />
      </dl>
    </div>
  );
}

export function ProfileCatalogSelector({
  profileSurface,
  catalogSurface,
  packs,
  packsLoading,
  loadPacks,
  client,
  onActivated,
}: {
  profileSurface: RuntimeSurfaceState<unknown>;
  catalogSurface: RuntimeSurfaceState<RuntimeProfileCatalogProjection>;
  packs: Pack[];
  packsLoading: boolean;
  loadPacks: () => Promise<void>;
  client?: ProfileCeremonyClient;
  onActivated?: (result: ProfileActivateResult) => Promise<void>;
}) {
  const [selectedProfileId, setSelectedProfileId] = useState<string | null>(null);
  const [ceremonyMode, setCeremonyMode] = useState<CeremonyMode>('catalog');
  const [ceremonyBusy, setCeremonyBusy] = useState(false);
  const previousPackFingerprint = useRef<string | null>(null);

  const catalogProjection = useMemo(
    () => catalogSurface.data ? extractExactProfileCatalog(catalogSurface.data.data) : null,
    [catalogSurface.data],
  );
  const packFingerprint = useMemo(
    () => packs
      .map((pack) => [
        pack.id,
        pack.name,
        pack.version,
        pack.artifactDigest,
        pack.profileId,
        pack.profileRevision,
        pack.planDigest,
        pack.catalogRevision,
        pack.installed,
        pack.approved,
        pack.enabled,
        pack.required,
        pack.approvalStatus,
        pack.approvalReason,
        pack.hashValid,
        pack.criticalChanged,
      ].join(':'))
      .sort()
      .join('|'),
    [packs],
  );

  useEffect(() => {
    if (!catalogProjection) return;
    setSelectedProfileId((current) => (
      current && catalogProjection.profiles.some((entry) => entry.profile_id === current)
        ? current
        : catalogProjection.active_profile_id
    ));
  }, [catalogProjection]);

  useEffect(() => {
    if (packsLoading) return;
    if (previousPackFingerprint.current === null) {
      previousPackFingerprint.current = packFingerprint;
      return;
    }
    if (previousPackFingerprint.current === packFingerprint) return;
    previousPackFingerprint.current = packFingerprint;
    void catalogSurface.refresh(true);
  }, [catalogSurface.refresh, packFingerprint, packsLoading]);

  const selectedEntry = catalogProjection?.profiles.find((entry) => entry.profile_id === selectedProfileId) ?? null;
  const catalogCeremonyMode = ceremonyMode === 'catalog' && selectedEntry !== null;
  const handleActivated = useCallback(async (result: ProfileActivateResult) => {
    setSelectedProfileId(result.profile_id);
    await onActivated?.(result);
  }, [onActivated]);

  const showLoading = (catalogSurface.status === 'idle' || catalogSurface.status === 'loading') && !catalogSurface.data;
  const catalogInvalid = Boolean(catalogSurface.data && !catalogProjection);
  const showFailure = catalogInvalid || Boolean(catalogSurface.error) || catalogSurface.status !== 'ready' && !showLoading;

  return (
    <>
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle className="flex items-center gap-2"><ShieldCheck className="h-4 w-4" aria-hidden="true" />Advanced Profile catalog</CardTitle>
            <Badge variant={catalogProjection && !catalogSurface.stale ? 'success' : 'warning'}>
              {catalogProjection ? `${catalogProjection.count} definitions` : 'locked'}
            </Badge>
          </div>
          <CardDescription>Profiles are read from the Broker-backed Protocol v4 catalog. The selected definition owns its Pack closure and exact digest bindings; this Launcher cannot invent or edit named Profiles.</CardDescription>
        </CardHeader>
        <CardContent>
          {showLoading ? (
            <div className="flex min-h-28 items-center gap-3 rounded-lg border border-border bg-bg-main px-4 py-4 text-sm text-text-muted" role="status" aria-live="polite">
              <RefreshCw className="h-4 w-4 animate-spin" aria-hidden="true" />
              Loading authoritative Profile definitions…
            </div>
          ) : null}

          {showFailure ? (
            <div className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-4" role="alert">
              <div className="flex min-w-0 items-start gap-3 text-sm">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />
                <div>
                  <p className="font-semibold text-text-main">Authoritative Profile catalog is locked</p>
                  <p className="mt-1 text-text-muted">{catalogInvalid ? 'The Broker response failed exact v4 validation.' : catalogSurface.error?.message ?? 'No accepted catalog snapshot is available.'}</p>
                  {catalogSurface.stale ? <p className="mt-1 text-xs text-text-muted">The last accepted definitions remain read-only until the catalog refreshes.</p> : null}
                </div>
              </div>
              <Button type="button" variant="outline" size="sm" onClick={() => void catalogSurface.refresh(true)} disabled={catalogSurface.status === 'loading'}>
                <RefreshCw className="h-4 w-4" aria-hidden="true" />
                Refresh catalog
              </Button>
            </div>
          ) : null}

          {!showLoading && !showFailure && catalogProjection && catalogProjection.profiles.length === 0 ? (
            <div className="flex min-h-28 flex-col items-center justify-center rounded-lg border border-dashed border-border px-5 py-8 text-center" role="status">
              <p className="text-sm font-semibold text-text-main">No Profile definitions are currently published</p>
              <p className="mt-2 max-w-xl text-sm text-text-muted">The authoritative catalog is empty. No client-side Profile candidates or Pack closures are created.</p>
            </div>
          ) : null}

          {!showLoading && catalogProjection && catalogProjection.profiles.length > 0 ? (
            <>
              {catalogSurface.stale ? (
                <p className="mb-3 rounded-lg border border-amber-300/70 bg-amber-50/70 px-4 py-3 text-sm text-amber-800 dark:border-amber-800/60 dark:bg-amber-950/20 dark:text-amber-200" role="alert">The catalog is stale. Definitions and markers remain visible for diagnosis, but selection and ceremony actions are locked.</p>
              ) : null}
              <div className="grid gap-2 sm:grid-cols-2" role="group" aria-label="Select an authoritative Profile definition">
                {catalogProjection.profiles.map((entry) => {
                  const selected = selectedProfileId === entry.profile_id;
                  const unavailableLabel = entry.available ? '' : ' unavailable';
                  return (
                    <button
                      key={entry.profile_id}
                      type="button"
                      className="flex min-h-11 items-center gap-3 rounded-lg border border-border bg-bg-main px-3 py-3 text-left transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] disabled:pointer-events-none disabled:opacity-60"
                      aria-label={`Select Profile ${entry.display_name} (${entry.profile_id})${unavailableLabel}`}
                      aria-pressed={selected}
                      disabled={!entry.available || catalogSurface.stale || ceremonyBusy}
                      onClick={() => {
                        setSelectedProfileId(entry.profile_id);
                        setCeremonyMode('catalog');
                      }}
                    >
                      <span className={selected ? 'flex size-5 shrink-0 items-center justify-center rounded-full border border-accent bg-accent text-accent-fg' : 'size-5 shrink-0 rounded-full border border-border'} aria-hidden="true">
                        {selected ? <CheckCircle2 className="h-4 w-4" /> : null}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex flex-wrap items-center gap-2 text-sm font-medium text-text-main">
                          <span className="truncate">{entry.display_name}</span>
                          {entry.active ? <Badge variant="success">Active</Badge> : null}
                          {!entry.available ? <Badge variant="destructive">Unavailable</Badge> : null}
                        </span>
                        <span className="mt-1 block truncate font-mono text-xs text-text-muted">{entry.profile_id} · {entry.pack_closure.length} closure rows</span>
                      </span>
                    </button>
                  );
                })}
              </div>
            </>
          ) : null}
        </CardContent>
      </Card>

      {selectedEntry ? (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Selected authoritative Profile</CardTitle>
              <CardDescription>Every displayed field below comes from the verified catalog projection and is bound into the resolve request. Pack refreshes can change compatibility, never the definition.</CardDescription>
            </CardHeader>
            <CardContent>
              <ProfileDefinitionDetails entry={selectedEntry} />
              <div className="mt-5 flex flex-wrap items-center gap-2" role="group" aria-label="Choose Profile ceremony mode">
                <Button
                  type="button"
                  size="sm"
                  variant={catalogCeremonyMode ? 'default' : 'outline'}
                  aria-pressed={catalogCeremonyMode}
                  disabled={ceremonyBusy}
                  onClick={() => setCeremonyMode('catalog')}
                >
                  Use selected Profile ceremony
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant={catalogCeremonyMode ? 'outline' : 'default'}
                  aria-pressed={!catalogCeremonyMode}
                  disabled={ceremonyBusy}
                  onClick={() => setCeremonyMode('defaults')}
                >
                  Edit Defaults Pack-set
                </Button>
              </div>
            </CardContent>
          </Card>
        </>
      ) : null}
      <ProfileCeremonyPanel
        surface={profileSurface}
        packs={packs}
        packsLoading={packsLoading}
        loadPacks={loadPacks}
        client={client}
        onActivated={handleActivated}
        onBusyChange={setCeremonyBusy}
        authoritativeSelection={catalogCeremonyMode && catalogProjection ? {
          entry: selectedEntry,
          catalogDigest: catalogProjection.catalog_digest,
          bundleLockDigest: catalogProjection.bundle_lock_digest,
        } : undefined}
        catalogSurface={catalogCeremonyMode ? catalogSurface : undefined}
      />
    </>
  );
}
