import {useEffect, useRef, useState} from 'react';
import {CircleAlert, ListPlus} from 'lucide-react';

import {Button} from '@/src/components/ui/Button';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {Input} from '@/src/components/ui/Input';
import {
  validateLauncherModelProfileInput,
  type LauncherModelProfileInput,
  type LauncherProviderConnectionStatus,
} from '@/src/lib/launcherConnections';

const COPY = {
  heading: 'Register a model',
  profileId: 'Registration id',
  profileIdHelper: 'Stable id, e.g. main-chat. Existing ids are only re-saved when every detail matches.',
  modelId: 'Model id',
  modelIdHelper: 'The exact model identifier the provider expects, e.g. gpt-4o-mini.',
  provider: 'Connection',
  providerChoose: 'Choose a connection…',
  providerEmpty: 'Register an enabled provider connection first.',
  displayName: 'Display name',
  submit: 'Register model',
  submitting: 'Registering…',
  done: 'The model was registered.',
  retryNote:
    'Saving always uses the current registry revisions. If a registration '
    + 'conflicts, the save fails closed and nothing is retried until you '
    + 'submit again.',
};

export function ModelProfileForm({
  disabled,
  providers,
  onSubmit,
}: {
  disabled: boolean;
  providers: readonly LauncherProviderConnectionStatus[];
  onSubmit: (input: LauncherModelProfileInput) => Promise<void>;
}) {
  const [profileId, setProfileId] = useState('');
  const [modelId, setModelId] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [providerInstanceId, setProviderInstanceId] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const mountedRef = useRef(true);
  // Same-render double clicks must not dispatch twice; state lags one render.
  const inFlightRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const enabledProviders = providers.filter((provider) => provider.enabled);
  // A removed or disabled provider clears the selection to an explicit
  // choice — never retarget the model to another provider silently.
  const selectedProvider = enabledProviders.some(
    (provider) => provider.provider_instance_id === providerInstanceId,
  )
    ? providerInstanceId
    : '';
  const inputsDisabled = disabled || submitting || enabledProviders.length === 0;

  const submit = async () => {
    if (disabled || submitting || inFlightRef.current) return;
    inFlightRef.current = true;
    setError(null);
    setDone(false);
    const input: LauncherModelProfileInput = {
      model_profile_id: profileId.trim(),
      model_id: modelId.trim(),
      provider_instance_id: selectedProvider,
      display_name: displayName,
    };
    const invalid = validateLauncherModelProfileInput(input);
    if (invalid) {
      inFlightRef.current = false;
      setError(invalid.error);
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit(input);
      if (!mountedRef.current) return;
      setDone(true);
    } catch (submitError) {
      if (!mountedRef.current) return;
      setError(
        submitError instanceof Error
          ? submitError.message
          : 'The registration could not be completed.',
      );
    } finally {
      inFlightRef.current = false;
      if (mountedRef.current) setSubmitting(false);
    }
  };

  return (
    <section aria-label={COPY.heading} className="flex flex-col gap-3">
      <h3 className="flex items-center gap-2 text-sm font-medium text-text-main">
        <ListPlus className="h-4 w-4" aria-hidden="true" />
        {COPY.heading}
      </h3>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Input
          id="launcher-model-profile-id"
          label={COPY.profileId}
          helperText={COPY.profileIdHelper}
          value={profileId}
          disabled={inputsDisabled}
          onChange={(event) => setProfileId(event.target.value)}
          placeholder="main-chat"
          autoComplete="off"
          spellCheck={false}
        />
        <Input
          id="launcher-model-model-id"
          label={COPY.modelId}
          helperText={COPY.modelIdHelper}
          value={modelId}
          disabled={inputsDisabled}
          onChange={(event) => setModelId(event.target.value)}
          placeholder="gpt-4o-mini"
          autoComplete="off"
          spellCheck={false}
        />
        <div className="space-y-1.5">
          <label htmlFor="launcher-model-provider" className="text-sm font-medium text-text-main">
            {COPY.provider}
          </label>
          {enabledProviders.length === 0 ? (
            <p className="text-xs text-text-muted">{COPY.providerEmpty}</p>
          ) : (
            <select
              id="launcher-model-provider"
              className="flex h-10 w-full rounded-lg border border-border bg-bg-main px-3 py-2 text-sm text-text-main disabled:cursor-not-allowed disabled:opacity-50"
              value={selectedProvider}
              disabled={inputsDisabled}
              onChange={(event) => setProviderInstanceId(event.target.value)}
            >
              <option value="">{COPY.providerChoose}</option>
              {enabledProviders.map((provider) => (
                <option
                  key={provider.provider_instance_id}
                  value={provider.provider_instance_id}
                >
                  {provider.display_name || provider.provider_instance_id}
                </option>
              ))}
            </select>
          )}
        </div>
        <Input
          id="launcher-model-display-name"
          label={COPY.displayName}
          value={displayName}
          disabled={inputsDisabled}
          onChange={(event) => setDisplayName(event.target.value)}
          placeholder="Main chat model"
          autoComplete="off"
        />
      </div>
      <p className="text-xs text-text-muted">{COPY.retryNote}</p>
      {error ? (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2" role="alert">
          <CircleAlert aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
          <p className="min-w-0 flex-1 break-words text-xs text-destructive">{error}</p>
          <CopyErrorButton label="Copy model registration error" text={error} />
        </div>
      ) : null}
      {done ? (
        <p className="text-xs text-success" role="status">{COPY.done}</p>
      ) : null}
      <div>
        <Button
          type="button"
          size="sm"
          loading={submitting}
          disabled={disabled || submitting || enabledProviders.length === 0}
          onClick={() => {
            void submit();
          }}
        >
          {submitting ? COPY.submitting : COPY.submit}
        </Button>
      </div>
    </section>
  );
}
