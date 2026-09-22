import {Cable, Link2} from 'lucide-react';
import {Link} from 'react-router';

import {AdvancedSurfaceFrame, EmptySurfacePanel} from '@/src/components/advanced/AdvancedSurfaceFrame';
import {RuntimeEvidenceCard} from '@/src/components/advanced/RuntimeEvidenceCard';
import {Badge} from '@/src/components/ui/Badge';
import {Card, CardContent, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {LAUNCHER_ADVANCED_VIEWS} from '@/src/lib/advancedSurfaces';
import {extractExactPlanBindings} from '@/src/lib/runtimeSurface';
import {panelRoutes} from '@/src/lib/routes';

export function ProfileWiring() {
  const surface = useRuntimeSurface<unknown>('profile');
  const descriptor = LAUNCHER_ADVANCED_VIEWS.profileWiring;
  const bindings = surface.data ? extractExactPlanBindings(surface.data.data) : null;

  return (
    <AdvancedSurfaceFrame
      descriptor={descriptor}
      state={{status: surface.status, stale: surface.stale, error: surface.error}}
      onRetry={() => void surface.refresh(true)}
    >
      {surface.data ? <RuntimeEvidenceCard envelope={surface.data} title="Profile wiring provenance" /> : null}
      {surface.status === 'ready' && bindings && bindings.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><Cable className="h-4 w-4" aria-hidden="true" />Exact Plan bindings</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-3">
            <Link
              to={panelRoutes.profile}
              className="min-h-11 self-start rounded-lg border border-border px-3 py-2 text-sm font-medium text-text-main hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
            >
              Change Profile closure in the v4 ceremony
            </Link>
            {bindings.map((binding) => (
              <div key={binding.binding_id} className="grid gap-2 rounded-lg border border-border bg-bg-main p-4 sm:grid-cols-[minmax(0,0.8fr)_minmax(0,1fr)_minmax(0,1fr)] sm:items-center">
                <div className="min-w-0">
                  <Badge variant="outline">{binding.binding_id}</Badge>
                  {binding.edge_digest ? <p className="mt-1 break-all font-mono text-xs text-text-muted">{binding.edge_digest}</p> : null}
                </div>
                <div className="min-w-0 text-xs text-text-muted"><span className="font-medium text-text-main">Principal</span><span className="ml-2 break-all font-mono">{binding.source_principal_id}</span></div>
                <div className="min-w-0 text-xs text-text-muted"><span className="font-medium text-text-main">Contract / operation</span><span className="ml-2 break-all font-mono">{binding.target_contract_id} / {binding.operation_id}</span></div>
              </div>
            ))}
          </CardContent>
        </Card>
      ) : (
        <EmptySurfacePanel
          icon={<Link2 className="size-6" />}
          title="Exact wiring is not published"
          message="The four inventory projections cannot supply wiring. This v4 operation is not provided until the accepted Profile projection contains complete ResolvedPlan binding identities. Profile closure changes remain available through the Profile ceremony when its exact operation is published."
        />
      )}
    </AdvancedSurfaceFrame>
  );
}
