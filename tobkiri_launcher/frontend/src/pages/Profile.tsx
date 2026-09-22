import {FormEvent, useEffect, useState} from 'react';
import {Check, UserRound} from 'lucide-react';

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
import type {RuntimeProfileCatalogProjection} from '@/src/lib/runtimeSurface';
import {AVATAR_OPTIONS, useAppStore} from '@/src/store';

export function Profile() {
  const profile = useAppStore((state) => state.profile);
  const updateLocalProfile = useAppStore((state) => state.updateLocalProfile);
  const addToast = useAppStore((state) => state.addToast);
  const packs = useAppStore((state) => state.packs);
  const packsLoading = useAppStore((state) => state.packsLoading);
  const loadPacks = useAppStore((state) => state.loadPacks);
  const loadFrontendCatalog = useAppStore((state) => state.loadFrontendCatalog);
  const surface = useRuntimeSurface<unknown>('profile');
  const catalogSurface = useRuntimeSurface<RuntimeProfileCatalogProjection>('profiles');
  const descriptor = LAUNCHER_ADVANCED_VIEWS.profile;
  const [username, setUsername] = useState(profile.username);
  const [job, setJob] = useState(profile.job);
  const [avatar, setAvatar] = useState(profile.avatar);

  useEffect(() => {
    void loadPacks();
  }, [loadPacks]);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const nextUsername = username.trim().slice(0, 80) || 'User';
    updateLocalProfile({username: nextUsername, job: job.slice(0, 120), avatar});
    setUsername(nextUsername);
    addToast('Launcher profile saved locally.', 'success');
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
              <CardTitle>Launcher profile</CardTitle>
              <Badge variant="warning">source: launcher_local</Badge>
            </div>
            <CardDescription>These presentation preferences stay in this Launcher and are not runtime authority.</CardDescription>
          </CardHeader>
          <CardContent>
            <form className="flex flex-col gap-5" onSubmit={handleSubmit}>
              <div>
                <p className="text-sm font-medium text-text-main">Avatar</p>
                <div className="mt-3 flex flex-wrap gap-3" role="group" aria-label="Choose avatar">
                  {AVATAR_OPTIONS.map((option, index) => (
                    <button
                      key={option}
                      type="button"
                      className="relative min-h-11 min-w-11 rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2"
                      aria-label={`Choose avatar ${index + 1}`}
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
                label="Username"
                value={username}
                maxLength={80}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="nickname"
              />
              <Input
                label="Job or role"
                value={job}
                maxLength={120}
                onChange={(event) => setJob(event.target.value)}
                autoComplete="organization-title"
              />
              <Button type="submit" className="min-h-11 self-start">
                Save local profile
              </Button>
            </form>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><UserRound className="h-4 w-4" aria-hidden="true" />Runtime profile</CardTitle>
            <CardDescription>Canonical runtime state is evidence-bound. Runtime Pack closure changes use the staged v4 ceremony below.</CardDescription>
          </CardHeader>
          <CardContent>
            {surface.data ? (
              <RuntimeEvidenceCard envelope={surface.data} title="Accepted Profile snapshot" />
            ) : (
              <p className="rounded-lg border border-dashed border-border px-4 py-4 text-sm leading-6 text-text-muted">
                No canonical Profile snapshot is available from the Broker-backed Protocol v4 surface. Local profile editing remains available; no runtime mutation is attempted.
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
