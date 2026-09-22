import {useRef, useState, type FormEvent} from 'react';

import {Button} from '@/src/components/ui/Button';
import {Card, CardContent, CardHeader, CardTitle} from '@/src/components/ui/Card';
import type {Pack, PackOperation} from '@/src/store';
import {formatUserFacingError} from '@/src/lib/userFacingError';

const FILE_INSPECT_OPERATION_ID = 'rumi_file_inspect_pack.file-inspect';
const FILE_INSPECT_NAMES = ['stat', 'read', 'list', 'search'] as const;
type FileInspectName = typeof FILE_INSPECT_NAMES[number];

export interface FileInspectOperationProps {
  pack: Pack;
  operation: PackOperation;
  contributionVerified: boolean;
  pending: boolean;
  onInvoke: (payload: Record<string, unknown>) => Promise<unknown>;
}

function isUnsafeWorkspacePath(path: string): boolean {
  if (!path || path.includes('\0') || path.includes('\\')) return true;
  if (path.startsWith('/') || /^[A-Za-z]:/.test(path) || path.startsWith('~')) return true;
  return path.split('/').some((segment) => segment === '..');
}

function formatResult(value: unknown): string {
  try {
    const serialized = JSON.stringify(value, null, 2);
    return serialized === undefined ? String(value) : serialized;
  } catch {
    return 'Tobkiri returned a result that could not be displayed safely.';
  }
}

export function FileInspectOperation({
  pack,
  operation,
  contributionVerified,
  pending,
  onInvoke,
}: FileInspectOperationProps) {
  const [name, setName] = useState<FileInspectName>('stat');
  const [path, setPath] = useState('');
  const [pattern, setPattern] = useState('');
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);

  if (operation.operationId !== FILE_INSPECT_OPERATION_ID) return null;

  const revoked = !pack.approved
    || pack.approvalStatus === 'revoked'
    || pack.approvalReason === 'approval_revoked'
    || pack.approvalIssues.includes('approval_revoked');
  const lifecycleReady = pack.installed && pack.enabled && pack.approved && !revoked;
  const busy = submitting || pending;
  const available = operation.invokable && contributionVerified && lifecycleReady;

  const availabilityMessage = !pack.installed
    ? 'Install this Pack before using its file inspection operation.'
    : revoked
      ? 'Tobkiri approval is revoked. Approve the Pack again before using file inspection.'
      : !pack.approved
        ? 'Approve this Pack before using file inspection.'
        : !pack.enabled
          ? 'Enable this Pack before using file inspection.'
          : !operation.invokable
            ? 'Tobkiri has not exposed a verified capability route for this operation.'
            : !contributionVerified
              ? 'Tobkiri has not exposed this Pack contribution in the current verified catalog.'
            : null;

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || submittingRef.current || !available) return;

    const form = event.currentTarget;
    const pathInput = form.elements.namedItem('path') as HTMLInputElement | null;
    const patternInput = form.elements.namedItem('pattern') as HTMLInputElement | null;
    const workspacePath = pathInput?.value.trim() ?? '';
    const patternValue = patternInput?.value.trim() ?? '';
    if (!workspacePath) {
      setError('Enter a workspace-relative file path.');
      setResult(null);
      return;
    }
    if (isUnsafeWorkspacePath(workspacePath)) {
      setError('Use a workspace-relative path without absolute prefixes, backslashes, or .. segments.');
      setResult(null);
      return;
    }

    submittingRef.current = true;
    setSubmitting(true);
    setError(null);
    setResult(null);
    try {
      const payload: Record<string, unknown> = {
        name,
        path: workspacePath,
        profile_id: pack.profileId,
        workspace_id: pack.workspaceId,
        require_selected: true,
      };
      if (name === 'search' && patternValue) payload.pattern = patternValue;
      const response = await onInvoke(payload);
      setResult(response);
    } catch (invokeError) {
      setError(formatUserFacingError(
        invokeError,
        'Tobkiri could not inspect that file.',
        'file.inspect.invoke',
      ));
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  return (
    <Card aria-labelledby="file-inspect-title">
      <CardHeader>
        <CardTitle id="file-inspect-title">Safe file inspection</CardTitle>
        <p className="text-sm leading-relaxed text-text-muted">
          Inspect a file inside the selected Tobkiri workspace through the approved Pack Broker.
          Absolute paths, traversal, and symlink escapes are rejected by the runtime boundary.
        </p>
      </CardHeader>
      <CardContent>
        {availabilityMessage ? (
          <p className="mb-4 rounded-lg border border-border bg-bg-hover/40 px-3 py-3 text-sm text-text-muted" role="status">
            {availabilityMessage}
          </p>
        ) : null}
        <form className="space-y-4" onSubmit={(event) => void handleSubmit(event)}>
          <div className="grid gap-4 sm:grid-cols-[minmax(0,10rem)_minmax(0,1fr)]">
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-main" htmlFor="file-inspect-name">
                Operation
              </label>
              <select
                className="min-h-11 w-full rounded-lg border border-border bg-bg-main px-3 text-sm text-text-main focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                disabled={!available || busy}
                id="file-inspect-name"
                name="name"
                onChange={(event) => setName(event.target.value as FileInspectName)}
                value={name}
              >
                {FILE_INSPECT_NAMES.map((value) => <option key={value} value={value}>{value}</option>)}
              </select>
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-main" htmlFor="file-inspect-path">
                Workspace-relative file path
              </label>
              <input
                aria-describedby="file-inspect-path-help"
                autoComplete="off"
                className="min-h-11 w-full rounded-lg border border-border bg-bg-main px-3 text-sm text-text-main placeholder:text-text-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                disabled={!available || busy}
                id="file-inspect-path"
                inputMode="text"
                name="path"
                onChange={(event) => setPath(event.target.value)}
                placeholder="docs/example.txt"
                required
                spellCheck={false}
                value={path}
              />
              <p className="text-xs text-text-muted" id="file-inspect-path-help">
                Enter or select a path relative to the selected workspace. The runtime performs the final jail and symlink checks.
              </p>
            </div>
          </div>
          {name === 'search' ? (
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-main" htmlFor="file-inspect-pattern">
                Search pattern (optional)
              </label>
              <input
                autoComplete="off"
                className="min-h-11 w-full rounded-lg border border-border bg-bg-main px-3 text-sm text-text-main placeholder:text-text-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                disabled={!available || busy}
                id="file-inspect-pattern"
                name="pattern"
                onChange={(event) => setPattern(event.target.value)}
                placeholder="*.md"
                spellCheck={false}
                value={pattern}
              />
            </div>
          ) : null}
          <div className="flex flex-wrap items-center gap-3">
            <Button
              className="min-h-11"
              disabled={!available || busy || !path.trim()}
              loading={busy}
              type="submit"
              aria-busy={busy}
            >
              {busy ? 'Inspecting…' : 'Inspect file'}
            </Button>
            {error ? <p className="text-sm text-destructive" role="alert">{error}</p> : null}
          </div>
        </form>
        {result !== null ? (
          <div className="mt-5 space-y-2">
            <h4 className="text-sm font-medium text-text-main">Inspection result</h4>
            <pre aria-live="polite" className="max-h-80 overflow-auto rounded-lg border border-border bg-bg-hover/40 p-3 text-xs text-text-main">
              {formatResult(result)}
            </pre>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

export {FILE_INSPECT_OPERATION_ID};
