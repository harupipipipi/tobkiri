import {useEffect} from 'react';
import {Link, useLocation, useSearchParams} from 'react-router';

import {ProfileCatalogSelector} from '@/src/components/advanced/ProfileCatalogSelector';
import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {useT} from '@/src/lib/i18n';
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
        <ProfileCatalogSelector
          compact
          // The verified catalog carries the current execution revision and Plan
          // as well as saved definitions. Activation must use that same capture.
          profileSurface={catalogSurface}
          catalogSurface={catalogSurface}
          packs={packs}
          packsLoading={packsLoading}
          loadPacks={loadPacks}
          initialSelectedProfileId={searchParams.get('profile_id') ?? searchParams.get('profile')}
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
