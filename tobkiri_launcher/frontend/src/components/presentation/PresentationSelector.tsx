import { CheckCircle2, LockKeyhole, PackageCheck, ShieldAlert, ShieldCheck } from 'lucide-react';

import type {
  ApiPresentationState,
  ApiPresentationSelection,
} from '@/src/lib/apiTypes';
import {
  approvalLabel,
  authorityLabel,
  checkShellCompatibility,
  compatibleShellProviders,
  findBasePack,
  findShellProvider,
  launchDisabledReasonForSelection,
  materializationLabel,
  materializationReason,
  selectShellAfterBaseChange,
} from '@/src/lib/presentation';
import { Badge } from '@/src/components/ui/Badge';
import { Button } from '@/src/components/ui/Button';
import { CopyErrorButton } from '@/src/components/ui/CopyErrorButton';
import { Card } from '@/src/components/ui/Card';

export interface PresentationSelectorProps {
  state: ApiPresentationState;
  selection: ApiPresentationSelection | null;
  saving?: boolean;
  launching?: boolean;
  error?: string | null;
  onSelectionChange: (selection: ApiPresentationSelection | null) => void;
  onSave: (selection: ApiPresentationSelection) => void | Promise<void>;
  onLaunch?: () => void | Promise<void>;
}

function approvalVariant(state: string): 'success' | 'warning' | 'destructive' | 'outline' {
  if (state === 'verified' || state === 'not_required') return 'success';
  if (state === 'blocked') return 'destructive';
  return 'warning';
}

function artifactVariant(status: string): 'success' | 'warning' | 'destructive' | 'outline' {
  if (status === 'verified') return 'success';
  if (status === 'digest_mismatch' || status === 'development_only') return 'destructive';
  if (status === 'missing' || status === 'unsupported_platform') return 'warning';
  return 'outline';
}

function approvalAllowsSelection(state: string | undefined): boolean {
  return state === 'verified' || state === 'not_required';
}

function AuthoritySummary({
  label,
  authority,
}: {
  label: string;
  authority: ApiPresentationState['catalog']['base_packs'][number]['approval'];
}) {
  return (
    <div className="mt-4 rounded-xl border border-border bg-bg-main/60 p-4" data-testid={`${label}-authority`}>
      <div className="flex items-start gap-2">
        <LockKeyhole className="mt-0.5 h-4 w-4 shrink-0 text-text-muted" aria-hidden="true" />
        <div className="min-w-0">
          <p className="text-xs font-semibold text-text-muted">{label} authority</p>
          <p className="mt-1 text-sm font-medium text-text-main">{authorityLabel(authority.authority_mode)}</p>
          <p className="mt-1 text-xs leading-5 text-text-muted">{authority.blast_radius}</p>
        </div>
      </div>
      <dl className="mt-3 grid gap-2 text-xs text-text-muted sm:grid-cols-2">
        <div>
          <dt className="font-medium text-text-main">Provider trust</dt>
          <dd>{approvalLabel(authority.provider_trust)}</dd>
        </div>
        <div>
          <dt className="font-medium text-text-main">Use Grant</dt>
          <dd>{authority.grant_state.replaceAll('_', ' ')}</dd>
        </div>
        <div>
          <dt className="font-medium text-text-main">Execution domain</dt>
          <dd className="break-words">{authority.execution_domain}</dd>
        </div>
        <div>
          <dt className="font-medium text-text-main">Effect scope</dt>
          <dd>{authority.effect_scope.length ? authority.effect_scope.join(', ') : 'None declared'}</dd>
        </div>
      </dl>
    </div>
  );
}

function IdentitySummary({
  state,
  basePack,
}: {
  state: ApiPresentationState;
  basePack: ApiPresentationState['catalog']['base_packs'][number] | null;
}) {
  return (
    <Card className="p-5" data-testid="presentation-profile-identity">
      <div>
        <p className="text-xs font-semibold text-text-muted">
          Verified profile identity
        </p>
        <p className="mt-2 text-sm leading-6 text-text-muted">
          The profile and backend identity stay pinned when the presentation Shell changes.
        </p>
      </div>
      <dl className="mt-4 grid gap-x-6 gap-y-3 text-xs text-text-muted sm:grid-cols-2">
        <div>
          <dt className="font-medium text-text-main">Selected catalog Profile</dt>
          <dd className="mt-1 break-words">{state.catalog.default_profile_id}</dd>
        </div>
        <div>
          <dt className="font-medium text-text-main">Catalog source</dt>
          <dd className="mt-1 break-words">{state.catalog.default_profile_source}</dd>
        </div>
        <div data-testid="default-profile-digest">
          <dt className="font-medium text-text-main">Profile SHA-256</dt>
          <dd className="mt-1 break-all font-mono">{state.catalog.default_profile_digest}</dd>
        </div>
        <div data-testid="backend-identity-digest">
          <dt className="font-medium text-text-main">Backend identity SHA-256</dt>
          <dd className="mt-1 break-all font-mono">{basePack?.backend_identity_digest ?? 'Unavailable'}</dd>
        </div>
        <div>
          <dt className="font-medium text-text-main">Backend Providers</dt>
          <dd className="mt-1 break-words">{basePack?.backend_provider_ids.join(', ') || 'Unavailable'}</dd>
        </div>
        <div>
          <dt className="font-medium text-text-main">State owners</dt>
          <dd className="mt-1 break-words">{basePack?.state_owners.join(', ') || 'Unavailable'}</dd>
        </div>
      </dl>
    </Card>
  );
}

export function PresentationSelector({
  state,
  selection,
  saving = false,
  launching = false,
  error = null,
  onSelectionChange,
  onSave,
  onLaunch,
}: PresentationSelectorProps) {
  const selectedBase = selection ? findBasePack(state.catalog, selection.base_pack_id) : null;
  const selectedShell = selection ? findShellProvider(state.catalog, selection.shell_provider_id) : null;
  const compatibleShells = selectedBase
    ? compatibleShellProviders(state.catalog, selectedBase.pack_id)
    : [];
  const compatibility = checkShellCompatibility(selectedBase, selectedShell);
  const saveBlockedReason = !selection
    ? 'Choose a Base Pack and a compatible Shell Provider.'
    : !compatibility.compatible
      ? compatibility.reasons[0] ?? 'The selected Base Pack and Shell Provider are not compatible.'
      : !approvalAllowsSelection(selectedBase?.approval.state)
        ? 'The selected Base Pack is not approved.'
        : !approvalAllowsSelection(selectedShell?.approval.state)
          ? 'The selected Shell Provider is not approved.'
          : selectedShell?.artifact?.status !== 'verified'
            ? 'The selected Shell production artifact is not verified.'
            : null;
  const canSave = saveBlockedReason === null && !saving && !launching;
  const launchReason = launchDisabledReasonForSelection(
    state.materialization,
    state.selection,
    selection,
  );

  const handleBaseChange = (basePackId: string) => {
    onSelectionChange(
      selectShellAfterBaseChange(
        state.catalog,
        basePackId,
        selection?.shell_provider_id ?? '',
      ),
    );
  };

  return (
    <div className="space-y-5" data-testid="presentation-selector">
      <div>
        <p className="text-xs font-medium text-text-muted">Presentation</p>
        <h2 className="mt-2 text-xl font-semibold tracking-[-.02em] text-text-main">
          Choose a Base Pack, then its Shell
        </h2>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-text-muted">
          Base Pack selection defines the capability graph. Shell selection only chooses the
          compatible <code className="rounded bg-bg-hover px-1 py-0.5 text-xs">app.shell.v1</code> presentation;
          it never mints Host authority or changes backend ownership.
        </p>
      </div>

      <IdentitySummary state={state} basePack={selectedBase} />

      {error ? (
        <div role="alert" className="flex items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/5 p-4 text-sm text-text-main">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />
          <span className="min-w-0 flex-1 break-words">{error}</span>
          <CopyErrorButton label="Copy presentation error" text={error} />
        </div>
      ) : null}

      <fieldset disabled={saving || launching}>
        <legend className="mb-2 text-sm font-semibold text-text-main">1. Base Pack</legend>
        <div className="grid gap-3">
          {state.catalog.base_packs.map((basePack) => {
            const selected = selection?.base_pack_id === basePack.pack_id;
            return (
              <button
                key={basePack.pack_id}
                type="button"
                aria-pressed={selected}
                onClick={() => handleBaseChange(basePack.pack_id)}
                className={`rounded-xl border p-4 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] ${selected ? 'border-accent bg-accent/5' : 'border-border bg-bg-card hover:bg-bg-hover'}`}
                data-testid={`base-pack-${basePack.pack_id}`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="font-semibold text-text-main">{basePack.display_name}</p>
                    <p className="mt-1 text-xs text-text-muted">{basePack.pack_id} · v{basePack.version}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge variant={approvalVariant(basePack.approval.state)}>
                      {approvalLabel(basePack.approval.state)}
                    </Badge>
                    {selected ? <CheckCircle2 className="h-4 w-4 text-accent" aria-hidden="true" /> : null}
                  </div>
                </div>
                <p className="mt-3 text-xs leading-5 text-text-muted">
                  Requires: {basePack.required_capabilities.join(', ') || 'no presentation capabilities'}
                </p>
                <AuthoritySummary label="Base Pack" authority={basePack.approval} />
              </button>
            );
          })}
        </div>
      </fieldset>

      <fieldset disabled={!selectedBase || saving || launching}>
        <legend className="mb-2 text-sm font-semibold text-text-main">2. Compatible Shell Provider</legend>
        <div className="grid gap-3">
          {compatibleShells.map((shell) => {
            const selected = selection?.shell_provider_id === shell.provider_id;
            const artifact = shell.artifact;
            return (
              <button
                key={shell.provider_id}
                type="button"
                aria-pressed={selected}
                onClick={() => onSelectionChange({
                  base_pack_id: selectedBase?.pack_id ?? '',
                  shell_provider_id: shell.provider_id,
                })}
                className={`rounded-xl border p-4 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] ${selected ? 'border-accent bg-accent/5' : 'border-border bg-bg-card hover:bg-bg-hover'}`}
                data-testid={`shell-provider-${shell.provider_id}`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="font-semibold text-text-main">{shell.display_name}</p>
                    <p className="mt-1 text-xs text-text-muted">
                      {shell.provider_id} · {shell.technology} · {shell.presentation_family}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge variant={approvalVariant(shell.approval.state)}>
                      {approvalLabel(shell.approval.state)}
                    </Badge>
                    {selected ? <CheckCircle2 className="h-4 w-4 text-accent" aria-hidden="true" /> : null}
                  </div>
                </div>
                <p className="mt-3 text-xs leading-5 text-text-muted">
                  Contract: {shell.contract_id} · {shell.presentation_kind}
                </p>
                <p className="mt-1 text-xs leading-5 text-text-muted">
                  Capabilities: {shell.capabilities.join(', ') || 'none declared'}
                </p>
                <AuthoritySummary label="Shell Provider" authority={shell.approval} />
                <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
                  <PackageCheck className="h-3.5 w-3.5 text-text-muted" aria-hidden="true" />
                  <span className="text-text-muted">Production artifact</span>
                  <Badge variant={artifactVariant(artifact?.status ?? 'missing')}>
                    {artifact?.status.replaceAll('_', ' ') ?? 'missing'}
                  </Badge>
                  {artifact?.status_detail ? <span className="text-text-muted">{artifact.status_detail}</span> : null}
                </div>
              </button>
            );
          })}
          {selectedBase && compatibleShells.length === 0 ? (
            <div role="alert" className="flex items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/5 p-4 text-sm text-text-muted">
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />
              <span className="min-w-0 flex-1">No Shell Provider satisfies this Base Pack's required capabilities. The Launcher will not fall back to another presentation family.</span>
              <CopyErrorButton label="Copy Shell Provider error" text="No Shell Provider satisfies this Base Pack's required capabilities. The Launcher will not fall back to another presentation family." />
            </div>
          ) : null}
        </div>
      </fieldset>

      <Card className="p-5" data-testid="presentation-materialization">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-xs font-semibold text-text-muted">Selected presentation</p>
            <p className="mt-2 text-sm font-semibold text-text-main">
              {materializationLabel(state.materialization)}
            </p>
            <p className="mt-1 max-w-2xl text-xs leading-5 text-text-muted">
              {materializationReason(state.materialization)}
            </p>
          </div>
          <Badge variant={state.materialization.status === 'materialized' ? 'success' : 'warning'}>
            {state.materialization.status.replaceAll('_', ' ')}
          </Badge>
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          {state.materialization.selected_contributions.length > 0 ? (
            state.materialization.selected_contributions.map((contribution) => (
              <Badge key={contribution.contribution_id} variant="outline">
                {contribution.label}
              </Badge>
            ))
          ) : (
            <span className="text-xs text-text-muted">No contribution artifacts are materialized.</span>
          )}
        </div>
        <div className="mt-5 flex flex-wrap gap-3">
          <Button
            type="button"
            onClick={() => {
              if (selection) void onSave(selection);
            }}
            disabled={!canSave}
            loading={saving}
            title={saveBlockedReason ?? 'Save the verified presentation selection'}
            data-testid="save-presentation"
          >
            Save presentation selection
          </Button>
          {onLaunch ? (
            <Button
              type="button"
              variant="outline"
              onClick={() => void onLaunch()}
              disabled={Boolean(launchReason) || saving || launching}
              loading={launching}
              title={launchReason ?? 'Launch the verified production Shell artifact'}
              data-testid="launch-presentation"
            >
              Launch selected Shell
            </Button>
          ) : null}
        </div>
        {saveBlockedReason ? (
          <p className="mt-3 flex items-start gap-2 text-xs leading-5 text-text-muted" data-testid="save-blocked-reason">
            <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-text-muted" aria-hidden="true" />
            <span>Save blocked: {saveBlockedReason}</span>
          </p>
        ) : null}
        {launchReason ? (
          <p className="mt-3 flex items-start gap-2 text-xs leading-5 text-text-muted" data-testid="launch-blocked-reason">
            <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-text-muted" aria-hidden="true" />
            <span>Launch blocked: {launchReason}</span>
          </p>
        ) : null}
      </Card>
    </div>
  );
}
