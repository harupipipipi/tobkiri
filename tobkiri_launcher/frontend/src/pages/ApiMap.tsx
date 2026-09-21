import {useMemo, useState} from 'react';
import {Map, Route as RouteIcon} from 'lucide-react';

import {AdvancedSurfaceFrame, EmptySurfacePanel} from '@/src/components/advanced/AdvancedSurfaceFrame';
import {RuntimeEvidenceCard} from '@/src/components/advanced/RuntimeEvidenceCard';
import {Badge} from '@/src/components/ui/Badge';
import {Card, CardContent, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {Input} from '@/src/components/ui/Input';
import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {LAUNCHER_ADVANCED_VIEWS} from '@/src/lib/advancedSurfaces';
import {extractExactRouteDescriptors, type RuntimeRouteDescriptor} from '@/src/lib/runtimeSurface';

export function filterRoutes(routes: readonly RuntimeRouteDescriptor[], query: string, method: string): RuntimeRouteDescriptor[] {
  const normalized = query.trim().toLocaleLowerCase();
  return routes.filter((route) => (
    (method === 'ALL' || route.method === method)
    && (!normalized || [
      route.route_id,
      route.method,
      route.logical_target,
      route.contract_id,
      route.operation_id,
      route.provider_id,
      route.function_id,
      route.owner_pack_id,
      route.presentation,
      ...route.allowed_payload_keys,
    ].some((value) => value.toLocaleLowerCase().includes(normalized)))
  ));
}

export function ApiMap() {
  const surface = useRuntimeSurface<unknown>('contracts');
  const descriptor = LAUNCHER_ADVANCED_VIEWS.apiMap;
  const routes = surface.data ? extractExactRouteDescriptors(surface.data.data) : null;
  const [query, setQuery] = useState('');
  const [method, setMethod] = useState('ALL');
  const methods = useMemo(() => routes ? [...new Set(routes.map((route) => route.method))].sort() : [], [routes]);
  const visibleRoutes = routes ? filterRoutes(routes, query, method) : [];

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
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="flex items-center gap-2"><Map className="h-4 w-4" aria-hidden="true" />API &amp; Route Map</CardTitle>
              <Badge variant="outline">{routes.length} routes</Badge>
            </div>
          </CardHeader>
          <CardContent className="grid gap-3">
            <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_10rem] sm:items-end">
              <Input label="Find a route" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Path, Contract, operation, provider, or Pack" />
              <label className="space-y-1.5 text-sm font-medium text-text-main">
                <span className="block">Method</span>
                <select className="h-10 w-full rounded-lg border border-border bg-bg-main px-3 text-sm text-text-main" value={method} onChange={(event) => setMethod(event.target.value)}>
                  <option value="ALL">All methods</option>
                  {methods.map((entry) => <option key={entry} value={entry}>{entry}</option>)}
                </select>
              </label>
            </div>
            <p className="text-xs text-text-muted">Showing {visibleRoutes.length} of {routes.length} routes</p>
            {visibleRoutes.map((route) => (
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
            {visibleRoutes.length === 0 ? <p className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-text-muted">No routes match the current filters.</p> : null}
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
