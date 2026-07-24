import { useState, useEffect, useRef, type KeyboardEvent } from 'react';
import { useAppStore, Theme, ColorMode, AVATAR_OPTIONS, UpdateTarget } from '@/src/store';
import { Avatar } from '@/src/components/ui/Avatar';
import { fetchBackgroundControlStatus, fetchDesktopSystemInfo, isDesktopShellAvailable, sendToBackground } from '@/src/lib/api';
import type { BackgroundControlStatus, DesktopPermissionStatus, DesktopSystemInfo } from '@/src/lib/apiTypes';
import { useT } from '@/src/lib/i18n';
import { cn } from '@/src/lib/utils';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/src/components/ui/Card';
import { Button } from '@/src/components/ui/Button';
import { Input } from '@/src/components/ui/Input';
import { Badge } from '@/src/components/ui/Badge';
import { Switch } from '@/src/components/ui/Switch';
import { User, Settings as SettingsIcon, Globe, Briefcase, Palette, Moon, Sun, LogIn, CheckCircle2, ChevronDown, RefreshCw, DownloadCloud, MonitorOff, ShieldCheck } from 'lucide-react';
import { LAUNCHER_DISPLAY_NAME, LAUNCHER_VERSION_LABEL, PRODUCT_DISPLAY_NAME } from '@/src/lib/launcherBrand';

function permissionBadgeVariant(permission: DesktopPermissionStatus): 'success' | 'warning' | 'destructive' | 'secondary' {
  if (permission.granted === true || permission.status === 'granted') return 'success';
  if (permission.granted === false || permission.status === 'missing') return 'destructive';
  if (permission.status === 'not_checked') return 'warning';
  return 'secondary';
}

function permissionStatusLabel(permission: DesktopPermissionStatus): string {
  if (permission.granted === true || permission.status === 'granted') return 'Granted';
  if (permission.granted === false || permission.status === 'missing') return 'Missing';
  if (permission.status === 'not_checked') return 'Manual check';
  if (permission.status === 'unsupported') return 'Unsupported';
  return permission.status || 'Unknown';
}

export function Settings() {
  const t = useT();
  const profile = useAppStore(state => state.profile);
  const updateProfile = useAppStore(state => state.updateProfile);
  const connectAccount = useAppStore(state => state.connectAccount);
  const loadProfile = useAppStore(state => state.loadProfile);
  const loadVersion = useAppStore(state => state.loadVersion);
  const version = useAppStore(state => state.version);
  const updates = useAppStore(state => state.updates);
  const autoUpdate = useAppStore(state => state.autoUpdate);
  const updatesLoading = useAppStore(state => state.updatesLoading);
  const updateSettingsLoading = useAppStore(state => state.updateSettingsLoading);
  const updateApplyingTarget = useAppStore(state => state.updateApplyingTarget);
  const loadUpdates = useAppStore(state => state.loadUpdates);
  const loadUpdateSettings = useAppStore(state => state.loadUpdateSettings);
  const setAutoUpdate = useAppStore(state => state.setAutoUpdate);
  const applyUpdate = useAppStore(state => state.applyUpdate);
  const theme = useAppStore(state => state.theme);
  const setTheme = useAppStore(state => state.setTheme);
  const colorMode = useAppStore(state => state.colorMode);
  const setColorMode = useAppStore(state => state.setColorMode);
  const addToast = useAppStore(state => state.addToast);

  const [activeTab, setActiveTab] = useState<'profile' | 'version'>('profile');
  const [formData, setFormData] = useState(profile);
  const [isConnecting, setIsConnecting] = useState(false);
  const [showAvatarPicker, setShowAvatarPicker] = useState(false);
  const [backgroundStatus, setBackgroundStatus] = useState<BackgroundControlStatus | null>(null);
  const [backgroundBusy, setBackgroundBusy] = useState(false);
  const [desktopInfo, setDesktopInfo] = useState<DesktopSystemInfo | null>(null);
  const [desktopInfoError, setDesktopInfoError] = useState<string | null>(null);
  const [desktopInfoBusy, setDesktopInfoBusy] = useState(false);
  const profileTabRef = useRef<HTMLButtonElement>(null);
  const versionTabRef = useRef<HTMLButtonElement>(null);
  const desktopShellAvailable = isDesktopShellAvailable();

  const loadDesktopInfo = async () => {
    if (!desktopShellAvailable) {
      setDesktopInfo(null);
      setDesktopInfoError(`macOS permission status is only available inside ${LAUNCHER_DISPLAY_NAME}.`);
      return;
    }
    setDesktopInfoBusy(true);
    setDesktopInfoError(null);
    try {
      const info = await fetchDesktopSystemInfo();
      if (!info) {
        setDesktopInfo(null);
        setDesktopInfoError(`${LAUNCHER_DISPLAY_NAME} permission bridge is unavailable. Reopen this page from ${LAUNCHER_DISPLAY_NAME} and try again.`);
        return;
      }
      setDesktopInfo(info);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to read macOS permissions';
      setDesktopInfoError(message);
      addToast(message, 'error');
    } finally {
      setDesktopInfoBusy(false);
    }
  };

  const loadBackgroundStatus = async () => {
    if (!desktopShellAvailable) {
      return;
    }
    setBackgroundBusy(true);
    try {
      setBackgroundStatus(await fetchBackgroundControlStatus());
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to read background status';
      addToast(message, 'error');
    } finally {
      setBackgroundBusy(false);
    }
  };

  const handleSendToBackground = async () => {
    setBackgroundBusy(true);
    try {
      await sendToBackground();
      setBackgroundStatus(await fetchBackgroundControlStatus());
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to send Rumi to background';
      addToast(message, 'error');
    } finally {
      setBackgroundBusy(false);
    }
  };

  useEffect(() => {
    loadProfile();
    loadVersion();
  }, [loadProfile, loadVersion]);

  useEffect(() => {
    if (activeTab === 'version') {
      loadUpdates();
      loadUpdateSettings();
      void loadBackgroundStatus();
      void loadDesktopInfo();
    }
  }, [activeTab, loadUpdates, loadUpdateSettings]);

  useEffect(() => {
    const refreshProfile = () => { void loadProfile(); };
    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') refreshProfile();
    };
    window.addEventListener('focus', refreshProfile);
    document.addEventListener('visibilitychange', refreshWhenVisible);
    return () => {
      window.removeEventListener('focus', refreshProfile);
      document.removeEventListener('visibilitychange', refreshWhenVisible);
    };
  }, [loadProfile]);

  useEffect(() => {
    setFormData(profile);
  }, [profile]);

  const handleSave = async () => {
    if (await updateProfile(formData)) addToast(t('settings.saved'), 'success');
  };

  const handleConnect = async () => {
    setIsConnecting(true);
    try {
      await connectAccount();
      addToast(t('settings.connect_started') || 'Browser opened. Finish signing in there, then return.', 'success');
    } catch {
      addToast(t('settings.connect_failed') || 'Failed to connect', 'error');
    } finally {
      setIsConnecting(false);
    }
  };

  const themes: Theme[] = ['Rounded', 'Minimal'];
  const updateName = (target: UpdateTarget) => target === 'tobkiri' ? PRODUCT_DISPLAY_NAME : 'defaultspack';
  const permissionRows = desktopInfo?.permissions ?? [];

  const handleApplyUpdate = async (target: UpdateTarget) => {
    await applyUpdate(target);
  };

  const handleTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    let nextTab: 'profile' | 'version' | null = null;
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
      nextTab = activeTab === 'profile' ? 'version' : 'profile';
    } else if (event.key === 'Home') {
      nextTab = 'profile';
    } else if (event.key === 'End') {
      nextTab = 'version';
    }
    if (!nextTab) return;
    event.preventDefault();
    setActiveTab(nextTab);
    (nextTab === 'profile' ? profileTabRef : versionTabRef).current?.focus();
  };

  return (
    <div className="flex-1 overflow-y-auto page-enter">
      <div className="mx-auto max-w-4xl px-6 py-8 flex flex-col gap-8">
        {/* Page header */}
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-text-main">{t('settings.title')}</h1>
          <p className="mt-1 text-sm text-text-muted">Manage your profile, appearance, and system updates.</p>
        </div>

        {/* Tab navigation */}
        <div className="flex gap-1 border-b border-border" role="tablist" aria-label={t('settings.title')}>
          <button
            ref={profileTabRef}
            id="settings-profile-tab"
            role="tab"
            aria-selected={activeTab === 'profile'}
            aria-controls="settings-profile-panel"
            tabIndex={activeTab === 'profile' ? 0 : -1}
            onClick={() => setActiveTab('profile')}
            onKeyDown={handleTabKeyDown}
            className={cn(
              "flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-main)]",
              activeTab === 'profile'
                ? "border-accent text-accent"
                : "border-transparent text-text-muted hover:text-text-main"
            )}
          >
            <User className="h-4 w-4" /> {t('settings.profile')}
          </button>
          <button
            ref={versionTabRef}
            id="settings-version-tab"
            role="tab"
            aria-selected={activeTab === 'version'}
            aria-controls="settings-version-panel"
            tabIndex={activeTab === 'version' ? 0 : -1}
            onClick={() => setActiveTab('version')}
            onKeyDown={handleTabKeyDown}
            className={cn(
              "flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-main)]",
              activeTab === 'version'
                ? "border-accent text-accent"
                : "border-transparent text-text-muted hover:text-text-main"
            )}
          >
            <SettingsIcon className="h-4 w-4" /> {t('settings.version_tab')}
          </button>
        </div>

        {/* Profile Tab */}
        {activeTab === 'profile' && (
          <div
            id="settings-profile-panel"
            role="tabpanel"
            aria-labelledby="settings-profile-tab"
            className="grid gap-6 lg:grid-cols-2"
          >
            {/* Left column */}
            <div className="flex flex-col gap-6">
              {/* Account connection */}
              <Card>
                <CardHeader>
                  <CardTitle>{t('settings.rumi_account')}</CardTitle>
                  <CardDescription>{t('settings.rumi_account_desc')}</CardDescription>
                </CardHeader>
                <CardContent>
                  {profile.connected ? (
                    <div className="flex items-center justify-between rounded-lg border border-border p-4">
                      <div className="flex items-center gap-3">
                        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-emerald-50 dark:bg-emerald-950/30">
                          <CheckCircle2 className="h-4 w-4 text-emerald-500" />
                        </div>
                        <div>
                          <p className="text-sm font-medium text-text-main">{profile.username}</p>
                          <p className="text-xs text-text-muted">{t('settings.connected')}</p>
                        </div>
                      </div>
                      <Button variant="outline" size="sm" onClick={handleConnect}>{t('settings.reconnect')}</Button>
                    </div>
                  ) : (
                    <div className="flex flex-col items-center gap-4 py-8">
                      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-bg-hover">
                        <LogIn className="h-5 w-5 text-text-muted" />
                      </div>
                      <p className="text-sm text-text-muted text-center">{t('settings.login_required')}</p>
                      <Button onClick={handleConnect} disabled={isConnecting} loading={isConnecting}>
                        {isConnecting ? t('settings.connecting') : t('settings.connect')}
                      </Button>
                    </div>
                  )}
                </CardContent>
              </Card>

              {/* Profile form (connected only) */}
              {profile.connected && (
                <Card>
                  <CardHeader>
                    <CardTitle>{t('settings.basic_info')}</CardTitle>
                    <CardDescription>{t('settings.basic_info_desc')}</CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-5">
                    {/* Avatar */}
                    <div className="space-y-2">
                      <span id="settings-avatar-label" className="text-sm font-medium text-text-main">{t('settings.select_icon')}</span>
                      <div className="flex items-center gap-4">
                        <Avatar src={formData.avatar} username={formData.username} className="h-14 w-14 text-lg" />
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => setShowAvatarPicker(!showAvatarPicker)}
                          aria-expanded={showAvatarPicker}
                          aria-controls="settings-avatar-options"
                        >
                          {t('settings.change_icon')}
                          <ChevronDown className={cn("h-3 w-3 transition-transform", showAvatarPicker && "rotate-180")} />
                        </Button>
                      </div>
                      {showAvatarPicker && (
                        <div
                          id="settings-avatar-options"
                          role="group"
                          aria-labelledby="settings-avatar-label"
                          className="flex gap-2 flex-wrap mt-2 p-3 border border-border rounded-lg bg-bg-main"
                        >
                          {AVATAR_OPTIONS.map((av, index) => (
                            <button
                              key={av}
                              onClick={() => {
                                setFormData({ ...formData, avatar: av });
                                setShowAvatarPicker(false);
                              }}
                              className={cn(
                                "rounded-full border-2 p-0.5 transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]",
                                formData.avatar === av
                                  ? "border-accent scale-105"
                                  : "border-transparent opacity-60 hover:opacity-100"
                              )}
                              aria-label={`${t('settings.select_icon')} ${index + 1}`}
                              aria-pressed={formData.avatar === av}
                            >
                              <Avatar src={av} username={formData.username} className="h-10 w-10 text-xs" />
                            </button>
                          ))}
                        </div>
                      )}
                    </div>

                    {/* Username */}
                    <div className="space-y-1.5">
                      <label htmlFor="settings-username" className="text-sm font-medium text-text-main">{t('settings.username')}</label>
                      <Input
                        id="settings-username"
                        value={formData.username}
                        onChange={(e) => setFormData({ ...formData, username: e.target.value })}
                      />
                    </div>

                    {/* Language */}
                    <div className="space-y-1.5">
                      <label htmlFor="settings-language" className="text-sm font-medium text-text-main flex items-center gap-2">
                        <Globe className="h-3.5 w-3.5 text-text-muted" /> {t('settings.language')}
                      </label>
                      <select
                        id="settings-language"
                        className="flex h-10 w-full rounded-lg border border-border bg-bg-main px-3 py-2 text-sm text-text-main transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                        value={formData.language}
                        onChange={(e) => setFormData({ ...formData, language: e.target.value })}
                      >
                        <option value="en">English</option>
                        <option value="ja">日本語</option>
                        <option value="zh">中文</option>
                        <option value="ko">한국어</option>
                        <option value="es">Español</option>
                        <option value="fr">Français</option>
                        <option value="de">Deutsch</option>
                        <option value="pt">Português</option>
                        <option value="ru">Русский</option>
                        <option value="ar">العربية</option>
                      </select>
                    </div>

                    {/* Job */}
                    <div className="space-y-1.5">
                      <label htmlFor="settings-job" className="text-sm font-medium text-text-main flex items-center gap-2">
                        <Briefcase className="h-3.5 w-3.5 text-text-muted" /> {t('settings.job')}
                      </label>
                      <Input
                        id="settings-job"
                        value={formData.job}
                        onChange={(e) => setFormData({ ...formData, job: e.target.value })}
                      />
                    </div>

                    {/* Save - right aligned */}
                    <div className="flex justify-end pt-2">
                      <Button onClick={handleSave}>{t('settings.save')}</Button>
                    </div>
                  </CardContent>
                </Card>
              )}
            </div>

            {/* Right column: Appearance */}
            <div className="flex flex-col gap-6">
              <Card>
                <CardHeader>
                  <CardTitle>{t('settings.theme')}</CardTitle>
                  <CardDescription>{t('settings.theme_desc')}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-6">
                  {/* Color mode */}
                  <div className="space-y-2">
                    <span id="settings-color-mode-label" className="text-sm font-medium text-text-main">{t('settings.color_mode')}</span>
                    <div role="group" aria-labelledby="settings-color-mode-label" className="grid grid-cols-2 gap-3">
                      <button
                        onClick={() => setColorMode('light')}
                        aria-pressed={colorMode === 'light'}
                        className={cn(
                          "flex items-center justify-center gap-2 rounded-lg border px-4 py-2.5 text-sm font-medium transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-main)]",
                          colorMode === 'light'
                            ? "border-accent bg-accent/5 text-accent"
                            : "border-border text-text-muted hover:border-text-muted/30"
                        )}
                      >
                        <Sun className="h-4 w-4" /> Light
                      </button>
                      <button
                        onClick={() => setColorMode('dark')}
                        aria-pressed={colorMode === 'dark'}
                        className={cn(
                          "flex items-center justify-center gap-2 rounded-lg border px-4 py-2.5 text-sm font-medium transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-main)]",
                          colorMode === 'dark'
                            ? "border-accent bg-accent/5 text-accent"
                            : "border-border text-text-muted hover:border-text-muted/30"
                        )}
                      >
                        <Moon className="h-4 w-4" /> Dark
                      </button>
                    </div>
                  </div>

                  {/* Style theme */}
                  <div className="space-y-2">
                    <span id="settings-style-theme-label" className="text-sm font-medium text-text-main">{t('settings.style_theme')}</span>
                    <div role="group" aria-labelledby="settings-style-theme-label" className="grid grid-cols-2 gap-3">
                      {themes.map((th) => (
                        <button
                          key={th}
                          onClick={() => setTheme(th)}
                          aria-pressed={theme === th}
                          className={cn(
                            "flex items-center justify-center gap-2 rounded-lg border px-4 py-2.5 text-sm font-medium transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-main)]",
                            theme === th
                              ? "border-accent bg-accent/5 text-accent"
                              : "border-border text-text-muted hover:border-text-muted/30"
                          )}
                        >
                          <Palette className="h-4 w-4" /> {th}
                        </button>
                      ))}
                    </div>
                  </div>
                </CardContent>
              </Card>
            </div>
          </div>
        )}

        {/* Version Tab */}
        {activeTab === 'version' && (
          <div
            id="settings-version-panel"
            role="tabpanel"
            aria-labelledby="settings-version-tab"
            className="flex flex-col gap-6"
          >
            {/* Version info */}
            <Card>
              <CardHeader>
                <CardTitle>{t('settings.version')}</CardTitle>
                <CardDescription>{t('settings.version_desc')}</CardDescription>
              </CardHeader>
              <CardContent>
                <div className="grid gap-3 sm:grid-cols-2">
                  {[
                    ['App Version', desktopInfo?.display_version ?? version.app],
                    [LAUNCHER_VERSION_LABEL, desktopInfo?.viewer_version ?? 'unknown'],
                    ['Kernel Version', version.kernel],
                    ['Python Version', version.python],
                    ['Platform', version.launcher],
                  ].map(([label, val]) => (
                    <div key={label} className="flex items-center justify-between rounded-lg border border-border p-3">
                      <span className="text-sm text-text-main">{label}</span>
                      <Badge variant="secondary">{val}</Badge>
                    </div>
                  ))}
                  <div className="flex items-center justify-between rounded-lg border border-border p-3">
                    <div className="flex flex-col">
                      <span className="text-sm text-text-main">Docker</span>
                      <span className="text-xs text-text-muted">{version.docker.type}</span>
                    </div>
                    <Badge variant={version.docker.installed ? 'secondary' : 'destructive'}>
                      {version.docker.installed ? version.docker.version : t('settings.not_installed')}
                    </Badge>
                  </div>
                </div>
              </CardContent>
            </Card>

            {desktopShellAvailable && (
              <Card>
                <CardHeader className="flex-row items-center justify-between space-y-0">
                  <div>
                    <CardTitle>Background Control</CardTitle>
                    <CardDescription>Keep the local Kernel available while the window is hidden.</CardDescription>
                  </div>
                  <Button variant="outline" size="sm" onClick={loadBackgroundStatus} disabled={backgroundBusy} loading={backgroundBusy}>
                    <RefreshCw className="h-3.5 w-3.5" />
                    Refresh
                  </Button>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="grid gap-3 sm:grid-cols-3">
                    <div className="flex items-center justify-between rounded-lg border border-border p-3">
                      <span className="text-sm text-text-main">Window</span>
                      <Badge variant={backgroundStatus?.app_visible ? 'secondary' : 'default'}>
                        {backgroundStatus ? (backgroundStatus.app_visible ? 'Visible' : 'Background') : 'Unknown'}
                      </Badge>
                    </div>
                    <div className="flex items-center justify-between rounded-lg border border-border p-3">
                      <span className="text-sm text-text-main">Kernel</span>
                      <Badge variant={backgroundStatus?.kernel_running ? 'secondary' : 'destructive'}>
                        {backgroundStatus ? (backgroundStatus.kernel_running ? 'Running' : 'Stopped') : 'Unknown'}
                      </Badge>
                    </div>
                    <div className="flex items-center justify-between rounded-lg border border-border p-3">
                      <span className="text-sm text-text-main">Control</span>
                      <Badge variant={backgroundStatus?.enabled === false ? 'destructive' : 'secondary'}>
                        {backgroundStatus ? (backgroundStatus.enabled === false ? 'Shutting down' : 'Ready') : 'Unknown'}
                      </Badge>
                    </div>
                  </div>
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <span className="text-xs text-text-muted">
                      Foreground: {backgroundStatus?.foreground_window ?? 'none'}
                    </span>
                    <Button onClick={handleSendToBackground} disabled={backgroundBusy || backgroundStatus?.enabled === false} loading={backgroundBusy}>
                      <MonitorOff className="h-3.5 w-3.5" />
                      Send to Background
                    </Button>
                  </div>
                </CardContent>
              </Card>
            )}

            {desktopShellAvailable && (
              <Card>
                <CardHeader className="flex-row items-center justify-between space-y-0">
                  <div>
                    <CardTitle>macOS Permissions</CardTitle>
                    <CardDescription>{LAUNCHER_DISPLAY_NAME} is the macOS permission host for Computer Use and screen capture.</CardDescription>
                  </div>
                  <Button variant="outline" size="sm" onClick={loadDesktopInfo} disabled={desktopInfoBusy} loading={desktopInfoBusy}>
                    <RefreshCw className="h-3.5 w-3.5" />
                    Refresh
                  </Button>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="rounded-lg border border-warning/40 bg-warning/10 p-4 text-xs leading-5 text-text-muted">
                    Tobkiri Launcher uses a new macOS app identity. You may need to grant its
                    permissions again in System Settings; legacy Rumi Viewer permissions are not copied.
                  </div>
                  <div className="rounded-lg border border-border bg-bg-main/50 p-4">
                    <p className="text-sm font-medium text-text-main">macOS権限ホスト: {desktopInfo?.permission_subject ?? LAUNCHER_DISPLAY_NAME}</p>
                    <p className="mt-2 text-xs leading-5 text-text-muted">
                      Tobkiriの画面確認・クリック・キーボード操作は、{LAUNCHER_DISPLAY_NAME}に許可された権限を使って実行されます。DefaultspackやCLIは、許可された操作だけをLauncher経由で要求します。
                    </p>
                    <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-text-muted">
                      <span className="rounded-full border border-border px-2.5 py-1">画面を見る</span>
                      <span className="rounded-full border border-border px-2.5 py-1">クリック・キーボード操作</span>
                      <span className="rounded-full border border-border px-2.5 py-1">ブラウザ操作</span>
                    </div>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    {permissionRows.map((permission) => (
                      <div key={permission.id} className="rounded-lg border border-border p-4">
                        <div className="flex items-start justify-between gap-3">
                          <div className="flex min-w-0 items-start gap-2">
                            <ShieldCheck className="mt-0.5 h-4 w-4 flex-shrink-0 text-text-muted" />
                            <div className="min-w-0">
                              <p className="text-sm font-medium text-text-main">{permission.label}</p>
                              <p className="mt-1 text-xs leading-5 text-text-muted">{permission.detail}</p>
                            </div>
                          </div>
                          <Badge variant={permissionBadgeVariant(permission)}>{permissionStatusLabel(permission)}</Badge>
                        </div>
                        {permission.settings_hint && (
                          <p className="mt-3 rounded-md bg-bg-hover px-3 py-2 text-xs text-text-muted">
                            {permission.settings_hint}
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                  {desktopInfoBusy && permissionRows.length === 0 && (
                    <p className="rounded-lg border border-border bg-bg-main/50 px-4 py-3 text-sm text-text-muted">
                      Reading macOS permission status from Tobkiri Launcher...
                    </p>
                  )}
                  {desktopInfoError && permissionRows.length === 0 && (
                    <p className="rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
                      {desktopInfoError}
                    </p>
                  )}
                  {!desktopInfoBusy && !desktopInfoError && desktopInfo && permissionRows.length === 0 && (
                    <p className="rounded-lg border border-border bg-bg-main/50 px-4 py-3 text-sm text-text-muted">
                      Tobkiri Launcher returned no macOS permission rows. Use Refresh after changing System Settings.
                    </p>
                  )}
                  {!desktopInfoBusy && !desktopInfoError && !desktopInfo && (
                    <p className="rounded-lg border border-border bg-bg-main/50 px-4 py-3 text-sm text-text-muted">
                      Click Refresh to read macOS permission status from Tobkiri Launcher.
                    </p>
                  )}
                </CardContent>
              </Card>
            )}

            {/* Updates */}
            <Card>
              <CardHeader className="flex-row items-center justify-between space-y-0">
                <div>
                  <CardTitle>{t('settings.updates')}</CardTitle>
                  <CardDescription>{t('settings.updates_desc')}</CardDescription>
                </div>
                <Button variant="outline" size="sm" onClick={() => loadUpdates()} disabled={updatesLoading} loading={updatesLoading}>
                  <RefreshCw className="h-3.5 w-3.5" />
                  {t('settings.check_updates')}
                </Button>
              </CardHeader>
              <CardContent className="space-y-3">
                {updates.map((update) => (
                  <div key={update.target} className="flex flex-col gap-4 rounded-lg border border-border p-4 sm:flex-row sm:items-center sm:justify-between">
                    <div className="flex min-w-0 flex-col gap-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium text-text-main">{updateName(update.target)}</span>
                        <Badge variant={update.updateAvailable ? 'default' : 'secondary'}>
                          {update.updateAvailable ? t('settings.update_available') : t('settings.up_to_date')}
                        </Badge>
                      </div>
                      <span className="text-xs text-text-muted">
                        {update.currentVersion} → {update.latestVersion}
                      </span>
                    </div>
                    <div className="flex items-center gap-4">
                      <label className="flex items-center gap-2 text-xs text-text-muted cursor-pointer">
                        <Switch
                          checked={autoUpdate[update.target]}
                          disabled={updateSettingsLoading}
                          onCheckedChange={(checked) => setAutoUpdate(update.target, checked)}
                        />
                        {t('settings.auto_update')}
                      </label>
                      <Button
                        variant={update.updateAvailable ? 'default' : 'outline'}
                        size="sm"
                        onClick={() => handleApplyUpdate(update.target)}
                        disabled={!update.updateAvailable || updateApplyingTarget !== null || updatesLoading}
                        loading={updateApplyingTarget === update.target}
                      >
                        <DownloadCloud className="h-3.5 w-3.5" />
                        {t('settings.apply_update')}
                      </Button>
                    </div>
                  </div>
                ))}
              </CardContent>
            </Card>
          </div>
        )}
      </div>
    </div>
  );
}
