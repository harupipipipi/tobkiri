import {AlertTriangle, Bot, ShieldCheck} from 'lucide-react';

import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {Card} from '@/src/components/ui/Card';
import type {PackConflictReport} from '@/src/lib/apiTypes';
import type {PackRepairAction} from '@/src/store';

interface PackConflictCenterProps {
  conflicts: PackConflictReport[];
  pending: Record<string, boolean>;
  onAction: (conflictId: string, action: PackRepairAction) => Promise<void>;
}

function actionButton(
  conflict: PackConflictReport,
  action: PackRepairAction,
  label: string,
  pending: Record<string, boolean>,
  onAction: PackConflictCenterProps['onAction'],
) {
  const key = `${conflict.conflict_id}:${action}`;
  return (
    <Button
      key={action}
      type="button"
      size="sm"
      variant={action === 'remove' || action === 'disable' ? 'outline' : 'default'}
      className="min-h-11"
      loading={Boolean(pending[key])}
      disabled={Object.keys(pending).some((candidate) => candidate.startsWith(`${conflict.conflict_id}:`))}
      onClick={() => { void onAction(conflict.conflict_id, action); }}
    >
      {label}
    </Button>
  );
}

function repairActions(
  conflict: PackConflictReport,
  pending: Record<string, boolean>,
  onAction: PackConflictCenterProps['onAction'],
) {
  const state = conflict.repair?.state;
  if (!state) return [actionButton(conflict, 'generate', 'Generate repair pack', pending, onAction)];
  if (state === 'generated' || state === 'blocked') {
    return [
      actionButton(conflict, 'review', 'Review generated repair', pending, onAction),
      actionButton(conflict, 'regenerate', 'Regenerate', pending, onAction),
      actionButton(conflict, 'remove', 'Remove', pending, onAction),
    ];
  }
  if (state === 'validated') return [actionButton(conflict, 'approve', 'Approve', pending, onAction)];
  if (state === 'approved') return [actionButton(conflict, 'install', 'Install', pending, onAction)];
  if (state === 'installed') return [actionButton(conflict, 'activate', 'Activate', pending, onAction)];
  if (state === 'active') {
    return [
      actionButton(conflict, 'disable', 'Disable', pending, onAction),
      actionButton(conflict, 'regenerate', 'Regenerate', pending, onAction),
      actionButton(conflict, 'remove', 'Remove', pending, onAction),
    ];
  }
  return [
    actionButton(conflict, 'regenerate', 'Regenerate', pending, onAction),
    actionButton(conflict, 'remove', 'Remove', pending, onAction),
  ];
}

export function PackConflictCenter({conflicts, pending, onAction}: PackConflictCenterProps) {
  if (conflicts.length === 0) return null;
  const repairableCount = conflicts.filter((conflict) => conflict.repairable).length;
  return (
    <section aria-labelledby="pack-conflicts-title" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 id="pack-conflicts-title" className="text-lg font-semibold text-text-main">Pack conflicts</h2>
          <p className="text-sm text-text-muted">{conflicts.length} blocked conflict{conflicts.length === 1 ? '' : 's'} · {repairableCount} potentially repairable</p>
        </div>
        <Badge variant="destructive">Activation blocked</Badge>
      </div>
      {conflicts.map((conflict) => {
        const review = conflict.repair;
        return (
          <Card key={conflict.conflict_id} className="p-5" aria-label={`Pack conflict ${conflict.conflict_id}`}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <AlertTriangle className="h-4 w-4 text-destructive" aria-hidden="true" />
                  <h3 className="font-semibold text-text-main">{conflict.kind.replaceAll('_', ' ')}</h3>
                  {review ? <Badge variant={review.state === 'active' ? 'success' : review.state === 'stale' || review.state === 'modified' ? 'destructive' : 'warning'}>Generated repair · {review.state}</Badge> : null}
                </div>
                <p className="mt-2 break-all font-mono text-xs text-text-muted">{conflict.conflict_id}</p>
              </div>
              <Badge variant={conflict.repairable ? 'warning' : 'destructive'}>
                {conflict.repairable ? 'Repairable' : 'Manual resolution required'}
              </Badge>
            </div>
            <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
              <div><dt className="font-medium text-text-main">Involved Packs</dt><dd className="mt-1 text-text-muted">{conflict.involved_packs.map((pack) => `${pack.pack_id} ${pack.version} (${pack.artifact_hash.slice(0, 18)}…)`).join(', ')}</dd></div>
              <div><dt className="font-medium text-text-main">Contracts / resources</dt><dd className="mt-1 text-text-muted">{[...conflict.affected_contracts, ...conflict.affected_resources].join(', ') || 'Profile-level conflict'}</dd></div>
              {review ? <>
                <div><dt className="font-medium text-text-main">Validation and dry run</dt><dd className="mt-1 text-text-muted">{review.validation_passed ? 'Validation passed' : 'Validation pending or blocked'} · {review.dry_run_resolved ? 'Conflict resolved in dry run' : 'Dry run unresolved'}</dd></div>
                <div><dt className="font-medium text-text-main">Capability / permission delta</dt><dd className="mt-1 text-text-muted">{review.capability_delta.length ? review.capability_delta.join(', ') : 'None'}</dd></div>
                <div><dt className="font-medium text-text-main">Artifact binding</dt><dd className="mt-1 break-all font-mono text-xs text-text-muted">{review.artifact_hash}</dd></div>
                <div><dt className="font-medium text-text-main">Approval</dt><dd className="mt-1 flex items-center gap-1 text-text-muted">{review.approval_actor_id ? <ShieldCheck className="h-4 w-4 text-success" aria-hidden="true" /> : <Bot className="h-4 w-4" aria-hidden="true" />}{review.approval_actor_id ? `Reviewed by ${review.approval_actor_id}` : 'Separate human review required'}</dd></div>
              </> : null}
            </dl>
            {conflict.diagnostics.length ? <ul className="mt-4 list-disc pl-5 text-sm text-text-muted">{conflict.diagnostics.map((diagnostic) => <li key={diagnostic}>{diagnostic}</li>)}</ul> : null}
            {conflict.repairable ? <div className="mt-5 flex flex-wrap gap-2" role="group" aria-label={`Repair actions for ${conflict.conflict_id}`}>{repairActions(conflict, pending, onAction)}</div> : null}
          </Card>
        );
      })}
    </section>
  );
}
