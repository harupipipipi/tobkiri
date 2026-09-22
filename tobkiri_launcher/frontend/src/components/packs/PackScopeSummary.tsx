import {Link} from 'react-router';
import {profileHref} from '@/src/lib/routes';
import type {PackControlBinding} from '@/src/lib/apiTypes';
import {Badge} from '@/src/components/ui/Badge';
import {isPackInCatalogScope} from '@/src/lib/packScope';
import type {Pack} from '@/src/store';

interface PackScopeSummaryProps {
  binding: PackControlBinding | null;
  pack?: Pick<Pack, 'profileId' | 'workspaceId' | 'profileRevision' | 'planDigest' | 'catalogRevision'>;
  packRows?: Array<Pick<Pack, 'profileId' | 'workspaceId' | 'profileRevision' | 'planDigest' | 'catalogRevision'>>;
  stale?: boolean;
}

/** Explain the Host-global inventory and Profile-scoped Pack state boundary. */
export function PackScopeSummary({binding, pack, packRows, stale = false}: PackScopeSummaryProps) {
  const rowMatchesScope = pack
    ? isPackInCatalogScope(pack, binding)
    : !packRows || packRows.every((row) => isPackInCatalogScope(row, binding));
  const authoritative = binding !== null && rowMatchesScope;
  const rowLabel = pack ? 'this Pack row' : 'one or more Pack rows';

  return (
    <section
      aria-labelledby="pack-scope-title"
      className="border-b border-border pb-4"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="pack-scope-title" className="text-sm font-semibold text-text-main">Pack availability</h2>
          <p className="mt-1 max-w-3xl text-sm leading-relaxed text-text-muted">
            Installed Packs are shared on this device. Enabled state and approval belong to the active Profile.
          </p>
        </div>
        {authoritative && binding && !stale ? <Link className="text-sm font-medium text-accent underline-offset-4 hover:underline" to={profileHref(binding.profile_id, 'profile-packs')}>Edit Packs in this Profile</Link> : null}
        <Badge variant={authoritative ? 'secondary' : 'warning'}>
          {authoritative ? `Active Profile: ${binding?.profile_id}` : 'Profile scope unavailable'}
        </Badge>
      </div>
      {authoritative && binding ? (
        <details className="mt-3 text-xs text-text-muted">
          <summary className="cursor-pointer">Technical details</summary>
        <dl className="mt-3 grid gap-2 sm:grid-cols-3">
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
        </details>
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
