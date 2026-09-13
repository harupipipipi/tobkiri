import {useEffect, useMemo, useState} from 'react';
import {
  CheckCircle2,
  CircleAlert,
  Clock3,
  FilePenLine,
  ListTree,
  PlayCircle,
  RefreshCw,
  ShieldAlert,
} from 'lucide-react';

import {AdvancedSurfaceFrame, EmptySurfacePanel} from '@/src/components/advanced/AdvancedSurfaceFrame';
import {OperationInputForm} from '@/src/components/advanced/OperationInputForm';
import {OperationInvocationMetadata} from '@/src/components/advanced/OperationInvocationMetadata';
import {RuntimeEvidenceCard} from '@/src/components/advanced/RuntimeEvidenceCard';
import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {Card, CardContent, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {Input} from '@/src/components/ui/Input';
import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {useRuntimeOperationInvocation} from '@/src/hooks/useRuntimeOperationInvocation';
import {
  selectAdvancedContractInvokableOperations,
  LAUNCHER_ADVANCED_VIEWS,
} from '@/src/lib/advancedSurfaces';
import {
  extractExactFlowDescriptors,
  extractExactOperationDescriptors,
  type RuntimeFlowEdgeDescriptor,
  type RuntimeFlowDescriptor,
  type RuntimeOperationDescriptor,
} from '@/src/lib/runtimeSurface';
import {
  createWorkflowDefinition,
  deleteWorkflowDefinition,
  hasUnknownWorkflowMutation,
  loadWorkflowDefinitions,
  loadWorkflowPalette,
  parseWorkflowDefinitionText,
  publishWorkflowDefinition,
  reconcileUnknownWorkflowMutations,
  type WorkflowDefinition,
  type WorkflowAuthoringDependencies,
  type WorkflowPalette,
  type WorkflowValidation,
  updateWorkflowDefinition,
  validateWorkflowDefinition,
} from '@/src/lib/workflowAuthoring';
import {isMutationResultUnknown} from '@/src/lib/mutationJournal';

type FlowTab = 'authoring' | 'profile';

const EMPTY_WORKFLOW_DOCUMENT = JSON.stringify({
  workflow_api_version: 'io.tobkiri.workflow.v4',
  steps: [],
}, null, 2);

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'The Workflow authoring request failed.';
}

interface WorkflowAuthoringNoticeProps {
  kind: 'error' | 'unknown';
  message: string;
}

/** Keep authoring failure severity separate from the stable clipboard action. */
function WorkflowAuthoringNotice({kind, message}: WorkflowAuthoringNoticeProps) {
  const unknown = kind === 'unknown';
  const Icon = unknown ? Clock3 : CircleAlert;
  return (
    <div
      className={unknown
        ? 'flex items-start gap-2 rounded-lg border border-amber-300/70 bg-amber-50/70 px-3 py-3 text-sm text-amber-800 dark:border-amber-800/60 dark:bg-amber-950/20 dark:text-amber-200'
        : 'flex items-start gap-2 rounded-lg border border-destructive/50 bg-destructive/10 px-3 py-3 text-sm text-destructive'}
      role="alert"
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <span className="min-w-0 flex-1 break-words">{message}</span>
      <CopyErrorButton
        label={unknown ? 'Copy unknown Workflow mutation details' : 'Copy Workflow authoring error'}
        text={message}
      />
    </div>
  );
}

export interface WorkflowAuthoringPanelProps {
  /** Injectable only for deterministic authoring/recovery UI tests. */
  dependencies?: WorkflowAuthoringDependencies;
}

export function WorkflowAuthoringPanel({
  dependencies,
}: WorkflowAuthoringPanelProps) {
  const [definitions, setDefinitions] = useState<WorkflowDefinition[]>([]);
  const [palette, setPalette] = useState<WorkflowPalette | null>(null);
  const [definitionId, setDefinitionId] = useState('');
  const [documentText, setDocumentText] = useState(EMPTY_WORKFLOW_DOCUMENT);
  const [selectedDefinitionId, setSelectedDefinitionId] = useState<string | null>(null);
  const [validation, setValidation] = useState<WorkflowValidation | null>(null);
  const [notice, setNotice] = useState<WorkflowAuthoringNoticeProps | null>(null);
  const [loading, setLoading] = useState(false);
  const [mutating, setMutating] = useState(false);
  const [unknownMutationBlocked, setUnknownMutationBlocked] = useState(
    hasUnknownWorkflowMutation,
  );

  const selectedDefinition = definitions.find((item) => (
    item.definition_id === selectedDefinitionId
  )) ?? null;
  const mutationsBlocked = mutating || unknownMutationBlocked;

  const loadAuthoringState = async (): Promise<{
    definitions: WorkflowDefinition[];
    palette: WorkflowPalette;
  }> => {
    const [nextDefinitions, nextPalette] = await Promise.all([
      loadWorkflowDefinitions(dependencies),
      loadWorkflowPalette(dependencies),
    ]);
    return {definitions: nextDefinitions, palette: nextPalette};
  };

  const refresh = async () => {
    setLoading(true);
    try {
      let next = await loadAuthoringState();
      const recovered = await reconcileUnknownWorkflowMutations(async () => {
        next = await loadAuthoringState();
      }, dependencies);
      const unresolved = recovered.filter((item) => (
        item.state === 'pending'
          || item.state === 'indeterminate'
          || item.state === 'blocked'
      ));
      setUnknownMutationBlocked(unresolved.length > 0 || hasUnknownWorkflowMutation());
      const failed = recovered.find((item) => item.state === 'failed');
      if (unresolved.length > 0) {
        setNotice({
          kind: 'unknown',
          message: 'The original Workflow mutation is still unresolved. Reload checks the same request ID only; no replacement request will be sent.',
        });
      } else if (failed) {
        setNotice({
          kind: 'error',
          message: failed.safeErrorCode
            ? `The Host rejected the original Workflow mutation (${failed.safeErrorCode}).`
            : 'The Host rejected the original Workflow mutation.',
        });
      } else {
        setNotice(null);
      }
      setDefinitions(next.definitions);
      setPalette(next.palette);
      const refreshedSelection = selectedDefinitionId
        ? next.definitions.find((item) => item.definition_id === selectedDefinitionId)
        : null;
      if (refreshedSelection) {
        // Reload is explicit reconciliation, so it replaces the draft with the
        // current server value before its ETag may be used for another write.
        setDefinitionId(refreshedSelection.definition_id);
        setDocumentText(JSON.stringify(refreshedSelection.document, null, 2));
        setValidation(null);
      }
      setSelectedDefinitionId(refreshedSelection?.definition_id ?? null);
    } catch (error) {
      setUnknownMutationBlocked(hasUnknownWorkflowMutation());
      setNotice({kind: 'error', message: errorMessage(error)});
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  const selectDefinition = (definition: WorkflowDefinition) => {
    if (mutationsBlocked) return;
    setSelectedDefinitionId(definition.definition_id);
    setDefinitionId(definition.definition_id);
    setDocumentText(JSON.stringify(definition.document, null, 2));
    setValidation(null);
    setNotice(null);
  };

  const document = (): Record<string, unknown> | null => {
    const parsed = parseWorkflowDefinitionText(documentText);
    if (!parsed) {
      setNotice({
        kind: 'error',
        message: 'Workflow definitions must be JSON objects with workflow_api_version io.tobkiri.workflow.v4 and a steps array.',
      });
    }
    return parsed;
  };

  const validate = async () => {
    const parsed = document();
    if (!parsed) return;
    setLoading(true);
    setNotice(null);
    try {
      setValidation(await validateWorkflowDefinition(parsed, dependencies));
    } catch (error) {
      setNotice({kind: 'error', message: errorMessage(error)});
    } finally {
      setLoading(false);
    }
  };

  const save = async () => {
    const normalizedId = definitionId.trim();
    const parsed = document();
    if (!normalizedId) {
      setNotice({kind: 'error', message: 'A Workflow definition ID is required.'});
      return;
    }
    if (!parsed || mutationsBlocked) return;
    if (
      selectedDefinition?.definition_id === normalizedId
      && selectedDefinition.state !== 'draft'
    ) {
      setNotice({
        kind: 'error',
        message: 'Only a selected draft can be updated. Create a new definition ID instead.',
      });
      return;
    }
    setMutating(true);
    setNotice(null);
    try {
      const saved = selectedDefinition?.definition_id === normalizedId
        ? await updateWorkflowDefinition(
          normalizedId,
          parsed,
          selectedDefinition.etag,
          dependencies,
        )
        : await createWorkflowDefinition(normalizedId, parsed, dependencies);
      setDefinitions((current) => [
        ...current.filter((item) => item.definition_id !== saved.definition_id),
        saved,
      ].sort((left, right) => left.definition_id.localeCompare(right.definition_id)));
      setSelectedDefinitionId(saved.definition_id);
      setDocumentText(JSON.stringify(saved.document, null, 2));
      setValidation(null);
    } catch (error) {
      const unknown = isMutationResultUnknown(error);
      if (unknown) setUnknownMutationBlocked(true);
      setNotice({
        kind: unknown ? 'unknown' : 'error',
        message: errorMessage(error),
      });
    } finally {
      setMutating(false);
    }
  };

  const publish = async () => {
    if (!selectedDefinition || selectedDefinition.state !== 'draft' || mutationsBlocked) return;
    setMutating(true);
    setNotice(null);
    try {
      const published = await publishWorkflowDefinition(
        selectedDefinition.definition_id,
        selectedDefinition.etag,
        dependencies,
      );
      setDefinitions((current) => current.map((item) => (
        item.definition_id === published.definition_id ? published : item
      )));
    } catch (error) {
      const unknown = isMutationResultUnknown(error);
      if (unknown) setUnknownMutationBlocked(true);
      setNotice({
        kind: unknown ? 'unknown' : 'error',
        message: errorMessage(error),
      });
    } finally {
      setMutating(false);
    }
  };

  const remove = async () => {
    if (!selectedDefinition || selectedDefinition.state !== 'draft' || mutationsBlocked) return;
    setMutating(true);
    setNotice(null);
    try {
      await deleteWorkflowDefinition(
        selectedDefinition.definition_id,
        selectedDefinition.etag,
        dependencies,
      );
      setDefinitions((current) => current.filter((item) => (
        item.definition_id !== selectedDefinition.definition_id
      )));
      setSelectedDefinitionId(null);
      setDefinitionId('');
      setDocumentText(EMPTY_WORKFLOW_DOCUMENT);
      setValidation(null);
    } catch (error) {
      const unknown = isMutationResultUnknown(error);
      if (unknown) setUnknownMutationBlocked(true);
      setNotice({
        kind: unknown ? 'unknown' : 'error',
        message: errorMessage(error),
      });
    } finally {
      setMutating(false);
    }
  };

  return (
    <div className="grid gap-5">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><FilePenLine className="h-4 w-4" aria-hidden="true" />Workflow definitions (v4)</CardTitle>
          <p className="text-sm leading-6 text-text-muted">Author JSON definitions through currently Profile-admitted Workflow v4 actions. This surface does not use the retired Flow endpoint or ReactFlow.</p>
        </CardHeader>
        <CardContent className="grid gap-4">
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" variant="outline" size="sm" onClick={() => void refresh()} loading={loading}>
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />Reload definitions and palette
            </Button>
            {selectedDefinition ? <Badge variant={selectedDefinition.state === 'draft' ? 'warning' : 'success'}>{selectedDefinition.state} revision {selectedDefinition.revision}</Badge> : null}
          </div>
          {notice ? <WorkflowAuthoringNotice {...notice} /> : null}
          <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
            <Input
              label="Workflow definition ID"
              value={definitionId}
              disabled={mutationsBlocked}
              onChange={(event) => {
                setDefinitionId(event.target.value);
                setValidation(null);
              }}
              placeholder="daily-summary"
            />
            <Button type="button" variant="outline" size="sm" disabled={loading || mutationsBlocked} onClick={() => {
              setSelectedDefinitionId(null);
              setDefinitionId('');
              setDocumentText(EMPTY_WORKFLOW_DOCUMENT);
              setValidation(null);
              setNotice(null);
            }}>New draft</Button>
          </div>
          <div className="grid gap-1.5">
            <label className="text-sm font-medium text-text-main" htmlFor="workflow-definition-json">Workflow definition JSON</label>
            <textarea
              id="workflow-definition-json"
              className="min-h-72 w-full rounded-lg border border-border bg-bg-main px-3 py-2 font-mono text-xs text-text-main focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] disabled:cursor-not-allowed disabled:opacity-50"
              value={documentText}
              disabled={mutationsBlocked}
              onChange={(event) => {
                setDocumentText(event.target.value);
                setValidation(null);
              }}
              spellCheck={false}
            />
          </div>
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" disabled={loading || mutationsBlocked} onClick={() => void validate()}>Validate</Button>
            <Button type="button" disabled={mutationsBlocked || (selectedDefinition?.definition_id === definitionId.trim() && selectedDefinition.state !== 'draft')} loading={mutating} onClick={() => void save()}>{selectedDefinition?.definition_id === definitionId.trim() ? 'Save draft' : 'Create draft'}</Button>
            <Button type="button" variant="secondary" disabled={selectedDefinition?.state !== 'draft' || mutationsBlocked} onClick={() => void publish()}>Publish selected draft</Button>
            <Button type="button" variant="destructive" disabled={selectedDefinition?.state !== 'draft' || mutationsBlocked} onClick={() => void remove()}>Delete selected draft</Button>
          </div>
          {validation ? (
            validation.valid ? (
              <p className="flex items-center gap-2 text-sm text-emerald-700 dark:text-emerald-300" role="status"><CheckCircle2 className="h-4 w-4" aria-hidden="true" />The definition is valid against the active Workflow v4 palette.</p>
            ) : (
              <WorkflowAuthoringNotice kind="error" message={`Validation failed: ${validation.errors.join('; ') || 'no diagnostic was returned.'}`} />
            )
          ) : null}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Saved definitions</CardTitle>
          <p className="text-sm text-text-muted">Updates, publish, and delete use the selected server ETag; no stale-value override is offered.</p>
        </CardHeader>
        <CardContent className="grid gap-2">
          {definitions.length > 0 ? definitions.map((definition) => (
            <button
              key={definition.definition_id}
              type="button"
              className="flex min-h-11 items-center justify-between gap-3 rounded-lg border border-border bg-bg-main px-3 py-2 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] disabled:cursor-not-allowed"
              aria-pressed={definition.definition_id === selectedDefinitionId}
              disabled={mutationsBlocked}
              onClick={() => selectDefinition(definition)}
            >
              <span className="min-w-0 truncate">{definition.definition_id}</span>
              <Badge variant={definition.state === 'draft' ? 'warning' : 'outline'}>{definition.state} · r{definition.revision}</Badge>
            </button>
          )) : <p className="text-sm text-text-muted">No Workflow definitions are currently visible.</p>}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Active operation palette</CardTitle>
          <p className="text-sm text-text-muted">Read-only full identities from the active Workflow v4 catalog.</p>
        </CardHeader>
        <CardContent className="grid gap-2">
          {palette?.operations.length ? palette.operations.map((operation) => (
            <div key={[operation.function_principal_id, operation.provider_id, operation.contract_id, operation.operation_id].join('\u0000')} className="rounded-lg border border-border bg-bg-main p-3 text-xs text-text-muted">
              <p className="font-medium text-text-main">{operation.operation_id}</p>
              <p className="mt-1 break-all">{operation.function_principal_id} → {operation.provider_id} · {operation.contract_id}</p>
            </div>
          )) : <p className="text-sm text-text-muted">Reload to retrieve the active palette.</p>}
        </CardContent>
      </Card>
    </div>
  );
}

/** Return only the canonical compositions admitted by the active Profile. */
export function readyFlowCompositions(
  flows: RuntimeFlowDescriptor[] | null,
): RuntimeFlowDescriptor[] {
  return flows?.filter((flow) => flow.state === 'ready') ?? [];
}

/** Compare all four fields so a shared operation ID cannot cross a Flow edge. */
export function operationMatchesFlowEdge(
  operation: RuntimeFlowEdgeDescriptor,
  edge: RuntimeFlowEdgeDescriptor,
): boolean {
  return operation.caller_function_id === edge.caller_function_id
    && operation.target_provider_id === edge.target_provider_id
    && operation.contract_id === edge.contract_id
    && operation.operation_id === edge.operation_id;
}

export function flowOperationSelectionKey(
  operation: RuntimeFlowEdgeDescriptor,
): string {
  return [
    operation.caller_function_id,
    operation.target_provider_id,
    operation.contract_id,
    operation.operation_id,
  ].map((value) => JSON.stringify(value)).join('\u0000');
}

export function Flow() {
  const surface = useRuntimeSurface<unknown>('operations');
  const descriptor = LAUNCHER_ADVANCED_VIEWS.flow;
  const flows = surface.data ? extractExactFlowDescriptors(surface.data.data) : null;
  const operations = surface.data ? extractExactOperationDescriptors(surface.data.data) : [];
  const operationIds = new Set(operations.map((operation) => operation.operation_id));
  const hasDeclaredCompositions = Boolean(flows && flows.length > 0);
  const readyFlows = useMemo(
    () => readyFlowCompositions(flows),
    [flows],
  );
  const [activeTab, setActiveTab] = useState<FlowTab>('authoring');
  const [selectedFlowId, setSelectedFlowId] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedFlowId || !readyFlows.some((flow) => flow.flow_id === selectedFlowId)) {
      setSelectedFlowId(readyFlows[0]?.flow_id ?? null);
    }
  }, [selectedFlowId, readyFlows]);

  const selectedFlow = readyFlows.find((flow) => flow.flow_id === selectedFlowId) ?? null;
  const invokableOperations = useMemo(
    () => {
      if (!selectedFlow) return [];
      return selectAdvancedContractInvokableOperations(
        descriptor,
        {status: surface.status, stale: surface.stale, error: surface.error},
        surface.data,
        operations,
      ).filter((operation) => selectedFlow.edges.some((edge) => (
        operationMatchesFlowEdge(operation, edge)
      )));
    },
    [descriptor, surface.status, surface.stale, surface.error, surface.data, operations, selectedFlow],
  );
  const [selectedOperationKey, setSelectedOperationKey] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedOperationKey || !invokableOperations.some((operation) => (
      flowOperationSelectionKey(operation) === selectedOperationKey
    ))) {
      const first = invokableOperations[0];
      setSelectedOperationKey(first ? flowOperationSelectionKey(first) : null);
    }
  }, [selectedOperationKey, invokableOperations]);

  const selectedOperation = invokableOperations.find((operation) => (
    flowOperationSelectionKey(operation) === selectedOperationKey
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
      <Card>
        <CardContent className="pt-5">
          <div role="tablist" aria-label="Flow workspace" className="flex flex-wrap gap-2">
            <Button
              role="tab"
              type="button"
              size="sm"
              variant={activeTab === 'authoring' ? 'default' : 'outline'}
              aria-selected={activeTab === 'authoring'}
              onClick={() => setActiveTab('authoring')}
            >
              Workflow authoring
            </Button>
            <Button
              role="tab"
              type="button"
              size="sm"
              variant={activeTab === 'profile' ? 'default' : 'outline'}
              aria-selected={activeTab === 'profile'}
              onClick={() => setActiveTab('profile')}
            >
              Profile-declared operations
            </Button>
          </div>
        </CardContent>
      </Card>
      {activeTab === 'authoring' ? <WorkflowAuthoringPanel /> : null}
      {activeTab === 'profile' && surface.data ? <RuntimeEvidenceCard envelope={surface.data} title="Flow catalog provenance" /> : null}
      {activeTab === 'profile' ? (
        surface.status === 'ready' && hasDeclaredCompositions ? (
          <div className="grid gap-5">
          {flows && flows.length > 0 ? (
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2"><ListTree className="h-4 w-4" aria-hidden="true" />Profile-declared compositions</CardTitle>
                <p className="text-sm leading-6 text-text-muted">Authoritative invokable_operations available for this surface: {invokableOperations.length}.</p>
              </CardHeader>
              <CardContent className="grid gap-3">
                {flows.map((flow) => (
                  <article key={flow.flow_id} className="rounded-lg border border-border bg-bg-main p-4">
                    <button
                      type="button"
                      className="flex min-h-11 w-full flex-wrap items-center gap-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] disabled:cursor-not-allowed"
                      aria-pressed={flow.flow_id === selectedFlowId}
                      aria-label={`Select Flow composition ${flow.label || flow.flow_id}`}
                      disabled={flow.state !== 'ready' || invocation.busy}
                      onClick={() => setSelectedFlowId(flow.flow_id)}
                    >
                      <span className="text-sm font-semibold text-text-main">{flow.label || flow.flow_id}</span>
                      <Badge variant={flow.state === 'ready' ? 'success' : 'warning'}>{flow.state}</Badge>
                    </button>
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
                    const key = flowOperationSelectionKey(operation);
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
                    <span className="min-w-0 flex-1 break-words">{invocation.error.code}: {invocation.error.message}</span>
                    <CopyErrorButton label="Copy Flow operation error" text={`${invocation.error.code}: ${invocation.error.message}`} />
                  </div>
                ) : null}
                {invocation.state === 'unknown' ? (
                  <div className="mb-4 flex items-start gap-2 rounded-lg border border-amber-300/70 bg-amber-50/70 px-3 py-3 text-sm text-amber-800 dark:border-amber-800/60 dark:bg-amber-950/20 dark:text-amber-200" role="alert">
                    <Clock3 className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                    <span className="min-w-0 flex-1 break-words">The Flow operation result is unknown. Refresh the authoritative operations surface before trying again; no replacement request will be sent.</span>
                    <CopyErrorButton label="Copy unknown Flow operation details" text="The Flow operation result is unknown. Refresh the authoritative operations surface before trying again; no replacement request will be sent." />
                  </div>
                ) : null}
                {invocation.state === 'succeeded' ? <p className="mb-4 text-sm text-emerald-700 dark:text-emerald-300" role="status">Flow operation accepted by the canonical Broker path.</p> : null}
                <OperationInputForm
                  operation={selectedOperation}
                  descriptor={descriptor}
                  busy={invocation.busy}
                  canInvoke={!invocation.error && invokableOperations.some((operation) => (
                    flowOperationSelectionKey(operation) === selectedOperationKey
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
            title="No Profile-declared Flow composition is available"
            message="No authoritative Contract operation is available for this declared Flow composition. Pack inventory is never promoted into a wildcard Flow."
          />
        )
      ) : null}
    </AdvancedSurfaceFrame>
  );
}
