import type {ReactNode} from 'react';
import {Link} from 'react-router';
import {UserRound} from 'lucide-react';

import {useProfileSelection} from '@/src/lib/profileSelection';
import {profileHref} from '@/src/lib/routes';

/**
 * State which execution Profile a surface's runtime data is bound to.
 *
 * Runtime surfaces are captured from the active execution Profile only, so
 * when the panel-wide inspection selection names a different Profile the
 * notice says the surface cannot show it and that Profile-scoped writes are
 * locked (see isProfileMutationBlocked) — instead of implying the data
 * belongs to, or can be mutated as, the selection.
 */
export function InspectedProfileNotice({
  surfaceProfileId,
}: {
  surfaceProfileId?: string | null;
}): ReactNode {
  const {selectedProfileId} = useProfileSelection();
  if (!selectedProfileId) return null;

  if (typeof surfaceProfileId === 'string' && surfaceProfileId !== selectedProfileId) {
    return (
      <p
        className="rounded-lg border border-warning/35 bg-warning/8 px-4 py-3 text-sm text-warning"
        role="status"
        data-testid="inspected-profile-notice"
      >
        Selected Profile <span className="font-mono">{selectedProfileId}</span> is not the active
        execution Profile. Profile-scoped writes here are locked, and the runtime state below
        reports the active execution Profile{' '}
        <span className="font-mono">{surfaceProfileId}</span>.{' '}
        <Link className="underline underline-offset-2" to={profileHref(selectedProfileId)}>
          Review or activate it on the Profile page
        </Link>
        .
      </p>
    );
  }

  return (
    <p
      className="flex items-center gap-2 text-xs text-text-muted"
      role="status"
      data-testid="inspected-profile-notice"
    >
      <UserRound aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
      {surfaceProfileId === selectedProfileId ? (
        <span>
          Inspecting the active execution Profile{' '}
          <span className="font-mono">{selectedProfileId}</span>.
        </span>
      ) : (
        <span>
          Selected Profile <span className="font-mono">{selectedProfileId}</span>. This surface
          binds to the active execution Profile only — writes are locked until it is active and
          its runtime state cannot appear here.{' '}
          <Link className="underline underline-offset-2" to={profileHref(selectedProfileId)}>
            Open its Profile page
          </Link>
          .
        </span>
      )}
    </p>
  );
}
