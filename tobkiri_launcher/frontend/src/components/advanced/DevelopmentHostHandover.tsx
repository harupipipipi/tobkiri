import {useRef, useState} from 'react';
import {Button} from '@/src/components/ui/Button';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {handoverPreviousDevelopmentHost, recoverDevelopmentHostHandover, isDesktopShellAvailable} from '@/src/lib/desktopHost';
import {useT} from '@/src/lib/i18n';

export function DevelopmentHostHandover() {
  const t = useT();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [completed, setCompleted] = useState(false);
  const [restored, setRestored] = useState(false);
  const pending = useRef(false);
  const review = async (recover = false) => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = recover ? await recoverDevelopmentHostHandover() : await handoverPreviousDevelopmentHost();
      if (result !== null && typeof result === 'object' && 'stage' in result) {
        setCompleted(result.stage === 'completed');
        setRestored(result.stage === 'source-restored' || result.stage === 'intent-abandoned');
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      pending.current = false;
      setBusy(false);
    }
  };
  if (typeof window === 'undefined' || !isDesktopShellAvailable()) return null;
  return (
    <section className="rounded-lg border border-border bg-bg-main p-4">
      <h3 className="font-medium text-text-main">{t('settings.development_handover_title')}</h3>
      <p className="mt-2 text-sm text-text-muted">{t('settings.development_handover_description')}</p>
      <Button className="mt-3" variant="outline" loading={busy} disabled={completed || restored} onClick={() => void review()}>{t('settings.development_handover_review')}</Button>
      <Button className="mt-3 ml-2" variant="outline" disabled={busy} onClick={() => void review(true)}>{t('settings.development_handover_recovery')}</Button>
      {completed ? <p role="status" className="mt-2 text-sm">{t('settings.development_handover_completed')}</p> : null}
      {restored ? <p role="status" className="mt-2 text-sm">{t('settings.development_handover_restored')}</p> : null}
      {error ? <div role="alert" className="mt-2 text-sm"><p>{error}</p><CopyErrorButton text={error} label={t('settings.development_handover_copy_error')} /></div> : null}
    </section>
  );
}
