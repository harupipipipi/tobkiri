import {useState} from 'react';
import {FileCheck2, Fingerprint} from 'lucide-react';

import {AdvancedSurfaceFrame, EmptySurfacePanel} from '@/src/components/advanced/AdvancedSurfaceFrame';
import {RuntimeEvidenceCard} from '@/src/components/advanced/RuntimeEvidenceCard';
import {Badge} from '@/src/components/ui/Badge';
import {Card, CardContent, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {Input} from '@/src/components/ui/Input';
import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {LAUNCHER_ADVANCED_VIEWS} from '@/src/lib/advancedSurfaces';
import {extractFiniteArtifactEntries, type RuntimeArtifactEntry} from '@/src/lib/runtimeSurface';

export function filterArtifactEntries(entries: readonly RuntimeArtifactEntry[], query: string): RuntimeArtifactEntry[] {
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized) return [...entries];
  return entries.filter((entry) => [
    entry.entry_id,
    entry.kind,
    entry.owner_pack_id,
    entry.path,
    entry.artifact_digest,
  ].some((value) => value.toLocaleLowerCase().includes(normalized)));
}

export function ProfileFiles() {
  const surface = useRuntimeSurface<unknown>('profile');
  const descriptor = LAUNCHER_ADVANCED_VIEWS.profileFiles;
  const artifactEntries = surface.data ? extractFiniteArtifactEntries(surface.data.data) : null;
  const [query, setQuery] = useState('');
  const visibleEntries = artifactEntries ? filterArtifactEntries(artifactEntries, query) : [];

  return (
    <AdvancedSurfaceFrame
      descriptor={descriptor}
      state={{status: surface.status, stale: surface.stale, error: surface.error}}
      onRetry={() => void surface.refresh(true)}
    >
      {surface.data ? <RuntimeEvidenceCard envelope={surface.data} title="Record digests / activation evidence" /> : null}
      {surface.status === 'ready' && artifactEntries && artifactEntries.length > 0 ? (
        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="flex items-center gap-2"><FileCheck2 className="h-4 w-4" aria-hidden="true" />Profile artifacts</CardTitle>
              <Badge variant="outline">{artifactEntries.length} records</Badge>
            </div>
          </CardHeader>
          <CardContent>
            <Input label="Find an artifact" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="File, Pack, kind, or digest" />
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
            {visibleEntries.map((entry) => (
              <div key={entry.entry_id} className="min-w-0 rounded-lg border border-border bg-bg-main p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="outline">{entry.kind}</Badge>
                  <span className="break-all text-xs font-medium text-text-main">{entry.entry_id}</span>
                </div>
                <p className="mt-2 break-all font-mono text-xs text-text-muted">{entry.owner_pack_id} / {entry.path}</p>
                <p className="mt-1 break-all font-mono text-xs text-text-muted">{entry.artifact_digest}</p>
              </div>
            ))}
            </div>
            {visibleEntries.length === 0 ? <p className="mt-3 rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-text-muted">No artifacts match “{query.trim()}”.</p> : null}
          </CardContent>
        </Card>
      ) : (
        <EmptySurfacePanel
          icon={<Fingerprint className="size-6" />}
          title="No finite evidence entries are available"
          message="This surface does not browse profile.yaml, a database, or host files. It will expose only backend-declared record digests and finite artifact evidence."
        />
      )}
    </AdvancedSurfaceFrame>
  );
}
