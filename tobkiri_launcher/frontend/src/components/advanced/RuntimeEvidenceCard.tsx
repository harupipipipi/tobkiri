import {Badge} from '@/src/components/ui/Badge';
import type {RuntimeSurfaceEnvelope} from '@/src/lib/runtimeSurface';

const recordLabels = {
  profile_lock: 'ProfileLock',
  resolved_plan: 'ResolvedPlan',
  activation_record: 'ActivationRecord',
  authority_snapshot: 'Authority snapshot',
} as const;

export function RuntimeEvidenceCard<T>({
  envelope,
  title = 'Canonical runtime evidence',
}: {
  envelope: RuntimeSurfaceEnvelope<T>;
  title?: string;
}) {
  return (
    <section className="rounded-xl border border-border bg-bg-card p-4 sm:p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold text-text-main">{title}</h2>
      <p className="mt-1 text-xs text-text-muted">Digest-pinned evidence references for the accepted v4 projection.</p>
        </div>
        <Badge variant="success">ready</Badge>
      </div>
      <dl className="mt-4 grid gap-3 sm:grid-cols-3">
        <div className="min-w-0">
          <dt className="text-xs font-medium text-text-muted">Profile</dt>
          <dd className="mt-1 break-all font-mono text-xs text-text-main">{envelope.profile_id}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-xs font-medium text-text-muted">Profile revision</dt>
          <dd className="mt-1 break-all font-mono text-xs text-text-main">{envelope.profile_revision}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-xs font-medium text-text-muted">Plan digest</dt>
          <dd className="mt-1 break-all font-mono text-xs text-text-main">{envelope.plan_digest}</dd>
        </div>
      </dl>
      <div className="mt-4 grid gap-2 sm:grid-cols-2">
        {(Object.entries(envelope.records) as Array<[keyof typeof recordLabels, typeof envelope.records[keyof typeof recordLabels]]>).map(([kind, record]) => (
          <div key={kind} className="min-w-0 rounded-lg border border-border/70 bg-bg-main px-3 py-2">
            <div className="text-xs font-medium text-text-main">{recordLabels[kind]}</div>
            <div className="mt-1 break-all font-mono text-xs text-text-main">{record.digest}</div>
            <div className="mt-1 break-all text-xs text-text-muted">{record.source_ref}</div>
          </div>
        ))}
      </div>
    </section>
  );
}
