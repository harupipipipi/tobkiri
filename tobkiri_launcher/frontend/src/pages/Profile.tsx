import {useEffect} from 'react';
import {Link, useLocation, useSearchParams} from 'react-router';

import {ProfileCatalogSelector} from '@/src/components/advanced/ProfileCatalogSelector';
import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {useT} from '@/src/lib/i18n';
import {useProfileSelection} from '@/src/lib/profileSelection';
import {panelRoutes} from '@/src/lib/routes';
import type {RuntimeProfileCatalogProjection} from '@/src/lib/runtimeSurface';
import {useAppStore} from '@/src/store';

/** Inspect and configure a named execution Profile, never a personal account. */
export function Profile() {
  const t = useT();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const packs = useAppStore((state) => state.packs);
  const packsLoading = useAppStore((state) => state.packsLoading);
  const loadPacks = useAppStore((state) => state.loadPacks);
  const loadFrontendCatalog = useAppStore((state) => state.loadFrontendCatalog);
  const profileCeremonyAvailable = useAppStore((state) => state.profileCeremonyAvailable);
  const defaultsBootstrapRequired = useAppStore((state) => state.defaultsBootstrapRequired);
  const catalogSurface = useRuntimeSurface<RuntimeProfileCatalogProjection>('profiles');
  const profileCeremonyVerified = profileCeremonyAvailable && !defaultsBootstrapRequired;
  const {selectProfile} = useProfileSelection();
  const inspectedProfileId = searchParams.get('profile_id') ?? searchParams.get('profile');

  // The Profile page's inspected identity is the same panel-wide selection
  // Dev surfaces explain themselves against; selecting never activates.
  useEffect(() => {
    selectProfile(inspectedProfileId);
  }, [inspectedProfileId, selectProfile]);

  useEffect(() => {
    const anchor = location.hash.slice(1);
    if (['profile-packs', 'profile-closure', 'profile-ceremony'].includes(anchor)) {
      document.getElementById(anchor)?.scrollIntoView?.({block: 'start'});
    }
  }, [location.hash, catalogSurface.data]);

  return (
    <div className="flex-1 overflow-y-auto px-6 py-8 lg:px-10">
      <div className="mx-auto flex max-w-[1100px] flex-col gap-5">
        <Link className="text-sm text-text-muted hover:text-text-main" to={panelRoutes.home}>← {t('nav.home')}</Link>
        <h1 className="text-2xl font-semibold text-text-main">{t('nav.profile')}</h1>
        <nav
          aria-label="Profile workspace entries"
          className="flex flex-wrap items-center gap-3"
        >
          <Link
            className="inline-flex min-h-11 items-center rounded-lg border border-border bg-bg-card px-4 text-sm font-medium text-text-main transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
            to={inspectedProfileId
              ? `${panelRoutes.flow}?profile_id=${encodeURIComponent(inspectedProfileId)}`
              : panelRoutes.flow}
          >
            {t('nav.flow')} →
          </Link>
          <Link
            className="inline-flex min-h-11 items-center rounded-lg border border-border bg-bg-card px-4 text-sm font-medium text-text-muted transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
            to={panelRoutes.settings}
          >
            {t('nav.settings')}
          </Link>
          <span className="text-xs text-text-muted">
            {inspectedProfileId
              ? `The Flow editor keeps the inspected Profile identity (${inspectedProfileId}) in its route; runtime surfaces still bind the active execution Profile.`
              : 'Select a Profile to carry its identity into the Flow editor route.'}
          </span>
        </nav>
        <ProfileCatalogSelector
          compact
          // The verified catalog carries the current execution revision and Plan
          // as well as saved definitions. Activation must use that same capture.
          profileSurface={catalogSurface}
          catalogSurface={catalogSurface}
          packs={packs}
          packsLoading={packsLoading}
          loadPacks={loadPacks}
          initialSelectedProfileId={inspectedProfileId}
          onSelectedProfileId={(profileId) => {
            setSearchParams((current) => {
              const next = new URLSearchParams(current);
              next.set('profile_id', profileId);
              next.delete('profile');
              return next;
            }, {replace: true});
          }}
          runtimeVerified={profileCeremonyVerified}
          onActivated={async () => {
            await Promise.all([
              catalogSurface.refresh(true),
              loadFrontendCatalog(),
            ]);
          }}
        />

      </div>
    </div>
  );
}
