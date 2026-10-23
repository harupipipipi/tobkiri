import {type FormEvent, useState} from 'react';
import {Check} from 'lucide-react';

import {Avatar} from '@/src/components/ui/Avatar';
import {Button} from '@/src/components/ui/Button';
import {Input} from '@/src/components/ui/Input';
import {useT} from '@/src/lib/i18n';
import {AVATAR_OPTIONS, useAppStore} from '@/src/store';

/** Personal presentation settings; independent of runtime Profiles and Packs. */
export function Account() {
  const t = useT();
  const profile = useAppStore((state) => state.profile);
  const updateLocalProfile = useAppStore((state) => state.updateLocalProfile);
  const addToast = useAppStore((state) => state.addToast);
  const [username, setUsername] = useState(profile.username);
  const [job, setJob] = useState(profile.job);
  const [avatar, setAvatar] = useState(profile.avatar);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const nextUsername = username.trim().slice(0, 80) || t('profile.default_username');
    updateLocalProfile({username: nextUsername, job: job.slice(0, 120), avatar});
    setUsername(nextUsername);
    addToast(t('profile.saved_personal_profile'), 'success');
  };

  return (
    <div className="flex-1 overflow-y-auto px-6 py-8 lg:px-10">
      <div className="mx-auto max-w-2xl">
        <h1 className="text-2xl font-semibold text-text-main">{t('nav.account')}</h1>
        <p className="mb-8 mt-2 text-sm text-text-muted">{t('account.description')}</p>
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
      </div>
    </div>
  );
}
