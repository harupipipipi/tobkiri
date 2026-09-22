import {useEffect, useMemo, useRef, useState} from 'react';
import {Button} from '@/src/components/ui/Button';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {Input} from '@/src/components/ui/Input';
import {useAutoRefresh} from '@/src/hooks/useAutoRefresh';
import {fetchNamedProfiles, fetchProfileCompositionCatalog, updateNamedProfile} from '@/src/lib/hostClient';
import type {NamedProfileRecord, NamedProfileRegistry} from '@/src/lib/profileRegistry';
import {profilePackDependencies, type ProfileCompositionCatalog} from '@/src/lib/profileComposition';
import type {RuntimeProfileCatalogEntry} from '@/src/lib/runtimeSurface';

// Retain unsaved choices across in-app navigation. A draft never grants authority
// and is saved only against the exact definition from which it was edited.
const drafts = new Map<string, {revision: string; packIds: string[]}>();
const keyFor = (ids: string[]) => ids.slice().sort().join(',');
function selectedIds(record: NamedProfileRecord): string[] {
  if (!Array.isArray(record.profile.packs)) throw new Error('This Profile has no editable Pack definition.');
  return record.profile.packs.filter((row) => row.role !== 'application').map((row) => row.pack_id);
}

/** Save a named definition. Activation and authority review are separate actions. */
export function ProfilePackEditor({entry, locked, onEditingChange, onSaved}: {
  entry: RuntimeProfileCatalogEntry;
  locked: boolean;
  onEditingChange: (editing: boolean) => void;
  onSaved: () => Promise<unknown>;
}) {
  const [registry, setRegistry] = useState<NamedProfileRegistry | null>(null);
  const [catalog, setCatalog] = useState<ProfileCompositionCatalog | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [baseline, setBaseline] = useState<string[]>([]);
  const [query, setQuery] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(true);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState('');
  const mounted = useRef(true);
  const savingRef = useRef(false);
  const dirty = keyFor(selected) !== keyFor(baseline);
  const record = registry?.profiles.find((profile) => profile.profile_id === entry.profile_id);
  const targetCurrent = record?.profile_revision === entry.definition.digest;

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    onEditingChange(dirty || saving);
    return () => onEditingChange(false);
  }, [dirty, saving, onEditingChange]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  useAutoRefresh(async (signal) => {
    try {
      const [nextRegistry, nextCatalog] = await Promise.all([fetchNamedProfiles(), fetchProfileCompositionCatalog()]);
      const next = nextRegistry.profiles.find((profile) => profile.profile_id === entry.profile_id);
      if (!next) throw new Error('This Profile was removed. Return to Home to choose another Profile.');
      if (signal.aborted) return false;
      const ids = selectedIds(next);
      setRegistry(nextRegistry);
      setCatalog(nextCatalog);
      const draft = drafts.get(entry.profile_id);
      const draftStale = Boolean(draft && draft.revision !== next.profile_revision);
      setBaseline(ids);
      setSelected(draft?.packIds ?? ids);
      setError(draftStale ? 'This Profile changed while you were editing. Discard the draft to reload its saved configuration.' : null);
      setStale(draftStale);
      return true;
    } catch (error) {
      if (!signal.aborted) {
        setError(error instanceof Error ? error.message : 'Pack choices could not be loaded.');
        setStale(true);
      }
      return false;
    }
  }, {paused: dirty || saving});

  const fixedIds = useMemo(() => entry.pack_closure
    .filter((pack) => ['base', 'shell', 'application'].includes(pack.role))
    .map((pack) => pack.pack_id), [entry]);
  const closure = useMemo(() => profilePackDependencies(catalog?.packs ?? [], [...fixedIds, ...selected]), [catalog, fixedIds, selected]);
  const choices = catalog?.packs.filter((pack) => !['base', 'shell', 'application'].includes(pack.kind)) ?? [];
  const visible = choices.filter((pack) => `${pack.display_name} ${pack.pack_id}`.toLowerCase().includes(query.toLowerCase()));

  const save = async () => {
    if (savingRef.current || !record || !registry || !catalog || !dirty || locked || stale || !targetCurrent) return;
    savingRef.current = true;
    setSaving(true);
    setError(null);
    try {
      const result = await updateNamedProfile({
        profile_id: record.profile_id,
        display_name: String(record.profile.display_name || record.profile_id),
        expected_profile_revision: record.profile_revision,
        expected_store_generation: registry.generation,
        composition: {
          pack_ids: selected,
          profile_catalog_digest: catalog.profile_catalog_digest,
          bundle_lock_digest: catalog.bundle_lock_digest,
        },
      });
      if (!mounted.current) return;
      if (result.action !== 'update' || result.changed_profile?.profile_id !== record.profile_id) {
        throw new Error('The save response did not match this Profile. Reload its saved definition before continuing.');
      }
      drafts.delete(record.profile_id);
      setRegistry(result);
      setBaseline(selectedIds(result.changed_profile));
      setSelected(selectedIds(result.changed_profile));
      setNotice('Pack selection saved. Review and activate it to use this configuration.');
      await onSaved();
    } catch (error) {
      if (mounted.current) {
        setError(error instanceof Error ? error.message : 'Pack selection could not be saved.');
        // A response may be lost after persistence. Do not automatically replay
        // a mutation; retain the draft and require an explicit reload.
        setStale(true);
      }
    } finally {
      savingRef.current = false;
      if (mounted.current) setSaving(false);
    }
  };

  return (
    <section id="profile-packs" className="scroll-mt-6 border border-border bg-bg-card p-5" aria-label={`Edit Packs for ${entry.display_name}`}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-text-main">Packs</h2>
          <p className="mt-1 text-sm text-text-muted">Choose the Packs in {entry.display_name}. Saving leaves the running configuration as it is.</p>
        </div>
        <span className="text-sm text-text-muted">{selected.length} selected</span>
      </div>
      <Input className="mt-4" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search available Packs" aria-label="Search available Packs" />
      {error ? <div role="alert" className="mt-4 flex items-start gap-3 text-sm text-destructive"><span className="min-w-0 flex-1">{error}</span><CopyErrorButton text={error} label="Copy Pack selection error" visibleLabel="Copy error" /></div> : null}
      {!catalog ? <p className="mt-4 text-sm text-text-muted" role="status">Loading Pack choices…</p> : (
        <div className="mt-4 max-h-[28rem] overflow-y-auto divide-y divide-border" role="group" aria-label="Profile Pack selection">
          {visible.map((pack) => {
            const included = selected.includes(pack.pack_id);
            const dependency = !included && closure.has(pack.pack_id);
            return (
              <label key={pack.pack_id} className="flex cursor-pointer items-start gap-3 py-3">
                <input type="checkbox" className="mt-1 size-4 accent-accent" checked={included} disabled={saving || locked || stale || !targetCurrent}
                  onChange={() => {
                    if (!record) return;
                    setNotice('');
                    const next = included ? selected.filter((id) => id !== pack.pack_id) : [...selected, pack.pack_id];
                    if (keyFor(next) === keyFor(baseline)) drafts.delete(entry.profile_id);
                    else drafts.set(entry.profile_id, {revision: record.profile_revision, packIds: next});
                    setSelected(next);
                  }} />
                <span className="min-w-0 flex-1"><span className="block text-sm font-medium text-text-main">{pack.display_name}</span><span className="block break-all text-xs text-text-muted">{pack.pack_id} · {pack.version}</span></span>
                {dependency ? <span className="text-xs text-text-muted">Included as a dependency</span> : null}
              </label>
            );
          })}
          {visible.length === 0 ? <p className="py-4 text-sm text-text-muted">No matching Packs.</p> : null}
        </div>
      )}
      <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-border pt-4">
        <Button type="button" onClick={() => void save()} disabled={!dirty || !selected.length || saving || locked || stale || !targetCurrent}>{saving ? 'Saving…' : 'Save Pack selection'}</Button>
        {dirty ? <Button variant="ghost" disabled={saving} onClick={() => {drafts.delete(entry.profile_id); setSelected(baseline); setNotice('');}}>Discard changes{stale ? ' and reload' : ''}</Button> : null}
        {dirty ? <span role="status" className="text-sm text-text-muted">{selected.length ? 'Unsaved changes' : 'Select at least one Pack.'}</span> : null}
        {notice ? <p role="status" className="text-sm text-text-muted">{notice}</p> : null}
        {record && !targetCurrent ? <p role="status" className="text-sm text-text-muted">Waiting for the latest Profile definition…</p> : null}
      </div>
    </section>
  );
}
