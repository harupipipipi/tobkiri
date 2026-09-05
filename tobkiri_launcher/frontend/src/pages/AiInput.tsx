import {useEffect, useState} from 'react';
import {BrainCircuit, ShieldAlert} from 'lucide-react';

import {AdvancedSurfaceFrame, EmptySurfacePanel} from '@/src/components/advanced/AdvancedSurfaceFrame';
import {OperationInputForm} from '@/src/components/advanced/OperationInputForm';
import {OperationInvocationMetadata} from '@/src/components/advanced/OperationInvocationMetadata';
import {RuntimeEvidenceCard} from '@/src/components/advanced/RuntimeEvidenceCard';
import {Badge} from '@/src/components/ui/Badge';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {Card, CardContent, CardDescription, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {useRuntimeOperationInvocation} from '@/src/hooks/useRuntimeOperationInvocation';
import {
  authoritativeOperationKey,
  selectAdvancedContractInvokableOperations,
  LAUNCHER_ADVANCED_VIEWS,
} from '@/src/lib/advancedSurfaces';
import {
  extractExactOperationDescriptors,
} from '@/src/lib/runtimeSurface';

export function AiInput() {
  const surface = useRuntimeSurface<unknown>('operations');
  const descriptor = LAUNCHER_ADVANCED_VIEWS.aiInput;
  const operations = surface.data ? extractExactOperationDescriptors(surface.data.data) : [];
  const invokableOperations = selectAdvancedContractInvokableOperations(
    descriptor,
    {status: surface.status, stale: surface.stale, error: surface.error},
    surface.data,
    operations,
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
  const refreshAiInput = async () => {
    await surface.refresh(true);
    await invocation.reconcileUnknown();
  };

  return (
    <AdvancedSurfaceFrame
      descriptor={descriptor}
      state={{status: surface.status, stale: surface.stale, error: surface.error}}
      onRetry={() => void refreshAiInput()}
    >
      {surface.data ? <RuntimeEvidenceCard envelope={surface.data} title="Operation catalog provenance" /> : null}
      {surface.status === 'ready' && invokableOperations.length > 0 && selectedOperation ? (
        <div className="grid gap-5 lg:grid-cols-[minmax(15rem,0.8fr)_minmax(0,1.2fr)]">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2"><BrainCircuit className="h-4 w-4" aria-hidden="true" />Invokable operations</CardTitle>
              <CardDescription>Only operations marked invokable and listed by authoritative Packs invokable_operations in the accepted snapshot can be selected.</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-2">
              {invokableOperations.map((operation) => {
                const key = authoritativeOperationKey(operation.contract_id, operation.operation_id);
                return (
                  <button
                    key={key}
                    type="button"
                    className="flex min-h-11 flex-col items-start gap-1 rounded-lg border border-border px-3 py-2 text-left transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                    aria-pressed={key === selectedOperationKey}
                    aria-label={`Select contract operation ${operation.contract_id} / ${operation.operation_id}`}
                    disabled={invocation.busy}
                    onClick={() => {
                      if (invocation.busy) return;
                      setSelectedOperationKey(key);
                    }}
                  >
                    <span className="text-sm font-medium text-text-main">{operation.label || operation.operation_id}</span>
                    <span className="break-all font-mono text-xs text-text-muted">{operation.contract_id}</span>
                  </button>
                );
              })}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <div className="flex flex-wrap items-center gap-2">
                <CardTitle>{selectedOperation.label || selectedOperation.operation_id}</CardTitle>
                <Badge variant="success">invokable</Badge>
              </div>
              <CardDescription>Input controls are generated from the declared operation schema. Invocation remains bound to the accepted Profile / Plan / catalog digests.</CardDescription>
            </CardHeader>
            <CardContent>
              <OperationInvocationMetadata operation={selectedOperation} />
              {invocation.error ? (
                <div className="mb-4 flex items-start gap-3 rounded-lg border border-amber-300/70 bg-amber-50/70 px-4 py-3 text-sm dark:border-amber-800/60 dark:bg-amber-950/20" role="alert">
                  <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden="true" />
                  <span className="min-w-0 flex-1 break-words">{invocation.error.code}: {invocation.error.message}</span>
                  <CopyErrorButton label="Copy AI Input operation error" text={`${invocation.error.code}: ${invocation.error.message}`} />
                </div>
              ) : null}
              {invocation.state === 'unknown' ? (
                <div className="mb-4 rounded-lg border border-amber-300/70 bg-amber-50/70 px-4 py-3 text-sm text-amber-800 dark:border-amber-800/60 dark:bg-amber-950/20 dark:text-amber-200" role="alert">
                  The AI Input operation result is unknown. Refresh the authoritative operations surface before trying again; no replacement request will be sent.
                </div>
              ) : null}
              {invocation.state === 'succeeded' ? <p className="mb-4 text-sm text-emerald-700 dark:text-emerald-300" role="status">Operation accepted by the canonical Broker path.</p> : null}
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
        </div>
      ) : (
        <EmptySurfacePanel
          icon={<BrainCircuit className="size-6" />}
          title="No invokable operation schema is available"
          message="AI Input does not infer prompts from Pack labels. A Pack must publish exact operation metadata, an input schema, a valid catalog digest, and an invokable binding before this form becomes active."
        />
      )}
    </AdvancedSurfaceFrame>
  );
}
