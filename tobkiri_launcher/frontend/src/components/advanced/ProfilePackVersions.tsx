import {useEffect, useRef, useState} from 'react';
import {Button} from '@/src/components/ui/Button';
import {Card, CardContent, CardDescription, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {fetchProfilePackVersions, selectProfilePackVersion, type ProfilePackVersions as VersionView} from '@/src/lib/profilePackVersions';
import {useT} from '@/src/lib/i18n';

export function ProfilePackVersions({profileId, definitionDigest, disabled, onSaved, onBusyChange}: {
  profileId: string; definitionDigest: string; disabled: boolean;
  onSaved: () => Promise<void>; onBusyChange: (busy: boolean) => void;
}) {
  const t = useT();
  const [view, setView] = useState<VersionView | null>(null);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const lifetime = useRef(0);
  const mutating = useRef(false);
  useEffect(() => {
    const epoch = ++lifetime.current;
    setView(null);
    setChoices({});
    setError(null);
    void fetchProfilePackVersions(profileId).then((result) => {
      if (lifetime.current !== epoch) return;
      setView(result);
      setChoices(Object.fromEntries(result.packs.map((pack) => [pack.pack_id, pack.selected_digest])));
    }).catch(() => {
      if (lifetime.current === epoch) setError('pack_versions.unavailable');
    });
    return () => { lifetime.current++; };
  }, [profileId, definitionDigest, refresh]);
  const save = async (packId: string) => {
    if (!view || disabled || mutating.current) return;
    const epoch = lifetime.current;
    mutating.current = true;
    setBusy(true);
    onBusyChange(true);
    setError(null);
    setMessage(null);
    try {
      const changed = await selectProfilePackVersion(view, packId, choices[packId]);
      if (lifetime.current === epoch) setMessage(changed ? 'pack_versions.saved' : 'pack_versions.unchanged');
      await onSaved();
    } catch {
      // A lost response may follow a committed successor. Refresh, never replay.
      if (lifetime.current === epoch) setError('pack_versions.save_failed');
      await onSaved();
    } finally {
      mutating.current = false;
      onBusyChange(false);
      if (lifetime.current === epoch) { setBusy(false); setRefresh((value) => value + 1); }
    }
  };
  return <Card>
    <CardHeader><CardTitle>{t('pack_versions.title')}</CardTitle><CardDescription>{t('pack_versions.description')}</CardDescription></CardHeader>
    <CardContent>
      {error ? <p role="alert" className="text-sm text-destructive">{t(error)}</p> : null}
      {message ? <p role="status" className="mb-3 text-sm">{t(message)}</p> : null}
      {!view && !error ? <p role="status">{t('pack_versions.loading')}</p> : null}
      {view?.packs.map((pack) => <div key={pack.pack_id} className="mb-3 flex flex-wrap items-end gap-3">
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-sm">{pack.pack_id}
          {pack.role === 'optional' ? <span className="text-xs text-text-secondary">{t('pack_versions.optional')}</span> : null}
          <select aria-label={t('pack_versions.choose', {pack: pack.pack_id})} value={choices[pack.pack_id] ?? pack.selected_digest}
            disabled={disabled || busy} className="rounded-md border border-border bg-bg-main px-3 py-2"
            onChange={(event) => setChoices((current) => ({...current, [pack.pack_id]: event.target.value}))}>
            {pack.versions.map((revision) => <option key={revision.artifact_digest} value={revision.artifact_digest}>
              {revision.version} · {revision.artifact_digest.slice(7, 19)} · {revision.publisher_id ?? t('pack_versions.bundled')}
            </option>)}
          </select>
        </label>
        <Button type="button" variant="outline" disabled={disabled || busy || !choices[pack.pack_id] || choices[pack.pack_id] === pack.selected_digest}
          onClick={() => void save(pack.pack_id)}>{t('pack_versions.save')}</Button>
      </div>)}
      {error ? <Button type="button" variant="outline" disabled={disabled || busy} onClick={() => setRefresh((value) => value + 1)}>{t('pack_versions.refresh')}</Button> : null}
    </CardContent>
  </Card>;
}
