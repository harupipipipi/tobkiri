import {useRef, useState} from 'react';

import {Button} from '@/src/components/ui/Button';
import {Card, CardContent, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import type {Pack, PackOperation} from '@/src/store';
import {formatUserFacingError} from '@/src/lib/userFacingError';

const QA_PACK_ID = 'tobkiri_packvm_sandbox_qa_pack';
const ISOLATION_OPERATION = `${QA_PACK_ID}.probe_isolation`;

export interface PackVMAcceptanceOperationProps {
  pack: Pack;
  operation: PackOperation;
  contributionVerified: boolean;
  pending: boolean;
  onInvoke: (payload: Record<string, unknown>) => Promise<unknown>;
}

function nonceHex(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function formatResult(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return 'Tobkiri returned evidence that could not be displayed safely.';
  }
}

/** Manual, acceptance-only surface for the signed PackVM QA fixture. */
export function PackVMAcceptanceOperation({
  pack,
  operation,
  contributionVerified,
  pending,
  onInvoke,
}: PackVMAcceptanceOperationProps) {
  const [armed, setArmed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);
  const submittingRef = useRef(false);

  if (pack.id !== QA_PACK_ID || operation.operationId !== ISOLATION_OPERATION) return null;
  const available = pack.installed
    && pack.approved
    && pack.enabled
    && operation.invokable
    && contributionVerified;
  const busy = submitting || pending;

  const run = async () => {
    if (!available || !armed || busy || submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    setError(null);
    setResult(null);
    try {
      setResult(await onInvoke({nonce: nonceHex()}));
    } catch (invokeError) {
      setError(formatUserFacingError(
        invokeError,
        'Tobkiri could not run the PackVM acceptance probe.',
        'packvm.acceptance.invoke',
      ));
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
      setArmed(false);
    }
  };

  return (
    <Card aria-labelledby="packvm-acceptance-title">
      <CardHeader>
        <CardTitle id="packvm-acceptance-title">PackVM QA acceptance only</CardTitle>
        <p className="text-sm leading-relaxed text-text-muted">
          This manually launches one signed QA-fixture isolation probe through the active QA
          Profile, Authority, Broker, and attested PackVM guest. It is never run automatically and
          does not add filesystem, network, process, VSOCK, or key access to the Defaults Profile.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {!available ? (
          <p className="rounded-lg border border-border bg-bg-hover/40 px-3 py-3 text-sm text-text-muted" role="status">
            Select a QA Profile containing the exact signed fixture, then approve and enable it.
            The launch remains unavailable until its verified contribution and PackVM attestation
            are present.
          </p>
        ) : null}
        <label className="flex items-start gap-3 text-sm text-text-main">
          <input
            checked={armed}
            className="mt-1"
            disabled={!available || busy}
            onChange={(event) => setArmed(event.target.checked)}
            type="checkbox"
          />
          <span>I understand this is a manual QA acceptance run, not a normal Pack action.</span>
        </label>
        <Button disabled={!available || !armed || busy} loading={busy} onClick={() => void run()}>
          Run guest isolation probe
        </Button>
        {error ? (
          <div className="flex items-start gap-2 text-sm text-destructive" role="alert">
            <p className="min-w-0 flex-1 break-words">{error}</p>
            <CopyErrorButton label="Copy acceptance error" text={error} />
          </div>
        ) : null}
        {result !== null ? (
          <pre className="max-h-72 overflow-auto rounded-lg border border-border bg-bg-main p-3 text-xs text-text-main" aria-label="PackVM guest evidence">
            {formatResult(result)}
          </pre>
        ) : null}
      </CardContent>
    </Card>
  );
}
