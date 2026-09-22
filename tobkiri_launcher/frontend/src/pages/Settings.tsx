import {useState} from 'react';
import {
  AlertCircle, Check, CheckCircle2, Download, ExternalLink, Moon, Palette, Sun,
  Wrench,
} from 'lucide-react';

import {RuntimeSettingsDetails} from '@/src/components/settings/RuntimeSettingsDetails';
import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {Card, CardContent, CardDescription, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {Switch} from '@/src/components/ui/Switch';
import {VALID_COLOR_MODES, VALID_THEMES} from '@/src/lib/appearance';
import {
  checkLauncherUpdate, isDesktopShellAvailable, openLauncherUpdateRelease,
} from '@/src/lib/api';
import type {LauncherUpdateStatus} from '@/src/lib/apiTypes';
import {useT} from '@/src/lib/i18n';
import {useAppStore} from '@/src/store';

export function Settings() {
  const t = useT();
  const theme = useAppStore((state) => state.theme);
  const colorMode = useAppStore((state) => state.colorMode);
  const language = useAppStore((state) => state.profile.language);
  const devtoolsEnabled = useAppStore((state) => state.devtoolsEnabled);
  const setTheme = useAppStore((state) => state.setTheme);
  const setColorMode = useAppStore((state) => state.setColorMode);
  const updateLocalProfile = useAppStore((state) => state.updateLocalProfile);
  const setDevtoolsEnabled = useAppStore((state) => state.setDevtoolsEnabled);
  const [launcherUpdate, setLauncherUpdate] = useState<LauncherUpdateStatus | null>(null);
  const [updateError, setUpdateError] = useState<string | null>(null);
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [openingRelease, setOpeningRelease] = useState(false);
  const [runtimeDetailsOpen, setRuntimeDetailsOpen] = useState(false);
  const desktopShell = typeof window !== 'undefined' && isDesktopShellAvailable();

  const checkUpdate = async () => {
    setCheckingUpdate(true);
    setUpdateError(null);
    try {
      setLauncherUpdate(await checkLauncherUpdate());
    } catch (error) {
      setLauncherUpdate(null);
      setUpdateError(error instanceof Error ? error.message : String(error));
    } finally {
      setCheckingUpdate(false);
    }
  };

  const openRelease = async () => {
    setOpeningRelease(true);
    setUpdateError(null);
    try {
      await openLauncherUpdateRelease();
    } catch (error) {
      setUpdateError(error instanceof Error ? error.message : String(error));
    } finally {
      setOpeningRelease(false);
    }
  };

  return (
    <div className="flex-1 overflow-y-auto page-enter">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 sm:p-6 lg:p-8">
        <h1 className="text-2xl font-semibold tracking-tight text-text-main">{t('settings.title')}</h1>
      <div className="grid items-start gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="flex items-center gap-2"><Palette className="h-4 w-4" aria-hidden="true" />{t('settings.appearance')}</CardTitle>
              <span className="text-xs text-text-muted">{t('settings.source_launcher_local')}</span>
            </div>
            <CardDescription>{t('settings.appearance_description')}</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-5">
            <div>
              <p className="text-sm font-medium text-text-main">{t('settings.color_mode')}</p>
              <div className="mt-3 grid gap-2 sm:grid-cols-2" role="group" aria-label={t('settings.color_mode')}>
                {VALID_COLOR_MODES.map((mode) => (
                  <Button
                    key={mode}
                    type="button"
                    variant={colorMode === mode ? 'secondary' : 'outline'}
                    className="min-h-11 justify-start"
                    aria-pressed={colorMode === mode}
                    onClick={() => setColorMode(mode)}
                  >
                    {mode === 'dark' ? <Moon className="h-4 w-4" aria-hidden="true" /> : <Sun className="h-4 w-4" aria-hidden="true" />}
                    {mode === 'dark' ? t('settings.dark') : t('settings.light')}
                    {colorMode === mode ? <Check className="ml-auto h-4 w-4" aria-hidden="true" /> : null}
                  </Button>
                ))}
              </div>
            </div>
            <div>
              <p className="text-sm font-medium text-text-main">{t('settings.style_theme')}</p>
              <div className="mt-3 grid gap-2 sm:grid-cols-2" role="group" aria-label={t('settings.style_theme')}>
                {VALID_THEMES.map((option) => (
                  <Button
                    key={option}
                    type="button"
                    variant={theme === option ? 'secondary' : 'outline'}
                    className="min-h-11 justify-start"
                    aria-pressed={theme === option}
                    onClick={() => setTheme(option)}
                  >
                    {option}
                    {theme === option ? <Check className="ml-auto h-4 w-4" aria-hidden="true" /> : null}
                  </Button>
                ))}
              </div>
            </div>
            <label className="flex flex-col gap-1.5 text-sm font-medium text-text-main">
              {t('settings.language')}
              <select
                className="min-h-11 rounded-lg border border-border bg-bg-main px-3 py-2 text-sm text-text-main focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                value={language}
                onChange={(event) => updateLocalProfile({language: event.target.value})}
                aria-label={t('settings.language')}
              >
                <option value="en">English</option>
                <option value="ja">日本語</option>
              </select>
              <span className="text-xs font-normal text-text-muted">{t('settings.language_storage_note')}</span>
            </label>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="flex items-center gap-2">
                <Download className="h-4 w-4" aria-hidden="true" />
                {t('settings.updates')}
              </CardTitle>
              {launcherUpdate ? (
                <Badge variant={launcherUpdate.state === 'available' ? 'warning' : 'success'}>
                  {launcherUpdate.state === 'available'
                    ? t('settings.update_available')
                    : t('settings.up_to_date')}
                </Badge>
              ) : null}
            </div>
            <CardDescription>{t('settings.updates_desc')}</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <p className="rounded-lg border border-border bg-bg-main px-4 py-3 text-xs leading-5 text-text-muted">
              {t('settings.update_bundle_note')}
            </p>

            {launcherUpdate ? (
              <div
                className="flex items-start gap-3 rounded-lg border border-border bg-bg-main px-4 py-3"
                role="status"
              >
                {launcherUpdate.state === 'available' ? (
                  <Download
                    className="mt-0.5 h-4 w-4 shrink-0 text-warning"
                    aria-hidden="true"
                    data-status-icon="update-available"
                  />
                ) : (
                  <CheckCircle2
                    className="mt-0.5 h-4 w-4 shrink-0 text-success"
                    aria-hidden="true"
                    data-status-icon="up-to-date"
                  />
                )}
                <dl className="grid min-w-0 flex-1 gap-x-5 gap-y-1 text-sm sm:grid-cols-2">
                  <div>
                    <dt className="text-xs text-text-muted">{t('settings.current_version')}</dt>
                    <dd className="font-mono text-text-main">{launcherUpdate.current_version}</dd>
                  </div>
                  {launcherUpdate.latest_version ? (
                    <div>
                      <dt className="text-xs text-text-muted">{t('settings.latest_version')}</dt>
                      <dd className="font-mono text-text-main">{launcherUpdate.latest_version}</dd>
                    </div>
                  ) : null}
                </dl>
              </div>
            ) : null}

            {updateError ? (
              <div
                className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
                role="alert"
              >
                <AlertCircle
                  aria-hidden="true"
                  className="mt-0.5 h-4 w-4 shrink-0"
                  data-error-icon="launcher-update"
                />
                <p className="min-w-0 flex-1 break-words">{updateError}</p>
                <CopyErrorButton label="Copy Launcher update error" text={updateError} />
              </div>
            ) : null}

            {!desktopShell ? (
              <p className="text-xs leading-5 text-text-muted">{t('settings.update_launcher_only')}</p>
            ) : null}

            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                disabled={!desktopShell || checkingUpdate || openingRelease}
                loading={checkingUpdate}
                onClick={() => void checkUpdate()}
              >
                {checkingUpdate ? t('settings.checking_updates') : t('settings.check_updates')}
              </Button>
              {launcherUpdate?.state === 'available' ? (
                <Button
                  type="button"
                  disabled={!desktopShell || checkingUpdate || openingRelease}
                  loading={openingRelease}
                  onClick={() => void openRelease()}
                >
                  <ExternalLink className="h-4 w-4" aria-hidden="true" />
                  {t('settings.open_release_page')}
                </Button>
              ) : null}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="flex items-center gap-2"><Wrench className="h-4 w-4" aria-hidden="true" />{t('settings.devtools')}</CardTitle>
              <span className="text-xs text-text-muted">{t('settings.source_launcher_local')}</span>
            </div>
            <CardDescription>{t('settings.devtools_description')}</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="flex min-h-11 items-center justify-between gap-4 rounded-lg border border-border bg-bg-main px-4 py-3">
              <div className="min-w-0">
                <label htmlFor="devtools-visibility" className="text-sm font-medium text-text-main">
                  {t('settings.show_devtools')}
                </label>
                <p id="devtools-visibility-description" className="mt-1 text-xs leading-5 text-text-muted">
                  {t('settings.devtools_includes')}
                </p>
              </div>
              <Switch
                id="devtools-visibility"
                checked={devtoolsEnabled}
                onCheckedChange={setDevtoolsEnabled}
                aria-describedby="devtools-visibility-description devtools-authority-note"
              />
            </div>
            <p id="devtools-authority-note" className="mt-3 text-xs leading-5 text-text-muted">
              {t('settings.devtools_authority_note')}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t('settings.runtime_profile_settings')}</CardTitle>
            <CardDescription>{t('settings.runtime_profile_settings_description')}</CardDescription>
          </CardHeader>
          <CardContent>
            <details
              className="rounded-lg border border-border bg-bg-main px-4 py-4"
              data-testid="runtime-settings-technical-details"
              onToggle={(event) => setRuntimeDetailsOpen(event.currentTarget.open)}
            >
              <summary className="cursor-pointer text-sm font-medium text-text-main focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]">
                {t('settings.technical_details')}
              </summary>
              {runtimeDetailsOpen ? <RuntimeSettingsDetails /> : null}
            </details>
          </CardContent>
        </Card>
      </div>
      </div>
    </div>
  );
}
