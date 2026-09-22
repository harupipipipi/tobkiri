import {Map, Route as RouteIcon} from 'lucide-react';

import {AdvancedSurfaceFrame, EmptySurfacePanel} from '@/src/components/advanced/AdvancedSurfaceFrame';
import {RuntimeEvidenceCard} from '@/src/components/advanced/RuntimeEvidenceCard';
import {Badge} from '@/src/components/ui/Badge';
import {Card, CardContent, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {LAUNCHER_ADVANCED_VIEWS} from '@/src/lib/advancedSurfaces';
import {extractExactRouteDescriptors} from '@/src/lib/runtimeSurface';

export function ApiMap() {
  const surface = useRuntimeSurface<unknown>('contracts');
  const descriptor = LAUNCHER_ADVANCED_VIEWS.apiMap;
  const routes = surface.data ? extractExactRouteDescriptors(surface.data.data) : null;

  return (
    <AdvancedSurfaceFrame
      descriptor={descriptor}
      state={{status: surface.status, stale: surface.stale, error: surface.error}}
      onRetry={() => void surface.refresh(true)}
    >
      {surface.data ? <RuntimeEvidenceCard envelope={surface.data} title="Contract map provenance" /> : null}
      {surface.status === 'ready' && routes && routes.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><Map className="h-4 w-4" aria-hidden="true" />Declared API routes</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-3">
            {routes.map((route) => (
              <div key={route.route_id} className="grid gap-2 rounded-lg border border-border bg-bg-main p-4 lg:grid-cols-[minmax(0,0.6fr)_minmax(0,1.3fr)_minmax(0,1fr)_minmax(0,1fr)] lg:items-center">
                <Badge variant="outline">{route.method}</Badge>
                <p className="break-all font-mono text-xs text-text-main">{route.logical_target}</p>
                <div className="break-all text-xs text-text-muted">
                  <p><span className="font-medium text-text-main">Route</span> {route.route_id}</p>
                  <p><span className="font-medium text-text-main">Contract</span> {route.contract_id}</p>
                  <p className="mt-1"><span className="font-medium text-text-main">Operation</span> {route.operation_id}</p>
                  <p className="mt-1"><span className="font-medium text-text-main">Provider</span> {route.provider_id} · {route.function_id}</p>
                  <p className="mt-1"><span className="font-medium text-text-main">Function principal</span> {route.function_principal_id}</p>
                </div>
                <div className="break-all text-xs text-text-muted">
                  <p><span className="font-medium text-text-main">Presentation</span> {route.presentation}</p>
                  <p className="mt-1"><span className="font-medium text-text-main">Pack</span> {route.owner_pack_id}</p>
                  <p className="mt-1"><span className="font-medium text-text-main">Contribution</span> {route.contribution_id}</p>
                  <p className="mt-1"><span className="font-medium text-text-main">Payload</span> {route.allowed_payload_keys.join(', ') || 'none'}</p>
                  <p className="mt-1"><span className="font-medium text-text-main">Security</span> Broker {route.security.broker_authority_required ? 'required' : 'not required'} · CSRF {route.security.csrf_required ? 'required' : 'not required'} · Request ID {route.security.request_id_required ? 'required' : 'not required'} · Replay {route.security.replay_protection_required ? 'protected' : 'not required'}</p>
                  <p className="mt-1"><span className="font-medium text-text-main">Map digest</span> {route.frontend_map_digest}</p>
                  <p className="mt-1"><span className="font-medium text-text-main">Manifest digest</span> {route.manifest_digest}</p>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      ) : (
        <EmptySurfacePanel
          icon={<RouteIcon className="size-6" />}
          title="Exact route metadata is not available"
          message="The API Map waits for generated Contract Map route, operation, and security metadata. It never composes a route from a row id or calls a retired HTTP endpoint."
        />
      )}
    </AdvancedSurfaceFrame>
  );
}
