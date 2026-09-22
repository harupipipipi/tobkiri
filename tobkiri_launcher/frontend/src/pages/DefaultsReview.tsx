import {Button} from '@/src/components/ui/Button';
import type {DefaultsSetupState} from '@/src/lib/defaultsSetup';

type Props = {
  readonly setup: DefaultsSetupState | null;
  readonly reviewed: boolean;
  readonly activating: boolean;
  readonly activationCommitted?: boolean;
  readonly error: string | null;
  readonly reconfirmationRequired?: boolean;
  readonly onRecover?: () => void;
  readonly onReviewedChange: (reviewed: boolean) => void;
  readonly onActivate: () => void;
};

export function DefaultsReview({
  setup,
  reviewed,
  activating,
  activationCommitted = false,
  error,
  reconfirmationRequired = false,
  onRecover = () => undefined,
  onReviewedChange,
  onActivate,
}: Props) {
  const canActivate = setup?.state === 'review_required' && !activationCommitted;
  return <section className="rounded-[18px] border border-border bg-bg-card p-7 shadow-lg" aria-labelledby="defaults-review-title">
    <p className="text-xs font-medium uppercase tracking-wider text-text-muted">Defaults v4 bootstrap</p>
    <h1 id="defaults-review-title" className="mt-3 text-2xl font-semibold text-text-main">
      {reconfirmationRequired ? 'Profile reconfirmation required' : 'Activate Defaults Profile'}
    </h1>
    <p className="mt-2 text-sm leading-6 text-text-muted">
      {reconfirmationRequired
        ? 'The Host has withheld the verified dispatch map. Review the exact Defaults v4 transaction below to restore local operations.'
        : 'Review the finite local composition. Activation occurs only after this exact confirmation.'}
    </p>
    {activationCommitted && setup?.state !== 'active' && <div role="alert" className="mt-6 rounded-lg border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-text-main">
      <p className="font-medium">Activation was submitted; verification is required.</p>
      <p className="mt-2 text-text-muted">Tobkiri will re-read the Host-owned Setup state. The previous confirmation will not be submitted again.</p>
      <div className="mt-4"><Button variant="outline" onClick={onRecover} loading={activating}>Verify activation</Button></div>
    </div>}
    {!setup && !error && <p role="status" className="mt-6 text-sm text-text-muted">Loading verified catalog…</p>}
    {setup?.state === 'activation_denied' && <p role="alert" className="mt-4 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-500">
      {setup.denial_diagnostic}
    </p>}
    {setup && <div className="mt-6 space-y-3 text-sm">
      <Identity label="Base" value={setup.recommended_default_profile.base_pack} />
      <Identity label="Shell" value={setup.recommended_default_profile.shell.provider_id} />
      <Identity label="Conversation provider" value={setup.recommended_default_profile.conversation_provider} />
      <div className="rounded-lg border border-border bg-bg-main p-4">
        <p className="text-xs font-medium text-text-muted">Selected Packs</p>
        <ul className="mt-2 space-y-1 text-text-main">{setup.recommended_default_profile.packs.map((pack) => <li key={pack.pack_id}>{pack.display_name} <span className="text-xs text-text-muted">({pack.pack_id})</span></li>)}</ul>
      </div>
      <div className="rounded-lg border border-border bg-bg-main p-4">
        <p className="text-xs font-medium text-text-muted">Host activation ceremony</p>
        <ol aria-label="Defaults v4 activation ceremony" className="mt-2 list-decimal space-y-1 pl-5 text-text-main">
          {setup.required_transaction.map((step) => <li key={step}>{step}</li>)}
        </ol>
        <p className="mt-3 text-xs leading-5 text-text-muted">
          Resolve, review, approve, activate, and capture are performed by the Host-owned transaction. This screen only submits the exact confirmation it issued.
        </p>
      </div>
      <label className="flex items-start gap-3 rounded-lg border border-border p-4 text-text-main">
        <input
          type="checkbox"
          checked={reviewed}
          disabled={!canActivate || activating}
          onChange={(event) => onReviewedChange(event.target.checked)}
          className="mt-1 size-4 shrink-0"
          aria-describedby="defaults-review-confirmation"
        />
        <span id="defaults-review-confirmation">I confirm this exact catalog/profile revision, provider operations, Authority snapshot, and SecurityEpoch.</span>
      </label>
      <Button size="lg" className="w-full" disabled={!reviewed || activating || !canActivate} loading={activating} onClick={onActivate}>Activate Defaults Profile</Button>
    </div>}
    {error && <p role="alert" className="mt-4 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-500">{error}</p>}
  </section>;
}

function Identity({label, value}: {readonly label: string; readonly value: string}) {
  return <div className="flex items-center justify-between gap-4 rounded-lg border border-border bg-bg-main p-4">
    <span className="text-text-muted">{label}</span><code className="break-all text-right text-xs text-text-main">{value}</code>
  </div>;
}
