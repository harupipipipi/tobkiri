import {AlertCircle, Clock3, ShieldAlert} from 'lucide-react';

import {Button} from '@/src/components/ui/Button';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import type {DefaultsSetupState} from '@/src/lib/defaultsSetupContract';
import {DEFAULTS_REQUIRED_TRANSACTION} from '@/src/lib/generatedDefaultsSetupContract';
import {DefaultsConfirmationDetails} from './DefaultsConfirmationDetails';

const activationStepLabels: Record<(typeof DEFAULTS_REQUIRED_TRANSACTION)[number], string> = {
  'catalog.verify': 'Verify the bundled catalog',
  'profile.resolve': 'Check the selected Packs and operations',
  'authority.snapshot': 'Check the permissions for this activation',
  'activation.prepare': 'Prepare the Profile for activation',
  'activation.commit': 'Save the activation',
  'runtime.capture': 'Load the activated Profile',
};

type Props = {
  readonly setup: DefaultsSetupState | null;
  readonly reviewed: boolean;
  readonly activating: boolean;
  readonly activationCommitted?: boolean;
  readonly error: string | null;
  readonly reconfirmationRequired?: boolean;
  readonly activationAllowed?: boolean;
  readonly activationBlockedMessage?: string;
  readonly readOnly?: boolean;
  readonly nativeConfirmation?: boolean;
  readonly nativeSlotGeneration?: number;
  readonly onRecover?: () => void;
  readonly onReviewedChange?: (reviewed: boolean) => void;
  readonly onActivate?: () => void;
};

export function DefaultsReview({
  setup,
  reviewed,
  activating,
  activationCommitted = false,
  error,
  reconfirmationRequired = false,
  activationAllowed = true,
  activationBlockedMessage = 'Select “Include new bundled Profile Packs and operation bindings” above to review a complete successor before activation.',
  readOnly = false,
  nativeConfirmation = false,
  nativeSlotGeneration,
  onRecover = () => undefined,
  onReviewedChange,
  onActivate,
}: Props) {
  const canActivate = setup?.state === 'review_required'
    && !activationCommitted
    && activationAllowed && !readOnly
    && onReviewedChange !== undefined && (onActivate !== undefined || nativeConfirmation);
  const predecessor = setup?.recommended_default_profile.confirmation.definition_predecessor;
  const packPinPredecessor = setup?.recommended_default_profile.confirmation.pack_pin_predecessor;
  const targetPacks = setup?.recommended_default_profile.pack_ids ?? [];
  const addedPacks = targetPacks.filter((id) => !predecessor?.pack_ids.includes(id));
  const removedPacks = predecessor?.pack_ids.filter((id) => !targetPacks.includes(id)) ?? [];
  return <section className="rounded-[18px] border border-border bg-bg-card p-7 shadow-lg"
    aria-labelledby="defaults-review-title">
    {nativeSlotGeneration !== undefined && setup && <span hidden key={nativeSlotGeneration}
      data-native-setup-review-anchor={setup.recommended_default_profile.confirmation.confirmation_digest} />}
    <p className="text-xs font-medium text-text-muted">Tobkiri Harness setup</p>
    <h1 id="defaults-review-title" className="mt-3 text-2xl font-semibold text-text-main">
      {reconfirmationRequired ? 'Profile reconfirmation required' : 'Activate Tobkiri Harness'}
    </h1>
    <p className="mt-2 text-sm leading-6 text-text-muted">
      {reconfirmationRequired
        ? 'Local operations are paused until you review and confirm this Profile again.'
        : 'Review the selected Packs and activation details below. Activation starts after you confirm them.'}
    </p>
    {!readOnly && activationCommitted && setup?.state !== 'active' && <div role="alert" className="mt-6 flex items-start gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-text-main">
      <Clock3 aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-amber-700 dark:text-amber-300" data-error-icon="activation-verification-required" />
      <div className="min-w-0 flex-1">
        <p className="font-medium">Activation was submitted; verification is required.</p>
        <p className="mt-2 text-text-muted">Tobkiri will check the setup status again using the existing submission.</p>
        <div className="mt-4"><Button variant="outline" onClick={onRecover} loading={activating}>Verify activation</Button></div>
      </div>
      <CopyErrorButton label="Copy activation verification status" text={'Activation was submitted and needs verification.\nTobkiri will check the setup status again using the existing submission.'} />
    </div>}
    {!setup && !error && <p role="status" className="mt-6 text-sm text-text-muted">Loading verified catalog…</p>}
    {setup?.state === 'activation_denied' && <div role="alert" className="mt-4 flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-500">
      <ShieldAlert aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" data-error-icon="activation-denied" />
      <p className="min-w-0 flex-1 break-words">{setup.denial_diagnostic}</p>
      {!readOnly && <CopyErrorButton label="Copy activation denial" text={setup.denial_diagnostic} />}
    </div>}
    {setup && <div className="mt-6 space-y-3 text-sm">
      <Identity label="Base" value={setup.recommended_default_profile.base_pack} />
      <Identity label="Shell" value={setup.recommended_default_profile.shell.provider_id} />
      <Identity label="Conversation provider" value={setup.recommended_default_profile.conversation_provider} />
      <div className="rounded-lg border border-border bg-bg-main p-4">
        <p className="text-xs font-medium text-text-muted">Selected Packs</p>
        <ul className="mt-2 space-y-1 text-text-main">{setup.recommended_default_profile.packs.map((pack) => <li key={pack.pack_id}>{pack.display_name} <span className="text-xs text-text-muted">({pack.pack_id})</span></li>)}</ul>
      </div>
      <div className="rounded-lg border border-border bg-bg-main p-4">
        <p className="text-xs font-medium text-text-muted">Activation steps</p>
        <ol aria-label="Activation steps" className="mt-2 list-decimal space-y-1 pl-5 text-text-main">
          {setup.required_transaction.map((step) => <li key={step}>{activationStepLabels[step]}</li>)}
        </ol>
        <p className="mt-3 text-xs leading-5 text-text-muted">
          Tobkiri uses the revisions, operations, and permissions shown here for this activation.
        </p>
      </div>
      {predecessor && <div role="status" className="rounded-lg border border-border p-4 text-sm text-text-main">
        <p className="font-medium">Review the imported Harness successor</p>
        <p className="mt-2">Imported name: {predecessor.display_name}. Successor name: {setup.recommended_default_profile.name}.</p>
        <p className="mt-2">This activation appends the displayed bundled Harness as a new revision of “{predecessor.display_name}”. The imported definition remains in immutable history. Existing credentials and approvals are not carried into this activation.</p>
        <p className="mt-2">Pack selections, operation bindings, and scopes are replaced with the displayed bundled composition.</p>
        <p className="mt-2">Imported base: {predecessor.base_pack_id}. Imported Shell: {predecessor.shell_provider_id} ({predecessor.shell_pack_id}).</p>
        <p className="mt-2">Imported Packs: {predecessor.pack_ids.join(', ') || 'None'}.</p>
        <p className="mt-2">Added Packs: {addedPacks.join(', ') || 'None'}. Removed Packs: {removedPacks.join(', ') || 'None'}.</p>
      </div>}
      {packPinPredecessor && <div role="status" className="rounded-lg border border-border p-4 text-sm text-text-main">
        <p className="font-medium">Review saved Pack versions</p>
        <p className="mt-2">This activates the Pack versions saved in this Profile. Tobkiri verifies the current activation and saved revision again before applying them.</p>
      </div>}
      <DefaultsConfirmationDetails confirmation={setup.recommended_default_profile.confirmation} />
      {!activationAllowed && <p role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-text-main">
        {activationBlockedMessage}
      </p>}
      <label className="flex items-start gap-3 rounded-lg border border-border p-4 text-text-main">
        <input
          type="checkbox"
          data-native-setup-reviewed={nativeConfirmation || undefined}
          checked={reviewed}
          disabled={!canActivate || activating}
          onChange={canActivate ? (event) => onReviewedChange?.(event.target.checked) : undefined}
          className="mt-1 size-4 shrink-0"
          aria-describedby="defaults-review-confirmation"
        />
        <span id="defaults-review-confirmation">I confirm the catalog and Profile revisions, provider operations, and permission identifiers shown in the activation details.{predecessor && ' I also confirm the imported Profile, its new revision, and the Pack selection changes shown here.'}{packPinPredecessor && ' I also confirm the saved Pack versions for the current activation shown here.'}</span>
      </label>
      <Button size="lg" className="w-full" data-native-setup-confirm={nativeConfirmation || undefined}
        disabled={!reviewed || activating || !canActivate} loading={activating}
        onClick={canActivate ? onActivate : undefined}>Activate Tobkiri Harness</Button>
    </div>}
    {error && <div role="alert" className="mt-4 flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-500"><AlertCircle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" data-error-icon="setup" /><p className="min-w-0 flex-1 break-words">{error}</p>{!readOnly && <CopyErrorButton label="Copy setup error" text={error} />}</div>}
  </section>;
}

function Identity({label, value}: {readonly label: string; readonly value: string}) {
  return <div className="flex items-center justify-between gap-4 rounded-lg border border-border bg-bg-main p-4">
    <span className="text-text-muted">{label}</span><code className="break-all text-right text-xs text-text-main">{value}</code>
  </div>;
}
