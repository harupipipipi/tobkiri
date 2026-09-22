import {useEffect, useMemo, useState} from 'react';
import {ListTree, PlayCircle, ShieldAlert} from 'lucide-react';

import {AdvancedSurfaceFrame, EmptySurfacePanel} from '@/src/components/advanced/AdvancedSurfaceFrame';
import {OperationInputForm} from '@/src/components/advanced/OperationInputForm';
import {OperationInvocationMetadata} from '@/src/components/advanced/OperationInvocationMetadata';
import {RuntimeEvidenceCard} from '@/src/components/advanced/RuntimeEvidenceCard';
import {Badge} from '@/src/components/ui/Badge';
import {Card, CardContent, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {useRuntimeOperationInvocation} from '@/src/hooks/useRuntimeOperationInvocation';
import {
  authoritativeOperationKey,
  selectAdvancedContractInvokableOperations,
  LAUNCHER_ADVANCED_VIEWS,
} from '@/src/lib/advancedSurfaces';
import {
  extractExactFlowDescriptors,
  extractExactOperationDescriptors,
  RUNTIME_CONTRACT_INVOKE_ACTION,
  type RuntimeFlowDescriptor,
  type RuntimeOperationDescriptor,
} from '@/src/lib/runtimeSurface';

export function exactFlowInvokableOperations(
  flows: RuntimeFlowDescriptor[] | null,
  operations: RuntimeOperationDescriptor[],
  authoritativeInvokableOperationKeys: ReadonlySet<string> = new Set(),
): RuntimeOperationDescriptor[] {
  if (!flows || flows.length === 0) return [];
  const declaredOperationIds = new Set(
    flows.filter((flow) => flow.state === 'ready').flatMap((flow) => flow.operation_ids),
  );
  return operations.filter((operation) => (
    operation.action === RUNTIME_CONTRACT_INVOKE_ACTION
      && operation.invokable
      && declaredOperationIds.has(operation.operation_id)
      && authoritativeInvokableOperationKeys.has(
        authoritativeOperationKey(operation.contract_id, operation.operation_id),
      )
  ));
}

export function Flow() {
  const surface = useRuntimeSurface<unknown>('operations');
  const descriptor = LAUNCHER_ADVANCED_VIEWS.flow;
  const flows = surface.data ? extractExactFlowDescriptors(surface.data.data) : null;
  const operations = surface.data ? extractExactOperationDescriptors(surface.data.data) : [];
  const operationIds = new Set(operations.map((operation) => operation.operation_id));
  const hasDeclaredCompositions = Boolean(flows && flows.length > 0);
  const declaredOperationIds = useMemo(
    () => (flows
      ? new Set(flows.filter((flow) => flow.state === 'ready').flatMap((flow) => flow.operation_ids))
      : undefined),
    [flows],
  );
  const invokableOperations = useMemo(
    () => selectAdvancedContractInvokableOperations(
      descriptor,
      {status: surface.status, stale: surface.stale, error: surface.error},
      surface.data,
      operations,
      declaredOperationIds,
    ),
    [descriptor, surface.status, surface.stale, surface.error, surface.data, operations, declaredOperationIds],
  );
  const [selectedOperationKey, setSelectedOperationKey] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedOperationKey || !invokableOperations.some((operation) => (
      authoritativeOperationKey(operation.contract_id, operation.operation_id) === selectedOperationKey
    ))) {
      const first = invokableOperations[0];
      setSelectedOperationKey(first ? authoritativeOperationKey(first.contract_id, first.operation_id) : null);
    }
  }, [selectedOperationKey, invokableOperations.map((operation) => authoritativeOperationKey(operation.contract_id, operation.operation_id)).join('\u0000')]);

  const selectedOperation = invokableOperations.find((operation) => (
    authoritativeOperationKey(operation.contract_id, operation.operation_id) === selectedOperationKey
  )) ?? null;
  const invocation = useRuntimeOperationInvocation(
    surface.data,
    selectedOperation,
  );
  const refreshFlow = async () => {
    await surface.refresh(true);
    await invocation.reconcileUnknown();
  };

  return (
    <AdvancedSurfaceFrame
      descriptor={descriptor}
      state={{status: surface.status, stale: surface.stale, error: surface.error}}
      onRetry={() => void refreshFlow()}
    >
      {surface.data ? <RuntimeEvidenceCard envelope={surface.data} title="Flow catalog provenance" /> : null}
      {surface.status === 'ready' && hasDeclaredCompositions ? (
        <div className="grid gap-5">
          {flows && flows.length > 0 ? (
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2"><ListTree className="h-4 w-4" aria-hidden="true" />Pack-declared compositions</CardTitle>
                <p className="text-sm leading-6 text-text-muted">Authoritative invokable_operations available for this surface: {invokableOperations.length}.</p>
              </CardHeader>
              <CardContent className="grid gap-3">
                {flows.map((flow) => (
                  <article key={flow.flow_id} className="rounded-lg border border-border bg-bg-main p-4">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="text-sm font-semibold text-text-main">{flow.label || flow.flow_id}</h2>
                      <Badge variant={flow.state === 'ready' ? 'success' : 'warning'}>{flow.state}</Badge>
                    </div>
                    <p className="mt-2 text-xs text-text-muted">Declared operations: {flow.operation_ids.length}</p>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {flow.operation_ids.map((operationId) => (
                        <Badge key={operationId} variant={operationIds.has(operationId) ? 'outline' : 'destructive'}>
                          {operationId}
                        </Badge>
                      ))}
                    </div>
                  </article>
                ))}
              </CardContent>
            </Card>
          ) : null}
          {selectedOperation ? (
            <Card>
              <CardHeader>
                <CardTitle>{selectedOperation.label || selectedOperation.operation_id}</CardTitle>
                <p className="text-sm leading-6 text-text-muted">Invoke an authoritative operation from the declared Flow composition. Inputs come only from its exact schema.</p>
              </CardHeader>
              <CardContent>
                <OperationInvocationMetadata operation={selectedOperation} />
                <div className="mb-4 flex flex-wrap gap-2">
                  {invokableOperations.map((operation) => {
                    const key = authoritativeOperationKey(operation.contract_id, operation.operation_id);
                    return (
                      <button
                        key={key}
                        type="button"
                        className="min-h-11 rounded-lg border border-border px-3 py-2 text-left text-xs text-text-main focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                        aria-pressed={key === selectedOperationKey}
                        aria-label={`Select contract operation ${operation.contract_id} / ${operation.operation_id}`}
                        disabled={invocation.busy}
                        onClick={() => {
                          if (invocation.busy) return;
                          setSelectedOperationKey(key);
                        }}
                      >
                        {operation.operation_id}
                      </button>
                    );
                  })}
                </div>
                {invocation.error ? (
                  <div className="mb-4 flex items-start gap-2 rounded-lg border border-amber-300/70 bg-amber-50/70 px-3 py-3 text-sm dark:border-amber-800/60 dark:bg-amber-950/20" role="alert">
                    <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden="true" />
                    {invocation.error.code}: {invocation.error.message}
                  </div>
                ) : null}
                {invocation.state === 'unknown' ? (
                  <div className="mb-4 rounded-lg border border-amber-300/70 bg-amber-50/70 px-3 py-3 text-sm text-amber-800 dark:border-amber-800/60 dark:bg-amber-950/20 dark:text-amber-200" role="alert">
                    The Flow operation result is unknown. Refresh the authoritative operations surface before trying again; no replacement request will be sent.
                  </div>
                ) : null}
                {invocation.state === 'succeeded' ? <p className="mb-4 text-sm text-emerald-700 dark:text-emerald-300" role="status">Flow operation accepted by the canonical Broker path.</p> : null}
                <OperationInputForm
                  operation={selectedOperation}
                  descriptor={descriptor}
                  busy={invocation.busy}
                  canInvoke={!invocation.error && invokableOperations.some((operation) => (
                    authoritativeOperationKey(operation.contract_id, operation.operation_id) === selectedOperationKey
                  ))}
                  onInvoke={invocation.invoke}
                />
              </CardContent>
            </Card>
          ) : (
            <EmptySurfacePanel
              icon={<PlayCircle className="size-6" />}
              title="No invokable operation binding is available"
              message="The operations surface has no fresh lifecycle/grant and catalog-bound invokable state for this Flow workspace."
            />
          )}
        </div>
      ) : (
        <EmptySurfacePanel
          icon={<PlayCircle className="size-6" />}
          title="No Pack-declared Flow composition is available"
          message="No authoritative Contract operation is available for this declared Flow composition. Pack inventory is never promoted into a wildcard Flow."
        />
      )}
    </AdvancedSurfaceFrame>
  );
}
