import type {PackControlBinding} from '@/src/lib/apiTypes';
import {Badge} from '@/src/components/ui/Badge';
import {isPackInCatalogScope} from '@/src/lib/packScope';
import type {Pack} from '@/src/store';

interface PackScopeSummaryProps {
  binding: PackControlBinding | null;
  pack?: Pick<Pack, 'profileId' | 'workspaceId' | 'profileRevision' | 'planDigest' | 'catalogRevision'>;
  packRows?: Array<Pick<Pack, 'profileId' | 'workspaceId' | 'profileRevision' | 'planDigest' | 'catalogRevision'>>;
  stale?: boolean;
  transitioning?: boolean;
}

/** Explain the Host-global inventory and Profile-scoped Pack state boundary. */
export function PackScopeSummary({binding, pack, packRows, stale = false, transitioning = false}: PackScopeSummaryProps) {
  const rowMatchesScope = pack
    ? isPackInCatalogScope(pack, binding)
    : !packRows || packRows.every((row) => isPackInCatalogScope(row, binding));
  const authoritative = binding !== null && rowMatchesScope;
  const rowLabel = pack ? 'this Pack row' : 'one or more Pack rows';

  return (
    <section
      aria-labelledby="pack-scope-title"
      className="rounded-xl border border-border bg-bg-card px-5 py-4 shadow-[var(--shadow-sm)]"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="pack-scope-title" className="text-sm font-semibold text-text-main">Pack catalog scope</h2>
          <p className="mt-1 max-w-3xl text-sm leading-relaxed text-text-muted">
            Host-global artifact inventory and install state. Required, membership, enablement, and approval are evaluated for the active execution Profile only.
          </p>
        </div>
        <Badge variant={authoritative || transitioning ? 'secondary' : 'warning'}>
          {authoritative ? 'Active execution Profile' : transitioning ? 'Updating Profile' : 'Profile scope unavailable'}
        </Badge>
      </div>
      {authoritative && binding ? (
        <dl className="mt-3 grid gap-2 text-xs text-text-muted sm:grid-cols-3">
          <div>
            <dt className="font-medium text-text-main">Authoritative Profile</dt>
            <dd className="mt-1 break-all font-mono">{binding.profile_id}</dd>
          </div>
          <div>
            <dt className="font-medium text-text-main">Profile revision</dt>
            <dd className="mt-1 break-all font-mono">{binding.profile_revision}</dd>
          </div>
          <div>
            <dt className="font-medium text-text-main">Plan digest</dt>
            <dd className="mt-1 break-all font-mono">{binding.plan_digest}</dd>
          </div>
        </dl>
      ) : transitioning ? (
        <p className="mt-3 text-sm text-text-muted" role="status">
          The Pack change is being applied. Profile-scoped actions will resume after the authoritative catalog is refreshed.
        </p>
      ) : (
        <p className="mt-3 text-sm text-amber-700 dark:text-amber-300" role="status">
          The active execution Profile scope is unavailable or does not match {rowLabel}; Profile-scoped Pack actions are locked until the authoritative catalog is refreshed.
        </p>
      )}
      {stale && authoritative ? (
        <p className="mt-3 text-xs text-text-muted" role="status">
          Showing the last successfully loaded catalog scope while the latest refresh is unavailable.
        </p>
      ) : null}
    </section>
  );
}
