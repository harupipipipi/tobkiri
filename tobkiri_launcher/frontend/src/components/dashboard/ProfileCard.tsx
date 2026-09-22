import type {FormEvent} from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Copy,
  Edit2,
  MoreHorizontal,
  Package,
  Rocket,
  Trash2,
} from 'lucide-react';
import {Link} from 'react-router';

import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {Card} from '@/src/components/ui/Card';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {Popover, PopoverContent, PopoverTrigger} from '@/src/components/ui/Popover';
import type {NamedProfileRecord} from '@/src/lib/profileRegistry';
import type {NamedProfileView} from '@/src/lib/profileRegistryView';
import {cn} from '@/src/lib/utils';
import {ProfileCover} from './ProfileCover';

export interface ProfileCardProps {
  activationHref: string;
  browseHref: string;
  closureHref: string;
  editing: boolean;
  editingName: string;
  isActive: boolean;
  isBrowsing: boolean;
  isBusy: boolean;
  mutationsAvailable: boolean;
  profile: NamedProfileRecord;
  profileView: NamedProfileView;
  profileCeremonyAvailable: boolean;
  activeProfileReady: boolean;
  launchReady: boolean;
  desktopShellAvailable: boolean;
  actionType?: string | null;
  onCancelEdit: () => void;
  onDelete: (profile: NamedProfileRecord) => void;
  onDuplicate: (profile: NamedProfileRecord) => void;
  onEdit: (profile: NamedProfileRecord) => void;
  onEditingNameChange: (name: string) => void;
  onLaunch: (profile: NamedProfileRecord) => void;
  onSubmitEdit: (event: FormEvent<HTMLFormElement>, profile: NamedProfileRecord) => void;
}

function actionButtonClass(): string {
  return 'flex min-h-10 w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-text-main transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]';
}

export function ProfileCard({
  activationHref,
  browseHref,
  closureHref,
  editing,
  editingName,
  isActive,
  isBrowsing,
  isBusy,
  mutationsAvailable,
  profile,
  profileView,
  profileCeremonyAvailable,
  activeProfileReady,
  launchReady,
  desktopShellAvailable,
  actionType,
  onCancelEdit,
  onDelete,
  onDuplicate,
  onEdit,
  onEditingNameChange,
  onLaunch,
  onSubmitEdit,
}: ProfileCardProps) {
  const displayName = profileView.displayName;
  const launchBlockedReason = !isActive
    ? 'Activate this Profile before launching it.'
    : profileView.status === 'error'
      ? profileView.statusDescription ?? 'Resolve this Profile before launching it.'
      : !activeProfileReady
        ? 'The active execution Profile is not ready.'
        : !launchReady
          ? 'Launch is unavailable until runtime readiness is confirmed.'
          : !desktopShellAvailable
            ? 'Launch is available in Tobkiri Launcher.'
            : null;
  const launchDisabled = Boolean(launchBlockedReason) || isBusy;
  const mutationDisabled = !mutationsAvailable || isBusy;
  const profileErrorDiagnostic = profileView.status === 'error'
    ? `${profileView.statusLabel}. ${profileView.statusDescription ?? 'Profile needs attention.'}`
    : null;

  return (
    <Card
      aria-labelledby={`profile-${profile.profile_id}-title`}
      className={cn(
        'group relative flex h-full min-w-0 w-full flex-col overflow-hidden transition-shadow duration-[var(--transition-base)] hover:shadow-[var(--shadow-md)]',
        isActive && 'ring-1 ring-accent/30',
        isBrowsing && 'border-accent/50',
      )}
      data-profile-card={profile.profile_id}
      data-profile-status={profileView.status}
    >
      <div className="relative aspect-[16/9] shrink-0 border-b border-border">
        <ProfileCover profileId={profile.profile_id} />
        {isActive && (
          <span className="absolute left-3 top-3 inline-flex max-w-[calc(100%-4rem)] items-center gap-1.5 rounded-full border border-accent/40 bg-bg-main/90 px-2 py-1 text-xs font-medium text-accent">
            <CheckCircle2 aria-hidden="true" className="h-3 w-3 shrink-0" />
            <span className="truncate">Active execution</span>
          </span>
        )}
        <div className="absolute right-3 top-3">
          <Popover>
            <PopoverTrigger
              aria-label={`Open actions for ${displayName}`}
              className="rounded-lg border border-border bg-bg-main/90 p-2 text-text-main transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
              title={`Open actions for ${displayName}`}
            >
              <MoreHorizontal aria-hidden="true" className="h-4 w-4" />
              <span className="sr-only">Profile actions</span>
            </PopoverTrigger>
            <PopoverContent align="right" className="w-48">
              <div className="flex flex-col gap-0.5 py-1">
                <button
                  className={actionButtonClass()}
                  disabled={mutationDisabled}
                  onClick={() => onEdit(profile)}
                  role="menuitem"
                  title={mutationsAvailable ? `Edit ${displayName}` : 'Profile catalog verification is unavailable'}
                  type="button"
                >
                  <Edit2 aria-hidden="true" className="h-3.5 w-3.5" /> Edit
                </button>
                {isActive ? (
                  <button
                    aria-disabled="true"
                    className={cn(actionButtonClass(), 'cursor-not-allowed text-text-muted opacity-60')}
                    disabled
                    role="menuitem"
                    type="button"
                  >
                    <CheckCircle2 aria-hidden="true" className="h-3.5 w-3.5 text-accent" /> Active
                  </button>
                ) : (
                  <Link
                    aria-disabled={!profileCeremonyAvailable}
                    aria-label={`Activate ${displayName}`}
                    className={cn(
                      actionButtonClass(),
                      !profileCeremonyAvailable && 'cursor-not-allowed text-text-muted opacity-60',
                    )}
                    onClick={(event) => {
                      if (!profileCeremonyAvailable) event.preventDefault();
                    }}
                    role="menuitem"
                    tabIndex={profileCeremonyAvailable ? undefined : -1}
                    title={profileCeremonyAvailable ? 'Open v4 activation ceremony' : 'Profile ceremony is unavailable'}
                    to={activationHref}
                  >
                    <CheckCircle2 aria-hidden="true" className="h-3.5 w-3.5" /> Set Active
                  </Link>
                )}
                <button
                  className={actionButtonClass()}
                  disabled={mutationDisabled}
                  onClick={() => onDuplicate(profile)}
                  role="menuitem"
                  type="button"
                >
                  <Copy aria-hidden="true" className="h-3.5 w-3.5" /> Duplicate
                </button>
                <div aria-hidden="true" className="my-1 border-t border-border" />
                <button
                  aria-label={`Delete ${displayName}`}
                  className={cn(actionButtonClass(), 'text-destructive hover:bg-destructive/10')}
                  disabled={isActive || mutationDisabled}
                  onClick={() => onDelete(profile)}
                  role="menuitem"
                  title={isActive ? 'Switch away before deleting this Profile' : 'Delete Profile'}
                  type="button"
                >
                  <Trash2 aria-hidden="true" className="h-3.5 w-3.5" /> Delete
                </button>
              </div>
            </PopoverContent>
          </Popover>
        </div>
      </div>

      <div className="flex min-w-0 flex-1 flex-col p-4">
        <Link
          className="min-w-0 rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
          to={browseHref}
          title={displayName}
        >
          <h3 className="line-clamp-2 min-h-10 break-words text-sm font-semibold leading-5 text-text-main" id={`profile-${profile.profile_id}-title`}>
            {displayName}
          </h3>
        </Link>
        <p className="mt-1 line-clamp-2 min-h-10 break-words text-xs leading-5 text-text-muted">
          {typeof profile.profile.description === 'string' && profile.profile.description.trim()
            ? profile.profile.description
            : 'Browse this Profile’s composition and review it before activation.'}
        </p>
        <p className="mt-3 flex min-w-0 items-center gap-1.5 text-xs text-text-muted">
          <Package aria-hidden="true" className="h-3 w-3 shrink-0" />
          <span className="shrink-0">{Array.isArray(profile.profile.packs) ? profileView.packIds.length : '--'} Packs</span>
          <span aria-hidden="true">·</span>
          <span className="truncate" title={profileView.basePackId ?? undefined}>{profileView.basePackId ?? 'Base Pack unavailable'}</span>
        </p>
        {(isBrowsing || profileView.status !== 'ready') && (
          <div className="my-3 flex min-w-0 flex-wrap content-start gap-1.5">
            {isBrowsing && <Badge variant="default">Selected browsing</Badge>}
            {profileView.status !== 'ready' && (
              <div
                aria-label={profileErrorDiagnostic ?? undefined}
                className="flex w-full items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/8 p-2.5 text-xs text-destructive"
                role="alert"
              >
                <AlertCircle aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span className="min-w-0 flex-1 line-clamp-2">
                  <span className="font-medium">{profileView.statusLabel}.</span>{' '}
                  {profileView.statusDescription}
                </span>
                {profileErrorDiagnostic ? (
                  <CopyErrorButton
                    label={`Copy ${displayName} Profile error`}
                    text={profileErrorDiagnostic}
                  />
                ) : null}
              </div>
            )}
          </div>
        )}

        {editing && (
          <form className="mt-2 space-y-2 border-t border-border pt-3" onSubmit={(event) => onSubmitEdit(event, profile)}>
            <label className="block text-xs font-medium text-text-main" htmlFor={`edit-profile-${profile.profile_id}`}>
              Display name
            </label>
            <input
              aria-label={`Display name for ${profile.profile_id}`}
              className="h-9 w-full rounded-lg border border-border bg-bg-main px-3 text-sm text-text-main outline-none focus:border-accent focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
              id={`edit-profile-${profile.profile_id}`}
              maxLength={120}
              name="display_name"
              onChange={(event) => onEditingNameChange(event.target.value)}
              required
              value={editingName}
            />
            <div className="flex flex-wrap justify-end gap-2">
              <Button onClick={onCancelEdit} size="sm" type="button" variant="ghost">Cancel</Button>
              <Button disabled={isBusy} size="sm" type="submit">Save</Button>
            </div>
          </form>
        )}

        <div className="mt-auto flex min-w-0 flex-col gap-3 pt-3">
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs">
            <Link
              aria-label={`Browse and review ${displayName}`}
              className="rounded px-1 py-1 text-text-muted underline-offset-2 hover:text-text-main hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
              to={browseHref}
            >
              Browse
            </Link>
            <Link
              aria-label={`View Pack closure for ${displayName}`}
              className="flex items-center gap-1 rounded px-1 py-1 text-text-muted underline-offset-2 hover:text-text-main hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
              to={closureHref}
            >
              <Package aria-hidden="true" className="h-3.5 w-3.5" /> Pack closure
            </Link>
          </div>
          <div className="flex min-w-0 items-center justify-between gap-2 border-t border-border pt-3">
            {profileView.status === 'ready' ? <Badge variant="success">Ready</Badge> : <span className="text-xs text-text-muted">Needs review</span>}
            <Button
              aria-label={`Launch ${displayName}`}
              disabled={launchDisabled}
              loading={isBusy && actionType === 'launch'}
              onClick={() => onLaunch(profile)}
              size="sm"
              title={launchBlockedReason ?? `Launch ${displayName}`}
              type="button"
            >
              <Rocket aria-hidden="true" className="h-3.5 w-3.5" />
              {isBusy && actionType === 'launch' ? 'Launching…' : 'Launch'}
            </Button>
          </div>
        </div>
      </div>
    </Card>
  );
}
