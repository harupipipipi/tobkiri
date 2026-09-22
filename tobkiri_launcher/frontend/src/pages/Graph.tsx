import {ArrowRight, GitBranch} from 'lucide-react';
import {Link} from 'react-router';

import {AdvancedSurfaceFrame, EmptySurfacePanel} from '@/src/components/advanced/AdvancedSurfaceFrame';
import {RuntimeEvidenceCard} from '@/src/components/advanced/RuntimeEvidenceCard';
import {Badge} from '@/src/components/ui/Badge';
import {Card, CardContent, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {LAUNCHER_ADVANCED_VIEWS} from '@/src/lib/advancedSurfaces';
import {extractExactPlanBindings} from '@/src/lib/runtimeSurface';
import {panelRoutes} from '@/src/lib/routes';

export function Graph() {
  const surface = useRuntimeSurface<unknown>('profile');
  const descriptor = LAUNCHER_ADVANCED_VIEWS.graph;
  const bindings = surface.data ? extractExactPlanBindings(surface.data.data) : null;

  return (
    <AdvancedSurfaceFrame
      descriptor={descriptor}
      state={{status: surface.status, stale: surface.stale, error: surface.error}}
      onRetry={() => void surface.refresh(true)}
    >
      {surface.data ? <RuntimeEvidenceCard envelope={surface.data} title="Plan graph provenance" /> : null}
      {surface.status === 'ready' && bindings && bindings.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><GitBranch className="h-4 w-4" aria-hidden="true" />Read-only Plan binding graph</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-3">
            <Link
              to={panelRoutes.profile}
              className="min-h-11 self-start rounded-lg border border-border px-3 py-2 text-sm font-medium text-text-main hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
            >
              Change Profile closure in the v4 ceremony
            </Link>
            {bindings.map((binding) => (
              <div key={binding.binding_id} className="flex flex-col gap-3 rounded-lg border border-border bg-bg-main p-4 sm:flex-row sm:items-center">
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-medium uppercase tracking-wide text-text-muted">Function principal</p>
                  <p className="mt-1 break-all font-mono text-xs text-text-main">{binding.source_principal_id}</p>
                </div>
                <ArrowRight className="hidden h-4 w-4 shrink-0 text-text-muted sm:block" aria-hidden="true" />
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-medium uppercase tracking-wide text-text-muted">Contract / operation</p>
                  <p className="mt-1 break-all font-mono text-xs text-text-main">{binding.target_contract_id} / {binding.operation_id}</p>
                </div>
                <Badge variant="outline">{binding.binding_id}</Badge>
              </div>
            ))}
          </CardContent>
        </Card>
      ) : (
        <EmptySurfacePanel
          icon={<GitBranch className="size-6" />}
          title="Exact Plan bindings are not available"
          message="The generic inventory cannot produce graph edges. This v4 operation is not provided until the Profile projection returns complete exact binding identities; no graph save, compile, or editor operation is implied."
        />
      )}
    </AdvancedSurfaceFrame>
  );
}
