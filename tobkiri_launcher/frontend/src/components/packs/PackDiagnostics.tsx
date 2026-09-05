import type {ApiFrontendDiagnostic} from '@/src/lib/apiTypes';
import {AlertCircle, AlertTriangle, Info} from 'lucide-react';
import {userSafePackVMError} from '@/src/lib/packvmLifecycle';
import {Badge} from '@/src/components/ui/Badge';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {Card, CardContent, CardHeader, CardTitle} from '@/src/components/ui/Card';

type DiagnosticSeverity = 'error' | 'warning' | 'info';

function severityOf(diagnostic: ApiFrontendDiagnostic): DiagnosticSeverity {
  const severity = diagnostic.severity?.toLowerCase();
  if (severity === 'error' || severity === 'critical') return 'error';
  if (severity === 'warning' || severity === 'warn') return 'warning';
  return 'info';
}

function severityVariant(severity: DiagnosticSeverity): 'destructive' | 'warning' | 'secondary' {
  if (severity === 'error') return 'destructive';
  if (severity === 'warning') return 'warning';
  return 'secondary';
}

function DiagnosticSeverityIcon({
  severity,
  blocking,
}: {
  severity: DiagnosticSeverity;
  blocking: boolean;
}) {
  if (blocking || severity === 'error') {
    return <AlertCircle aria-hidden="true" className="h-4 w-4 shrink-0 text-destructive" data-diagnostic-icon="error" />;
  }
  if (severity === 'warning') {
    return <AlertTriangle aria-hidden="true" className="h-4 w-4 shrink-0 text-amber-600" data-diagnostic-icon="warning" />;
  }
  return <Info aria-hidden="true" className="h-4 w-4 shrink-0 text-text-muted" data-diagnostic-icon="info" />;
}

export interface PackDiagnosticsProps {
  diagnostics: ApiFrontendDiagnostic[];
  title?: string;
}

function diagnosticText(diagnostic: ApiFrontendDiagnostic): string {
  const owner = diagnostic.owner_pack_id ?? diagnostic.pack_id;
  const recovery = diagnostic.code === 'production_backend_unavailable'
    ? 'Invocation remains unavailable until Tobkiri reports a healthy verified backend.'
    : null;
  return [
    userSafePackVMError(diagnostic.code),
    userSafePackVMError(diagnostic.message),
    ...(recovery ? [recovery] : []),
    `Owner: ${owner ? userSafePackVMError(owner) : 'Catalog'}`,
    `Contribution: ${diagnostic.contribution_id
      ? userSafePackVMError(diagnostic.contribution_id)
      : '—'}`,
    `Operation: ${diagnostic.operation_id
      ? userSafePackVMError(diagnostic.operation_id)
      : '—'}`,
  ].join('\n');
}

export function PackDiagnostics({diagnostics, title = 'Capability diagnostics'}: PackDiagnosticsProps) {
  if (diagnostics.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <p className="text-sm leading-relaxed text-text-muted">
          These diagnostics are authoritative for the current verified catalog.
        </p>
      </CardHeader>
      <CardContent>
        <ul className="space-y-3">
          {diagnostics.map((diagnostic, index) => {
            const severity = severityOf(diagnostic);
            const blocking = severity === 'error' || diagnostic.code === 'production_backend_unavailable';
            const owner = diagnostic.owner_pack_id ?? diagnostic.pack_id;
            const location = diagnostic.contribution_id
              ?? diagnostic.operation_id
              ?? owner;
            const copiedDiagnostic = diagnosticText(diagnostic);
            return (
              <li
                key={`${diagnostic.code}:${location ?? 'catalog'}:${index}`}
                role={blocking ? 'alert' : 'status'}
                className="rounded-lg border border-border bg-bg-main p-3 text-sm"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={severityVariant(severity)}>{severity}</Badge>
                  <span className="font-medium text-text-main">
                    {userSafePackVMError(diagnostic.code)}
                  </span>
                </div>
                <div className="mt-2 flex items-start gap-2">
                  <DiagnosticSeverityIcon blocking={blocking} severity={severity} />
                  <p className="min-w-0 flex-1 break-words text-text-muted">{userSafePackVMError(diagnostic.message)}</p>
                  {blocking ? <CopyErrorButton label="Copy Pack diagnostic" text={copiedDiagnostic} /> : null}
                </div>
                {diagnostic.code === 'production_backend_unavailable' ? (
                  <p className="mt-2 text-xs text-text-muted">
                    Invocation remains unavailable until Tobkiri reports a healthy verified backend.
                  </p>
                ) : null}
                <dl className="mt-3 grid gap-2 text-xs text-text-muted sm:grid-cols-3">
                  <div>
                    <dt className="font-medium text-text-main">Owner</dt>
                    <dd className="mt-0.5 break-all font-mono">
                      {owner ? userSafePackVMError(owner) : 'Catalog'}
                    </dd>
                  </div>
                  <div>
                    <dt className="font-medium text-text-main">Contribution</dt>
                    <dd className="mt-0.5 break-all font-mono">
                      {diagnostic.contribution_id
                        ? userSafePackVMError(diagnostic.contribution_id)
                        : '—'}
                    </dd>
                  </div>
                  <div>
                    <dt className="font-medium text-text-main">Operation</dt>
                    <dd className="mt-0.5 break-all font-mono">
                      {diagnostic.operation_id
                        ? userSafePackVMError(diagnostic.operation_id)
                        : '—'}
                    </dd>
                  </div>
                </dl>
              </li>
            );
          })}
        </ul>
      </CardContent>
    </Card>
  );
}
