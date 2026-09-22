import {RuntimeEvidenceCard} from '@/src/components/advanced/RuntimeEvidenceCard';
import {InlineLoadError} from '@/src/components/ui/InlineLoadError';
import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {extractRuntimeProfileSettings} from '@/src/lib/runtimeSurface';
import {useT} from '@/src/lib/i18n';

/** Load runtime diagnostics only when requested; local preferences work offline. */
export function RuntimeSettingsDetails() {
  const t = useT();
  const surface = useRuntimeSurface<unknown>('settings');
  const runtimeSettings = surface.data ? extractRuntimeProfileSettings(surface.data.data) : null;
  if (!surface.data && (surface.status === 'idle' || surface.status === 'loading')) {
    return <p className="mt-4 text-sm text-text-muted" role="status">{t('settings.loading_runtime')}</p>;
  }
  return <>
    {surface.error ? <InlineLoadError title={t('settings.runtime_settings_unavailable')} message={surface.error.message} onRetry={() => void surface.refresh(true)} retrying={surface.status === 'loading'} stale={surface.stale} /> : null}
              {surface.data ? (
                <div className="mt-4 flex flex-col gap-4">
                  <RuntimeEvidenceCard envelope={surface.data} title={t('settings.runtime_settings_snapshot')} />
                  {runtimeSettings ? (
                    <dl className="grid gap-3 rounded-lg border border-border bg-bg-card p-4 sm:grid-cols-2">
                      <div><dt className="text-xs text-text-muted">{t('settings.profile')}</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{runtimeSettings.profile_id}</dd></div>
                      <div><dt className="text-xs text-text-muted">{t('settings.security_epoch')}</dt><dd className="mt-1 font-mono text-xs text-text-main">{runtimeSettings.security_epoch}</dd></div>
                      <div><dt className="text-xs text-text-muted">{t('settings.profile_revision')}</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{runtimeSettings.profile_revision}</dd></div>
                      <div><dt className="text-xs text-text-muted">{t('settings.plan_digest')}</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{runtimeSettings.plan_digest}</dd></div>
                      <div><dt className="text-xs text-text-muted">{t('settings.catalog_revision')}</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{runtimeSettings.catalog_revision}</dd></div>
                      <div><dt className="text-xs text-text-muted">{t('settings.lock_digest')}</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{runtimeSettings.lock_digest}</dd></div>
                    </dl>
                  ) : (
                    <p className="rounded-lg border border-dashed border-border px-4 py-4 text-sm text-text-muted">{t('settings.runtime_settings_incomplete')}</p>
                  )}
                </div>
              ) : (
                <div className="mt-4 rounded-lg border border-dashed border-border px-4 py-4 text-sm leading-6 text-text-muted">
                <p>{t('settings.runtime_settings_unavailable')}</p>
                <p className="mt-2">{t('settings.runtime_settings_change_note')}</p>
                </div>
              )}
  </>;
}
