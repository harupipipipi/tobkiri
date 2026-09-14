import {useCallback, useEffect, useMemo, useRef, useState, type ReactNode} from 'react';
import {Link} from 'react-router';
import {AlertTriangle, CheckCircle2, Database, FileKey2, MessageSquare, PackageCheck, Plus, RefreshCw, Search, ShieldCheck} from 'lucide-react';

import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {Card, CardContent, CardDescription, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {Input} from '@/src/components/ui/Input';
import {ProfileCeremonyPanel} from '@/src/components/advanced/ProfileCeremonyPanel';
import type {RuntimeSurfaceState} from '@/src/hooks/useRuntimeSurface';
import type {ApiDynamicFrontendCatalog} from '@/src/lib/apiTypes';
import {
  extractExactProfileCatalog,
  type RuntimeProfileCatalogEntry,
  type RuntimeProfileCatalogProjection,
} from '@/src/lib/runtimeSurface';
import {
  resolveConversationCapabilityForProfile,
  verifiedCapabilityLabel,
} from '@/src/lib/presentation';
import {useT} from '@/src/lib/i18n';
import {panelRoutes} from '@/src/lib/routes';
import type {ProfileActivateResult, ProfileCeremonyClient} from '@/src/lib/profileCeremony';
import {useAppStore, type Pack} from '@/src/store';

export function profileMatchesQuery(entry: RuntimeProfileCatalogEntry, query: string): boolean {
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized) return true;
  return [
    entry.display_name,
    entry.profile_id,
    entry.bindings.base.pack_id,
    entry.bindings.shell.provider_id,
    entry.bindings.shell.pack_id,
    entry.bindings.application?.pack_id,
    ...entry.pack_closure.flatMap((pack) => [pack.pack_id, pack.role, pack.version]),
  ].some((value) => value?.toLocaleLowerCase().includes(normalized));
}

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

function OptionalConversationCapability({
  entry,
  catalog,
  loading,
  error,
}: {
  entry: RuntimeProfileCatalogEntry;
  catalog: ApiDynamicFrontendCatalog | null;
  loading: boolean;
  error: string | null;
}) {
  const capability = entry.active
    ? resolveConversationCapabilityForProfile(catalog, entry.profile_id)
    : null;
  const catalogBelongsToProfile = catalog?.profile_id === entry.profile_id;
  const capabilityErrorDiagnostic = error || (catalog && !catalogBelongsToProfile)
    ? 'No accepted capability snapshot is bound to this active Profile.'
    : null;

  return (
    <section
      aria-labelledby={`profile-${entry.profile_id}-capability-title`}
      className="rounded-lg border border-border bg-bg-main p-4"
      data-testid="profile-conversation-capability"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h4 id={`profile-${entry.profile_id}-capability-title`} className="flex items-center gap-2 text-sm font-semibold text-text-main">
            <MessageSquare className="h-4 w-4 text-text-muted" aria-hidden="true" />
            Optional verified capabilities
          </h4>
          <p className="mt-2 text-sm leading-6 text-text-muted">
            Conversation is shown only when this Profile publishes a verified view contribution. It is not required for Profile activation or Shell launch.
          </p>
        </div>
        <Badge variant={capability ? 'success' : 'outline'}>
          {capability ? 'Verified' : 'Optional'}
        </Badge>
      </div>
      {!entry.active ? (
        <p className="mt-4 rounded-md border border-dashed border-border px-3 py-3 text-sm text-text-muted" role="status">
          This Profile is browse-only. Its optional capabilities are resolved from its own accepted catalog after activation; browsing does not borrow the active Profile capability snapshot.
        </p>
      ) : loading ? (
        <p className="mt-4 rounded-md border border-dashed border-border px-3 py-3 text-sm text-text-muted" role="status" aria-live="polite">
          Loading the active Profile capability catalog…
        </p>
      ) : capability ? (
        <dl className="mt-4 grid gap-3 rounded-md border border-border px-3 py-3 text-xs sm:grid-cols-2" role="status" aria-live="polite">
          <div>
            <dt className="font-medium text-text-main">Verified view</dt>
            <dd className="mt-1 break-words text-text-muted">{verifiedCapabilityLabel(capability)}</dd>
          </div>
          <div>
            <dt className="font-medium text-text-main">Capability route</dt>
            <dd className="mt-1 break-all font-mono text-text-muted">{capability.route}</dd>
          </div>
          <div>
            <dt className="font-medium text-text-main">Owner Pack</dt>
            <dd className="mt-1 break-all font-mono text-text-muted">{capability.owner_pack_id}</dd>
          </div>
          <div>
            <dt className="font-medium text-text-main">Profile binding</dt>
            <dd className="mt-1 break-all font-mono text-text-muted">{entry.profile_id}</dd>
          </div>
        </dl>
      ) : (
        <div className="mt-4 flex items-start gap-2 rounded-md border border-dashed border-border px-3 py-3 text-sm text-text-muted" role={capabilityErrorDiagnostic ? 'alert' : 'status'}>
          {capabilityErrorDiagnostic ? <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-destructive" /> : null}
          <p className="min-w-0 flex-1">{capabilityErrorDiagnostic ?? 'No verified conversation capability is published for this Profile.'}</p>
          {capabilityErrorDiagnostic ? <CopyErrorButton label="Copy Profile capability error" text={capabilityErrorDiagnostic} /> : null}
        </div>
      )}
    </section>
  );
}

function profileCatalogFailureDiagnostic(
  catalogInvalid: boolean,
  error: RuntimeSurfaceState<RuntimeProfileCatalogProjection>['error'],
  stale: boolean,
  t: ReturnType<typeof useT>,
): string {
  const detail = catalogInvalid
    ? t('profile_catalog.validation_failed')
    : error?.message ?? t('profile_catalog.snapshot_unavailable');
  return [
    t('profile_catalog.locked_title'),
    detail,
    ...(stale
      ? [t('profile_catalog.stale_read_only')]
      : []),
  ].join('\n');
}

function ProfileDefinitionDetails({
  entry,
  catalog,
  frontendCatalogLoading,
  frontendCatalogError,
}: {
  entry: RuntimeProfileCatalogEntry;
  catalog: ApiDynamicFrontendCatalog | null;
  frontendCatalogLoading: boolean;
  frontendCatalogError: string | null;
}) {
  const t = useT();
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
              <Badge variant="success"><CheckCircle2 className="mr-1 h-3 w-3" aria-hidden="true" />{t('profile_catalog.active')}</Badge>
            ) : <Badge variant="outline">{t('profile_catalog.available_candidate')}</Badge>}
            <Badge variant={entry.available ? 'success' : 'destructive'}>{entry.available ? t('profile_catalog.verified') : t('profile_catalog.unavailable')}</Badge>
          </div>
        </div>
        {entry.diagnostics.length > 0 ? (
          <div className="mt-4 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm" role="alert">
            <p className="flex items-center gap-2 font-medium text-text-main"><AlertTriangle className="h-4 w-4 text-destructive" aria-hidden="true" />{t('profile_catalog.profile_unavailable')}</p>
            <div className="mt-2 flex items-start gap-2"><ul className="min-w-0 flex-1 list-disc space-y-1 pl-5 text-text-muted">
              {entry.diagnostics.map((diagnostic) => <li key={`${diagnostic.code}:${diagnostic.subject}`}>{diagnostic.code}: {diagnostic.subject}</li>)}
            </ul><CopyErrorButton label="Copy Profile catalog diagnostics" text={entry.diagnostics.map((diagnostic) => `${diagnostic.code}: ${diagnostic.subject}`).join('\n')} /></div>
          </div>
        ) : null}
      </section>

      <details className="rounded-lg border border-border bg-bg-main px-4 py-4" data-testid="profile-technical-details">
        <summary className="cursor-pointer text-sm font-medium text-text-main focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]">
          {t('profile_catalog.technical_details')}
        </summary>
        <div className="mt-4 flex flex-col gap-4">
          <dl className="grid gap-3 rounded-lg border border-border bg-bg-card p-4 sm:grid-cols-2">
            <BindingField label="Profile definition digest" value={entry.definition.digest} />
            <BindingField label="Definition catalog revision" value={entry.definition.catalog_revision} />
            <BindingField label="Definition reference" value={entry.definition.ref} />
            <BindingField label="Source path" value={entry.definition.source_path} />
          </dl>
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
          <section className="scroll-mt-6 rounded-lg border border-border bg-bg-card p-4" id="profile-closure">
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
          <OptionalConversationCapability
            entry={entry}
            catalog={catalog}
            loading={frontendCatalogLoading}
            error={frontendCatalogError}
          />
          <dl className="grid gap-3 rounded-lg border border-border bg-bg-card p-4 sm:grid-cols-3">
            <BindingField label="Profile revision" value={entry.records.profile_revision} />
            <BindingField label="Profile lock digest" value={entry.records.profile_lock_digest} />
            <BindingField label="Resolved Plan digest" value={entry.records.plan_digest} />
            <BindingField label="Authority snapshot" value={entry.authority_snapshot.digest} />
            <BindingField label="Authority snapshot reference" value={entry.authority_snapshot.ref} />
            <BindingField label="Candidate state" value={entry.candidate.state} />
          </dl>
        </div>
      </details>
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
  initialSelectedProfileId,
  onSelectedProfileId,
  runtimeVerified = true,
}: {
  profileSurface: RuntimeSurfaceState<unknown>;
  catalogSurface: RuntimeSurfaceState<RuntimeProfileCatalogProjection>;
  packs: Pack[];
  packsLoading: boolean;
  loadPacks: () => Promise<void>;
  client?: ProfileCeremonyClient;
  onActivated?: (result: ProfileActivateResult) => Promise<void>;
  initialSelectedProfileId?: string | null;
  onSelectedProfileId?: (profileId: string) => void;
  /** Runtime/effect ceremony access, independent from catalog browsing. */
  runtimeVerified?: boolean;
}) {
  const t = useT();
  const frontendCatalog = useAppStore((state) => state.frontendCatalog);
  const frontendCatalogLoading = useAppStore((state) => state.frontendCatalogLoading);
  const frontendCatalogError = useAppStore((state) => state.frontendCatalogError);
  const [selectedProfileId, setSelectedProfileId] = useState<string | null>(null);
  const [ceremonyBusy, setCeremonyBusy] = useState(false);
  const [query, setQuery] = useState('');
  const previousPackFingerprint = useRef<string | null>(null);
  const previousInitialSelectedProfileId = useRef<string | null | undefined>(undefined);

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
    const initialSelectionChanged = (
      previousInitialSelectedProfileId.current !== initialSelectedProfileId
    );
    previousInitialSelectedProfileId.current = initialSelectedProfileId;
    setSelectedProfileId((current) => (
      initialSelectionChanged
      && initialSelectedProfileId
      && catalogProjection.profiles.some((entry) => entry.profile_id === initialSelectedProfileId)
        ? initialSelectedProfileId
        : current && catalogProjection.profiles.some((entry) => entry.profile_id === current)
          ? current
          : catalogProjection.active_profile_id
    ));
  }, [catalogProjection, initialSelectedProfileId]);

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
  const filteredProfiles = useMemo(
    () => catalogProjection?.profiles.filter((entry) => profileMatchesQuery(entry, query)) ?? [],
    [catalogProjection, query],
  );
  const handleActivated = useCallback(async (result: ProfileActivateResult) => {
    setSelectedProfileId(result.profile_id);
    onSelectedProfileId?.(result.profile_id);
    await onActivated?.(result);
  }, [onActivated, onSelectedProfileId]);

  const showLoading = (catalogSurface.status === 'idle' || catalogSurface.status === 'loading') && !catalogSurface.data;
  const catalogInvalid = Boolean(catalogSurface.data && !catalogProjection);
  const showFailure = catalogInvalid || Boolean(catalogSurface.error) || catalogSurface.status !== 'ready' && !showLoading;
  const catalogFailure = profileCatalogFailureDiagnostic(
    catalogInvalid,
    catalogSurface.error,
    catalogSurface.stale,
    t,
  );

  return (
    <>
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle className="flex items-center gap-2"><ShieldCheck className="h-4 w-4" aria-hidden="true" />{t('profile_catalog.title')}</CardTitle>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={catalogProjection && !catalogSurface.stale ? 'success' : 'warning'}>
                {catalogProjection
                  ? t('profile_catalog.count', {count: String(catalogProjection.count)})
                  : t('profile_catalog.locked')}
              </Badge>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => void catalogSurface.refresh(true)}
                disabled={catalogSurface.status === 'loading'}
                aria-label={t('profile_catalog.refresh_profiles')}
              >
                <RefreshCw className={catalogSurface.status === 'loading' ? 'h-4 w-4 animate-spin' : 'h-4 w-4'} aria-hidden="true" />
                {t('profile_catalog.refresh_profiles')}
              </Button>
              <Link
                className="inline-flex min-h-9 items-center justify-center gap-2 rounded-md bg-accent px-3 text-sm font-medium text-accent-fg transition-colors hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                to={panelRoutes.home}
              >
                <Plus className="h-4 w-4" aria-hidden="true" />
                {t('profile_catalog.add_profile')}
              </Link>
            </div>
          </div>
          <CardDescription>{t('profile_catalog.description')}</CardDescription>
        </CardHeader>
        <CardContent>
          {showLoading ? (
            <div className="flex min-h-28 items-center gap-3 rounded-lg border border-border bg-bg-main px-4 py-4 text-sm text-text-muted" role="status" aria-live="polite">
              <RefreshCw className="h-4 w-4 animate-spin" aria-hidden="true" />
              {t('profile_catalog.loading')}
            </div>
          ) : null}

          {showFailure ? (
            <div className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-4" role="alert">
              <div className="flex min-w-0 items-start gap-3 text-sm">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />
                <div className="min-w-0 flex-1">
                  <p className="font-semibold text-text-main">{t('profile_catalog.locked_title')}</p>
                  <p className="mt-1 text-text-muted">{catalogInvalid ? t('profile_catalog.validation_failed') : catalogSurface.error?.message ?? t('profile_catalog.snapshot_unavailable')}</p>
                  {catalogSurface.stale ? <p className="mt-1 text-xs text-text-muted">{t('profile_catalog.stale_read_only')}</p> : null}
                </div>
              </div>
              <CopyErrorButton label="Copy Profile catalog error" text={catalogFailure} />
              <Button type="button" variant="outline" size="sm" onClick={() => void catalogSurface.refresh(true)} disabled={catalogSurface.status === 'loading'}>
                <RefreshCw className="h-4 w-4" aria-hidden="true" />
                {t('profile_catalog.refresh_catalog')}
              </Button>
            </div>
          ) : null}

          {!showLoading && !showFailure && catalogProjection && catalogProjection.profiles.length === 0 ? (
            <div className="flex min-h-28 flex-col items-center justify-center rounded-lg border border-dashed border-border px-5 py-8 text-center" role="status">
              <p className="text-sm font-semibold text-text-main">{t('profile_catalog.empty_title')}</p>
              <p className="mt-2 max-w-xl text-sm text-text-muted">{t('profile_catalog.empty_description')}</p>
            </div>
          ) : null}

          {!showLoading && catalogProjection && catalogProjection.profiles.length > 0 ? (
            <>
              {catalogSurface.stale ? (
                <p className="mb-3 rounded-lg border border-amber-300/70 bg-amber-50/70 px-4 py-3 text-sm text-amber-800 dark:border-amber-800/60 dark:bg-amber-950/20 dark:text-amber-200" role="alert">{t('profile_catalog.stale_warning')}</p>
              ) : null}
              <div className="mb-3 grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
                <Input
                  label={t('profile_catalog.find')}
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={t('profile_catalog.find_placeholder')}
                />
                <span className="pb-2 text-xs text-text-muted">{t('profile_catalog.result_count', {
                  filtered: String(filteredProfiles.length),
                  total: String(catalogProjection.profiles.length),
                })}</span>
              </div>
              <div className="grid gap-2 sm:grid-cols-2" role="group" aria-label={t('profile_catalog.select_group')}>
                {filteredProfiles.map((entry) => {
                  const selected = selectedProfileId === entry.profile_id;
                  const unavailableLabel = entry.available ? '' : t('profile_catalog.unavailable_suffix');
                  return (
                    <button
                      key={entry.profile_id}
                      type="button"
                      className="flex min-h-11 items-center gap-3 rounded-lg border border-border bg-bg-main px-3 py-3 text-left transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] disabled:pointer-events-none disabled:opacity-60"
                      aria-label={t('profile_catalog.select_profile', {
                        name: entry.display_name,
                        id: entry.profile_id,
                        availability: unavailableLabel,
                      })}
                      aria-pressed={selected}
                      disabled={!entry.available || catalogSurface.stale || ceremonyBusy}
                      onClick={() => {
                        setSelectedProfileId(entry.profile_id);
                        onSelectedProfileId?.(entry.profile_id);
                      }}
                    >
                      <span className={selected ? 'flex size-5 shrink-0 items-center justify-center rounded-full border border-accent bg-accent text-accent-fg' : 'size-5 shrink-0 rounded-full border border-border'} aria-hidden="true">
                        {selected ? <CheckCircle2 className="h-4 w-4" /> : null}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex flex-wrap items-center gap-2 text-sm font-medium text-text-main">
                          <span className="truncate">{entry.display_name}</span>
                          {entry.active ? <Badge variant="success">{t('profile_catalog.active')}</Badge> : null}
                          {!entry.available ? <Badge variant="destructive">{t('profile_catalog.unavailable')}</Badge> : null}
                        </span>
                        <span className="mt-1 block truncate font-mono text-xs text-text-muted">{entry.profile_id} · {t('profile_catalog.closure_rows', {count: String(entry.pack_closure.length)})}</span>
                      </span>
                    </button>
                  );
                })}
              </div>
              {filteredProfiles.length === 0 ? (
                <div className="mt-3 flex min-h-24 flex-col items-center justify-center rounded-lg border border-dashed border-border px-4 text-center">
                  <Search className="mb-2 h-5 w-5 text-text-muted" aria-hidden="true" />
                  <p className="text-sm font-medium text-text-main">{t('profile_catalog.no_match', {query: query.trim()})}</p>
                  <Button type="button" className="mt-2" size="sm" variant="ghost" onClick={() => setQuery('')}>{t('profile_catalog.clear_search')}</Button>
                </div>
              ) : null}
            </>
          ) : null}
        </CardContent>
      </Card>

      {selectedEntry ? (
        <>
          <Card>
            <CardHeader>
              <CardTitle>{t('profile_catalog.configure', {name: selectedEntry.display_name})}</CardTitle>
              <CardDescription>{t('profile_catalog.configure_description')}</CardDescription>
            </CardHeader>
            <CardContent>
              <ProfileDefinitionDetails
                entry={selectedEntry}
                catalog={frontendCatalog}
                frontendCatalogLoading={frontendCatalogLoading}
                frontendCatalogError={frontendCatalogError}
              />
              <p className="mt-5 text-sm text-text-muted">
                {t('profile_catalog.selected_instruction')}
              </p>
            </CardContent>
          </Card>
        </>
      ) : null}
      {selectedEntry && catalogProjection && runtimeVerified ? (
          <ProfileCeremonyPanel
            surface={profileSurface}
            packs={packs}
            loadPacks={loadPacks}
          client={client}
          onActivated={handleActivated}
          onBusyChange={setCeremonyBusy}
          authoritativeSelection={{
            entry: selectedEntry,
            catalogDigest: catalogProjection.catalog_digest,
            bundleLockDigest: catalogProjection.bundle_lock_digest,
          }}
          catalogSurface={catalogSurface}
        />
      ) : selectedEntry && catalogProjection ? (
        <Card>
          <CardHeader>
            <CardTitle>{t('profile_catalog.activation_unavailable')}</CardTitle>
            <CardDescription>{t('profile_catalog.activation_unavailable_description')}</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap items-center gap-3 rounded-lg border border-amber-300/70 bg-amber-50/70 px-4 py-4 text-sm text-amber-800 dark:border-amber-800/60 dark:bg-amber-950/20 dark:text-amber-200" data-testid="profile-ceremony-gate" role="alert">
              <span className="min-w-0 flex-1">{t('profile_catalog.complete_setup')}</span>
              <Link
                className="inline-flex min-h-11 items-center justify-center rounded-lg border border-border bg-bg-main px-4 py-2 text-sm font-medium text-text-main transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2"
                to={panelRoutes.setup}
              >
                {t('profile_catalog.open_setup')}
              </Link>
            </div>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent>
            <p className="py-4 text-sm text-text-muted">
              {t('profile_catalog.select_before_ceremony')}
            </p>
          </CardContent>
        </Card>
      )}
    </>
  );
}
