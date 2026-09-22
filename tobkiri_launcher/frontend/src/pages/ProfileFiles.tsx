import {FileCheck2, Fingerprint} from 'lucide-react';

import {AdvancedSurfaceFrame, EmptySurfacePanel} from '@/src/components/advanced/AdvancedSurfaceFrame';
import {RuntimeEvidenceCard} from '@/src/components/advanced/RuntimeEvidenceCard';
import {Badge} from '@/src/components/ui/Badge';
import {Card, CardContent, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {LAUNCHER_ADVANCED_VIEWS} from '@/src/lib/advancedSurfaces';
import {extractFiniteArtifactEntries} from '@/src/lib/runtimeSurface';

export function ProfileFiles() {
  const surface = useRuntimeSurface<unknown>('profile');
  const descriptor = LAUNCHER_ADVANCED_VIEWS.profileFiles;
  const artifactEntries = surface.data ? extractFiniteArtifactEntries(surface.data.data) : null;

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
            <CardTitle className="flex items-center gap-2"><FileCheck2 className="h-4 w-4" aria-hidden="true" />Finite artifact evidence</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-3 sm:grid-cols-2">
            {artifactEntries.map((entry) => (
              <div key={entry.entry_id} className="min-w-0 rounded-lg border border-border bg-bg-main p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="outline">{entry.kind}</Badge>
                  <span className="break-all text-xs font-medium text-text-main">{entry.entry_id}</span>
                </div>
                <p className="mt-2 break-all font-mono text-xs text-text-muted">{entry.owner_pack_id} / {entry.path}</p>
                <p className="mt-1 break-all font-mono text-xs text-text-muted">{entry.artifact_digest}</p>
              </div>
            ))}
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
