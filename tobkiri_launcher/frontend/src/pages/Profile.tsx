import {FormEvent, useEffect, useState} from 'react';
import {Check, UserRound} from 'lucide-react';
import {useSearchParams} from 'react-router';

import {AdvancedSurfaceFrame} from '@/src/components/advanced/AdvancedSurfaceFrame';
import {ProfileCatalogSelector} from '@/src/components/advanced/ProfileCatalogSelector';
import {RuntimeEvidenceCard} from '@/src/components/advanced/RuntimeEvidenceCard';
import {Avatar} from '@/src/components/ui/Avatar';
import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {Card, CardContent, CardDescription, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {Input} from '@/src/components/ui/Input';
import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {LAUNCHER_ADVANCED_VIEWS} from '@/src/lib/advancedSurfaces';
import {useT} from '@/src/lib/i18n';
import type {RuntimeProfileCatalogProjection} from '@/src/lib/runtimeSurface';
import {resolveSetupVerificationState} from '@/src/lib/setupVerification';
import {AVATAR_OPTIONS, useAppStore} from '@/src/store';
import {
  clearRecoverableDraft,
  readRecoverableDraft,
  saveRecoverableDraft,
} from '@/src/lib/crashRecovery';

const PROFILE_DRAFT_ID = 'profile:launcher-local';

export function Profile() {
  const t = useT();
  const [searchParams, setSearchParams] = useSearchParams();
  const profile = useAppStore((state) => state.profile);
  const updateLocalProfile = useAppStore((state) => state.updateLocalProfile);
  const addToast = useAppStore((state) => state.addToast);
  const packs = useAppStore((state) => state.packs);
  const packsLoading = useAppStore((state) => state.packsLoading);
  const loadPacks = useAppStore((state) => state.loadPacks);
  const loadFrontendCatalog = useAppStore((state) => state.loadFrontendCatalog);
  const packVmDoctorReady = useAppStore((state) => state.packVmDoctor?.ready === true);
  const isSetupDone = useAppStore((state) => state.isSetupDone);
  const runtimeReady = useAppStore((state) => state.runtimeReady);
  const runtimeStatus = useAppStore((state) => state.runtimeStatus);
  const runtimeDisconnected = useAppStore((state) => state.runtimeDisconnected);
  const hostCatalogVerified = useAppStore((state) => state.hostCatalogVerified);
  const profileCeremonyAvailable = useAppStore((state) => state.profileCeremonyAvailable);
  const defaultsBootstrapRequired = useAppStore((state) => state.defaultsBootstrapRequired);
  const verificationState = resolveSetupVerificationState({
    isSetupDone,
    runtimeReady,
    runtimeStatus,
    runtimeDisconnected,
    hostCatalogVerified,
    profileCeremonyAvailable,
    defaultsBootstrapRequired,
  });
  const profileCeremonyVerified = verificationState === 'verified';
  const surface = useRuntimeSurface<unknown>('profile');
  const catalogSurface = useRuntimeSurface<RuntimeProfileCatalogProjection>('profiles');
  const descriptor = {
    ...LAUNCHER_ADVANCED_VIEWS.profile,
    label: t('profile.descriptor_label'),
    summary: t('profile.descriptor_summary'),
  };
  const [username, setUsername] = useState(profile.username);
  const [job, setJob] = useState(profile.job);
  const [avatar, setAvatar] = useState(profile.avatar);

  useEffect(() => {
    void loadPacks();
  }, [loadPacks]);

  useEffect(() => {
    if (packVmDoctorReady) void loadFrontendCatalog();
  }, [loadFrontendCatalog, packVmDoctorReady]);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const nextUsername = username.trim().slice(0, 80) || t('profile.default_username');
    updateLocalProfile({username: nextUsername, job: job.slice(0, 120), avatar});
    clearRecoverableDraft(PROFILE_DRAFT_ID);
    setUsername(nextUsername);
    addToast(t('profile.saved_personal_profile'), 'success');
  };

  const refreshAdvanced = async () => {
    await Promise.all([
      surface.refresh(true),
      catalogSurface.refresh(true),
      loadPacks(),
      loadFrontendCatalog(),
    ]);
  };

  return (
    <AdvancedSurfaceFrame
      descriptor={descriptor}
      state={{status: surface.status, stale: surface.stale, error: surface.error}}
      onRetry={() => void refreshAdvanced()}
    >
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.15fr)_minmax(18rem,0.85fr)]">
        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle>{t('profile.personal_title')}</CardTitle>
              <Badge variant="outline">{t('profile.this_device')}</Badge>
            </div>
            <CardDescription>{t('profile.personal_description')}</CardDescription>
          </CardHeader>
          <CardContent>
            <form className="flex flex-col gap-5" onSubmit={handleSubmit}>
              <div>
                <p className="text-sm font-medium text-text-main">{t('profile.avatar')}</p>
                <div className="mt-3 flex flex-wrap gap-3" role="group" aria-label={t('profile.choose_avatar')}>
                  {AVATAR_OPTIONS.map((option, index) => (
                    <button
                      key={option}
                      type="button"
                      className="relative min-h-11 min-w-11 rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2"
                      aria-label={t('profile.choose_avatar_number', {number: String(index + 1)})}
                      aria-pressed={avatar === option}
                      onClick={() => setAvatar(option)}
                    >
                      <Avatar src={option} username={username} className="h-11 w-11" />
                      {avatar === option ? (
                        <span className="absolute -right-1 -top-1 flex size-5 items-center justify-center rounded-full bg-accent text-accent-fg" aria-hidden="true">
                          <Check className="h-3 w-3" />
                        </span>
                      ) : null}
                    </button>
                  ))}
                </div>
              </div>
              <Input
                label={t('settings.username')}
                value={username}
                maxLength={80}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="nickname"
              />
              <Input
                label={t('profile.job_or_role')}
                value={job}
                maxLength={120}
                onChange={(event) => setJob(event.target.value)}
                autoComplete="organization-title"
              />
              <Button type="submit" className="min-h-11 self-start">
                {t('profile.save_personal_profile')}
              </Button>
            </form>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><UserRound className="h-4 w-4" aria-hidden="true" />{t('profile.runtime_evidence')}</CardTitle>
            <CardDescription>{t('profile.runtime_evidence_description')}</CardDescription>
          </CardHeader>
          <CardContent>
            {surface.data ? (
              <RuntimeEvidenceCard envelope={surface.data} title={t('profile.runtime_snapshot')} />
            ) : (
              <p className="rounded-lg border border-dashed border-border px-4 py-4 text-sm leading-6 text-text-muted">
                {t('profile.runtime_snapshot_unavailable')}
              </p>
            )}
          </CardContent>
        </Card>
      </div>
      <ProfileCatalogSelector
        profileSurface={surface}
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
    </AdvancedSurfaceFrame>
  );
}
