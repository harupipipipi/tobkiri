import {useEffect, useRef, useState} from 'react';
import {CircleAlert, KeyRound} from 'lucide-react';

import {Button} from '@/src/components/ui/Button';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {Input} from '@/src/components/ui/Input';
import {
  LAUNCHER_AUDIO_CAPABILITIES,
  LAUNCHER_PROVIDER_SETUP_PRESETS,
  launcherProviderSetupPreset,
  validateLauncherProviderSetup,
  type LauncherProviderConfigureRequest,
  type LauncherProviderProtocol,
} from '@/src/lib/launcherConnections';

const COPY = {
  heading: 'Register a provider connection',
  provider: 'Provider',
  customProvider: 'Custom HTTPS provider',
  localProvider: 'Local keyless server (loopback only)',
  providerId: 'Provider id',
  apiId: 'Connection suffix',
  apiIdHelper: 'Letters, digits, dots, dashes, underscores. The registered name becomes provider.suffix.',
  protocol: 'Protocol',
  endpoint: 'Endpoint',
  key: 'API key',
  keyHelper: 'Sent only to the Host through the approval flow. Never stored by the panel.',
  keylessHelper: 'Local connections are saved without a key and require a numeric loopback endpoint.',
  submit: 'Register connection',
  submitting: 'Waiting for Host approval…',
  done: 'The connection was registered.',
  approvalNote:
    'Saving opens the Host approval window; the registration completes only '
    + 'after explicit approval there. The key is never stored by this panel '
    + 'and is never resent automatically.',
  audioGrant:
    'Optional audio scopes. Checked scopes are frozen into the Host approval '
    + 'with this new credential; leaving them unchecked keeps the existing '
    + 'text-only key behavior.',
  audioTranscribe: 'Audio transcription (ai.audio.transcribe)',
  audioSpeech: 'Audio speech (ai.audio.speech)',
};

type Mode = 'preset' | 'custom' | 'local';

const CUSTOM_MODE = 'custom';
const LOCAL_MODE = 'local';
const LOCAL_PROVIDER_ID = 'local';
const LOCAL_PROTOCOL: LauncherProviderProtocol = 'local-openai-compatible';

const PRESET_IDS = Object.keys(LAUNCHER_PROVIDER_SETUP_PRESETS).sort();
const CUSTOM_PROTOCOLS: readonly LauncherProviderProtocol[] = [
  'openai-compatible',
  'anthropic',
];

export function ProviderConnectionForm({
  disabled,
  onSubmit,
}: {
  disabled: boolean;
  onSubmit: (request: LauncherProviderConfigureRequest) => Promise<void>;
}) {
  const [selection, setSelection] = useState<string>(PRESET_IDS[0] ?? CUSTOM_MODE);
  const [customProviderId, setCustomProviderId] = useState('');
  const [customProtocol, setCustomProtocol] =
    useState<LauncherProviderProtocol>('openai-compatible');
  const [apiId, setApiId] = useState('default');
  const [endpoint, setEndpoint] = useState('');
  const [key, setKey] = useState('');
  const [audioTranscribe, setAudioTranscribe] = useState(false);
  const [audioSpeech, setAudioSpeech] = useState(false);
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

  const mode: Mode =
    selection === CUSTOM_MODE ? 'custom' : selection === LOCAL_MODE ? 'local' : 'preset';
  const preset = mode === 'preset' ? launcherProviderSetupPreset(selection) : null;
  const providerId =
    mode === 'preset'
      ? selection
      : mode === 'local'
        ? LOCAL_PROVIDER_ID
        : customProviderId;
  const shownEndpoint = mode === 'preset' ? preset?.endpoint ?? '' : endpoint;
  const inputsDisabled = disabled || submitting;
  const effectiveProtocol: LauncherProviderProtocol | undefined =
    mode === 'preset'
      ? preset?.protocol
      : mode === 'local'
        ? LOCAL_PROTOCOL
        : customProtocol;
  const audioGrantVisible = effectiveProtocol === 'openai-compatible';
  const checkedAudio = LAUNCHER_AUDIO_CAPABILITIES.filter(
    (capability) =>
      (capability === 'ai.audio.transcribe' && audioTranscribe)
      || (capability === 'ai.audio.speech' && audioSpeech),
  );

  const submit = async () => {
    if (inputsDisabled || inFlightRef.current) return;
    inFlightRef.current = true;
    setError(null);
    setDone(false);
    const result = validateLauncherProviderSetup({
      providerId,
      apiId,
      endpoint: mode === 'preset' ? undefined : endpoint,
      protocol:
        mode === 'preset'
          ? undefined
          : mode === 'local'
            ? LOCAL_PROTOCOL
            : customProtocol,
      key: mode === 'local' ? '' : key,
      capabilities: audioGrantVisible && checkedAudio.length > 0
        ? ['ai.generate', 'ai.stream', ...checkedAudio]
        : undefined,
    });
    if ('error' in result) {
      inFlightRef.current = false;
      setError(result.error);
      return;
    }
    const request = result.request;
    setSubmitting(true);
    // Cleared after handoff: the visible field no longer carries the secret
    // while the Host-owned effect proceeds.
    setKey('');
    try {
      await onSubmit(request);
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
        <KeyRound className="h-4 w-4" aria-hidden="true" />
        {COPY.heading}
      </h3>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <label htmlFor="launcher-connection-provider" className="text-sm font-medium text-text-main">
            {COPY.provider}
          </label>
          <select
            id="launcher-connection-provider"
            className="flex h-10 w-full rounded-lg border border-border bg-bg-main px-3 py-2 text-sm text-text-main disabled:cursor-not-allowed disabled:opacity-50"
            value={selection}
            disabled={inputsDisabled}
            onChange={(event) => setSelection(event.target.value)}
          >
            {PRESET_IDS.map((id) => (
              <option key={id} value={id}>{id}</option>
            ))}
            <option value={CUSTOM_MODE}>{COPY.customProvider}</option>
            <option value={LOCAL_MODE}>{COPY.localProvider}</option>
          </select>
        </div>
        {mode === 'custom' ? (
          <Input
            id="launcher-connection-provider-id"
            label={COPY.providerId}
            value={customProviderId}
            disabled={inputsDisabled}
            onChange={(event) => setCustomProviderId(event.target.value)}
            placeholder="myprovider"
            autoComplete="off"
            spellCheck={false}
          />
        ) : null}
        <Input
          id="launcher-connection-suffix"
          label={COPY.apiId}
          helperText={COPY.apiIdHelper}
          value={apiId}
          disabled={inputsDisabled}
          onChange={(event) => setApiId(event.target.value)}
          placeholder="default"
          autoComplete="off"
          spellCheck={false}
        />
        {mode === 'custom' ? (
          <div className="space-y-1.5">
            <label htmlFor="launcher-connection-protocol" className="text-sm font-medium text-text-main">
              {COPY.protocol}
            </label>
            <select
              id="launcher-connection-protocol"
              className="flex h-10 w-full rounded-lg border border-border bg-bg-main px-3 py-2 text-sm text-text-main disabled:cursor-not-allowed disabled:opacity-50"
              value={customProtocol}
              disabled={inputsDisabled}
              onChange={(event) =>
                setCustomProtocol(event.target.value as LauncherProviderProtocol)}
            >
              {CUSTOM_PROTOCOLS.map((protocol) => (
                <option key={protocol} value={protocol}>{protocol}</option>
              ))}
            </select>
          </div>
        ) : null}
        {mode === 'preset' ? (
          <Input
            id="launcher-connection-endpoint"
            label={COPY.endpoint}
            value={shownEndpoint}
            disabled
            readOnly
            autoComplete="off"
          />
        ) : (
          <Input
            id="launcher-connection-endpoint"
            label={COPY.endpoint}
            value={endpoint}
            disabled={inputsDisabled}
            onChange={(event) => setEndpoint(event.target.value)}
            placeholder={
              mode === 'local' ? 'http://127.0.0.1:8080/v1' : 'https://'
            }
            autoComplete="off"
            spellCheck={false}
          />
        )}
        {mode !== 'local' ? (
          <Input
            id="launcher-connection-key"
            label={COPY.key}
            helperText={COPY.keyHelper}
            type="password"
            value={key}
            disabled={inputsDisabled}
            onChange={(event) => setKey(event.target.value)}
            autoComplete="new-password"
            spellCheck={false}
          />
        ) : (
          <p className="self-end text-xs text-text-muted">{COPY.keylessHelper}</p>
        )}
      </div>
      {audioGrantVisible ? (
        <fieldset className="rounded-lg border border-border px-3 py-2">
          <legend className="px-1 text-xs text-text-muted">{COPY.audioGrant}</legend>
          <label className="mt-1 flex items-center gap-2 text-sm text-text-main">
            <input
              type="checkbox"
              checked={audioTranscribe}
              disabled={inputsDisabled}
              onChange={(event) => setAudioTranscribe(event.target.checked)}
            />
            {COPY.audioTranscribe}
          </label>
          <label className="mt-1 flex items-center gap-2 text-sm text-text-main">
            <input
              type="checkbox"
              checked={audioSpeech}
              disabled={inputsDisabled}
              onChange={(event) => setAudioSpeech(event.target.checked)}
            />
            {COPY.audioSpeech}
          </label>
        </fieldset>
      ) : null}
      <p className="text-xs text-text-muted">{COPY.approvalNote}</p>
      {error ? (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2" role="alert">
          <CircleAlert aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
          <p className="min-w-0 flex-1 break-words text-xs text-destructive">{error}</p>
          <CopyErrorButton label="Copy connection registration error" text={error} />
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
          disabled={inputsDisabled}
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
