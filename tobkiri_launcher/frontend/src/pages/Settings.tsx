import {Check, Moon, Palette, Sun, Wrench} from 'lucide-react';

import {AdvancedSurfaceFrame} from '@/src/components/advanced/AdvancedSurfaceFrame';
import {RuntimeEvidenceCard} from '@/src/components/advanced/RuntimeEvidenceCard';
import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {Card, CardContent, CardDescription, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {Switch} from '@/src/components/ui/Switch';
import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {VALID_COLOR_MODES, VALID_THEMES} from '@/src/lib/appearance';
import {LAUNCHER_ADVANCED_VIEWS} from '@/src/lib/advancedSurfaces';
import {extractRuntimeProfileSettings} from '@/src/lib/runtimeSurface';
import {useAppStore} from '@/src/store';

export function Settings() {
  const theme = useAppStore((state) => state.theme);
  const colorMode = useAppStore((state) => state.colorMode);
  const language = useAppStore((state) => state.profile.language);
  const devtoolsEnabled = useAppStore((state) => state.devtoolsEnabled);
  const setTheme = useAppStore((state) => state.setTheme);
  const setColorMode = useAppStore((state) => state.setColorMode);
  const updateLocalProfile = useAppStore((state) => state.updateLocalProfile);
  const setDevtoolsEnabled = useAppStore((state) => state.setDevtoolsEnabled);
  const surface = useRuntimeSurface<unknown>('settings');
  const descriptor = LAUNCHER_ADVANCED_VIEWS.settings;
  const runtimeSettings = surface.data
    ? extractRuntimeProfileSettings(surface.data.data)
    : null;

  return (
    <AdvancedSurfaceFrame
      descriptor={descriptor}
      state={{status: surface.status, stale: surface.stale, error: surface.error}}
      onRetry={() => void surface.refresh(true)}
    >
      <div className="grid gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="flex items-center gap-2"><Palette className="h-4 w-4" aria-hidden="true" />Appearance</CardTitle>
              <Badge variant="warning">source: launcher_local</Badge>
            </div>
            <CardDescription>Theme and color mode are stored in Launcher localStorage.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-5">
            <div>
              <p className="text-sm font-medium text-text-main">Color mode</p>
              <div className="mt-3 grid gap-2 sm:grid-cols-2" role="group" aria-label="Color mode">
                {VALID_COLOR_MODES.map((mode) => (
                  <Button
                    key={mode}
                    type="button"
                    variant={colorMode === mode ? 'default' : 'outline'}
                    className="min-h-11 justify-start"
                    aria-pressed={colorMode === mode}
                    onClick={() => setColorMode(mode)}
                  >
                    {mode === 'dark' ? <Moon className="h-4 w-4" aria-hidden="true" /> : <Sun className="h-4 w-4" aria-hidden="true" />}
                    {mode === 'dark' ? 'Dark' : 'Light'}
                    {colorMode === mode ? <Check className="ml-auto h-4 w-4" aria-hidden="true" /> : null}
                  </Button>
                ))}
              </div>
            </div>
            <div>
              <p className="text-sm font-medium text-text-main">Style theme</p>
              <div className="mt-3 grid gap-2 sm:grid-cols-2" role="group" aria-label="Style theme">
                {VALID_THEMES.map((option) => (
                  <Button
                    key={option}
                    type="button"
                    variant={theme === option ? 'default' : 'outline'}
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
              Language
              <select
                className="min-h-11 rounded-lg border border-border bg-bg-main px-3 py-2 text-sm text-text-main focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                value={language}
                onChange={(event) => updateLocalProfile({language: event.target.value})}
                aria-label="Language"
              >
                <option value="en">English</option>
                <option value="ja">日本語</option>
              </select>
              <span className="text-xs font-normal text-text-muted">Stored locally; this does not change runtime Profile policy.</span>
            </label>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="flex items-center gap-2"><Wrench className="h-4 w-4" aria-hidden="true" />Devtools</CardTitle>
              <Badge variant="warning">source: launcher_local</Badge>
            </div>
            <CardDescription>
              Show diagnostic and raw Contract-operation tools in a separate navigation group.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="flex min-h-11 items-center justify-between gap-4 rounded-lg border border-border bg-bg-main px-4 py-3">
              <div className="min-w-0">
                <label htmlFor="devtools-visibility" className="text-sm font-medium text-text-main">
                  Show Devtools
                </label>
                <p id="devtools-visibility-description" className="mt-1 text-xs leading-5 text-text-muted">
                  Includes Graph, Flow, API &amp; Route Map, AI Input, Node Manager,
                  Profile Files, and Profile Wiring.
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
              This local presentation preference does not grant runtime authority,
              change the active Profile, alter Pack closure, or bypass Host approval.
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Runtime Profile settings</CardTitle>
            <CardDescription>Runtime settings are separate from user preferences and are not editable through this local panel.</CardDescription>
          </CardHeader>
          <CardContent>
            {surface.data ? (
              <div className="flex flex-col gap-4">
                <RuntimeEvidenceCard envelope={surface.data} title="Accepted runtime settings snapshot" />
                {runtimeSettings ? (
                  <dl className="grid gap-3 rounded-lg border border-border bg-bg-main p-4 sm:grid-cols-2">
                    <div><dt className="text-xs text-text-muted">Profile</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{runtimeSettings.profile_id}</dd></div>
                    <div><dt className="text-xs text-text-muted">Security epoch</dt><dd className="mt-1 font-mono text-xs text-text-main">{runtimeSettings.security_epoch}</dd></div>
                    <div><dt className="text-xs text-text-muted">Profile revision</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{runtimeSettings.profile_revision}</dd></div>
                    <div><dt className="text-xs text-text-muted">Plan digest</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{runtimeSettings.plan_digest}</dd></div>
                    <div><dt className="text-xs text-text-muted">Catalog revision</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{runtimeSettings.catalog_revision}</dd></div>
                    <div><dt className="text-xs text-text-muted">Lock digest</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{runtimeSettings.lock_digest}</dd></div>
                  </dl>
                ) : (
                  <p className="rounded-lg border border-dashed border-border px-4 py-4 text-sm text-text-muted">The runtime Profile settings record is not complete; no runtime value is shown or edited.</p>
                )}
              </div>
            ) : (
              <div className="rounded-lg border border-dashed border-border px-4 py-4 text-sm leading-6 text-text-muted">
                <p>Runtime Profile settings are unavailable from the current generated Contract Map.</p>
                <p className="mt-2">When a runtime value must change, use the canonical resolve → review → Kernel approval → activation ceremony. Launcher-local preferences are never merged into that state.</p>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </AdvancedSurfaceFrame>
  );
}
