import {Badge} from '@/src/components/ui/Badge';
import type {RuntimeOperationDescriptor} from '@/src/lib/runtimeSurface';

function declaredEffects(operation: RuntimeOperationDescriptor): string {
  const effects = operation.schema.effect_ceiling;
  if (!Array.isArray(effects) || effects.length === 0) return 'Provider-defined Contract effects';
  return effects.filter((effect): effect is string => typeof effect === 'string').join(', ')
    || 'Provider-defined Contract effects';
}

/** Show the authority identity that makes a contract_invoke action meaningful. */
export function OperationInvocationMetadata({
  operation,
}: {
  operation: RuntimeOperationDescriptor;
}) {
  return (
    <section className="mb-4 rounded-lg border border-amber-300/70 bg-amber-50/60 px-4 py-3 dark:border-amber-800/60 dark:bg-amber-950/20" aria-label="Contract invocation authorization metadata">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="warning">Action: contract_invoke</Badge>
        <Badge variant="warning">Host approval required</Badge>
      </div>
      <p className="mt-2 text-sm leading-6 text-text-muted">This operation may cause provider side effects. The accepted Contract, provider, function, and principal bindings are shown below; no client-supplied authority fields are accepted.</p>
      <dl className="mt-3 grid gap-3 text-xs sm:grid-cols-2 lg:grid-cols-3">
        <div className="min-w-0">
          <dt className="text-text-muted">Provider</dt>
          <dd className="mt-1 break-all font-mono text-text-main">{operation.provider_id ?? operation.target_provider_id}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-text-muted">Function</dt>
          <dd className="mt-1 break-all font-mono text-text-main">{operation.function_id}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-text-muted">Operation principal</dt>
          <dd className="mt-1 break-all font-mono text-text-main">{operation.function_principal_id}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-text-muted">Contract / operation</dt>
          <dd className="mt-1 break-all font-mono text-text-main">{operation.contract_id} / {operation.operation_id}</dd>
        </div>
        <div className="min-w-0 sm:col-span-2">
          <dt className="text-text-muted">Declared effect ceiling</dt>
          <dd className="mt-1 break-all font-mono text-text-main">{declaredEffects(operation)}</dd>
        </div>
      </dl>
    </section>
  );
}
