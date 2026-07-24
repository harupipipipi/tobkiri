import {useEffect, useMemo, useState, type ReactNode} from 'react';
import {useSearchParams} from 'react-router';
import {BrainCircuit, Eye, GitBranch, RefreshCw, Save, Scissors, Sparkles, type LucideIcon} from 'lucide-react';

import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {Input} from '@/src/components/ui/Input';
import {TobkiriLoader, TobkiriLoadingMark} from '@/src/components/ui/TobkiriLoader';
import {
  compileStartupProfileAiInputPreview,
  fetchStartupProfileAiInput,
  fetchStartupProfileAiInputTraces,
  fetchStartupProfiles,
  updateStartupProfileAiInput,
} from '@/src/lib/api';
import {
  aiInputEffectiveToolIds,
  aiInputHeavyNodes,
  aiInputPolicySegments,
  insertConditionGate,
  normalizeAiInputConfig,
  toggleAiInputEdge,
} from '@/src/lib/aiInputGraph';
import type {
  ApiAiInputConfig,
  ApiAiInputTraceSummary,
  ApiPromptSegment,
  ApiStartupProfile,
  ApiToolSchemaSegment,
  StartupProfileAiInputResponseData,
} from '@/src/lib/apiTypes';
import {cn} from '@/src/lib/utils';
import {useAppStore} from '@/src/store';

export function AiInputInspector() {
  const addToast = useAppStore((state) => state.addToast);
  const advancedProfileId = useAppStore((state) => state.selectedStartupProfileId);
  const setAdvancedProfileId = useAppStore((state) => state.setSelectedStartupProfileId);
  const [searchParams, setSearchParams] = useSearchParams();
  const [profiles, setProfiles] = useState<ApiStartupProfile[]>([]);
  const [profileId, setProfileId] = useState(searchParams.get('profile') || '');
  const [data, setData] = useState<StartupProfileAiInputResponseData | null>(null);
  const [preview, setPreview] = useState<StartupProfileAiInputResponseData | null>(null);
  const [traces, setTraces] = useState<ApiAiInputTraceSummary[]>([]);
  const [draftConfig, setDraftConfig] = useState<ApiAiInputConfig>(normalizeAiInputConfig(null));
  const [selectedEdgeId, setSelectedEdgeId] = useState('');
  const [gateField, setGateField] = useState('message');
  const [gateOperator, setGateOperator] = useState('contains');
  const [gateValue, setGateValue] = useState('ブラウザ');
  const [previewMessage, setPreviewMessage] = useState('ブラウザで example.com を開いて');
  const [includeText, setIncludeText] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [previewing, setPreviewing] = useState(false);

  const selectedProfile = profiles.find((profile) => profile.profile_id === profileId) || null;
  const visibleData = preview || data;
  const selectedEdge = useMemo(
    () => visibleData?.graph.edges.find((edge) => edge.id === selectedEdgeId) || null,
    [selectedEdgeId, visibleData],
  );
  const heavyNodes = useMemo(() => aiInputHeavyNodes(visibleData), [visibleData]);
  const effectiveToolIds = useMemo(() => aiInputEffectiveToolIds(visibleData), [visibleData]);
  const policySegments = useMemo(() => aiInputPolicySegments(visibleData), [visibleData]);
  const dirty = useMemo(() => {
    if (!data) return false;
    return JSON.stringify(normalizeAiInputConfig(data.ai_input)) !== JSON.stringify(normalizeAiInputConfig(draftConfig));
  }, [data, draftConfig]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([fetchStartupProfiles()])
      .then(async ([startupProfiles]) => {
        if (cancelled) return;
        const nextProfileId = profileId || advancedProfileId || startupProfiles.active_profile_id || startupProfiles.profiles[0]?.profile_id || '';
        setProfiles(startupProfiles.profiles);
        setProfileId(nextProfileId);
        if (nextProfileId) setAdvancedProfileId(nextProfileId);
        if (nextProfileId) {
          const [response, traceResponse] = await Promise.all([
            fetchStartupProfileAiInput(nextProfileId, {include_text: includeText}),
            fetchStartupProfileAiInputTraces(nextProfileId),
          ]);
          if (cancelled) return;
          setData(response);
          setTraces(traceResponse.traces || []);
          setDraftConfig(normalizeAiInputConfig(response.ai_input));
          setSelectedEdgeId(response.graph.edges[0]?.id || '');
        }
      })
      .catch((error) => {
        if (!cancelled) {
          addToast(error instanceof Error ? error.message : 'Failed to load AI input graph', 'error');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [addToast, includeText, profileId]);

  const loadProfile = async (nextProfileId: string, nextIncludeText = includeText) => {
    setProfileId(nextProfileId);
    setAdvancedProfileId(nextProfileId);
    setPreview(null);
    setSearchParams(nextProfileId ? new URLSearchParams({profile: nextProfileId}) : new URLSearchParams(), {replace: true});
    if (!nextProfileId) {
      setTraces([]);
      return;
    }
    setLoading(true);
    try {
      const [response, traceResponse] = await Promise.all([
        fetchStartupProfileAiInput(nextProfileId, {include_text: nextIncludeText}),
        fetchStartupProfileAiInputTraces(nextProfileId),
      ]);
      setData(response);
      setTraces(traceResponse.traces || []);
      setDraftConfig(normalizeAiInputConfig(response.ai_input));
      setSelectedEdgeId(response.graph.edges[0]?.id || '');
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Failed to load AI input graph', 'error');
    } finally {
      setLoading(false);
    }
  };

  const handleToggleEdge = (disabled: boolean) => {
    if (!selectedEdgeId) return;
    setDraftConfig((current) => toggleAiInputEdge(current, selectedEdgeId, disabled));
    setPreview(null);
  };

  const handleInsertConditionGate = () => {
    if (!selectedEdge) return;
    if (!gateValue.trim() && gateOperator !== 'truthy' && gateOperator !== 'falsy') {
      addToast('Condition gate needs a value for this operator', 'error');
      return;
    }
    setDraftConfig((current) => insertConditionGate(current, selectedEdge, {
      field: gateField,
      op: gateOperator,
      value: gateValue,
    }));
    setPreview(null);
  };

  const handlePreview = async () => {
    if (!profileId) return;
    setPreviewing(true);
    try {
      const response = await compileStartupProfileAiInputPreview(profileId, {
        ai_input: draftConfig,
        message: previewMessage,
      });
      setPreview(response);
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Failed to compile AI input preview', 'error');
    } finally {
      setPreviewing(false);
    }
  };

  const handleApply = async () => {
    if (!profileId) return;
    setSaving(true);
    try {
      const response = await updateStartupProfileAiInput(profileId, draftConfig);
      setData(response);
      setPreview(null);
      setDraftConfig(normalizeAiInputConfig(response.ai_input));
      addToast('AI input settings saved', 'success');
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Failed to save AI input settings', 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleShowText = () => {
    setIncludeText(true);
    void loadProfile(profileId, true);
  };

  return (
    <div className="flex flex-1 flex-col gap-5 overflow-y-auto bg-bg-main p-6 animate-in fade-in slide-in-from-bottom-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-text-main">AI Input Inspector</h1>
          <p className="mt-1 max-w-3xl text-sm text-text-muted">
            Inspect the exact prompt segments, tool schemas, policy edges, and token weight that reach the model for a startup profile.
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <Badge variant="secondary">{visibleData?.token_estimate.total || 0} tokens</Badge>
            <Badge variant="outline">{visibleData?.effective_input.system_segments.length || 0} prompt segments</Badge>
            <Badge variant="outline">{effectiveToolIds.length} tool schemas</Badge>
            {preview?.diff ? <Badge variant="secondary">Preview delta {preview.diff.after_tokens - preview.diff.before_tokens}</Badge> : null}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button type="button" size="sm" variant="outline" onClick={() => void loadProfile(profileId)}>
            <RefreshCw className="h-4 w-4" />
            Refresh
          </Button>
          {!includeText ? (
            <Button type="button" size="sm" variant="outline" onClick={handleShowText}>
              <Eye className="h-4 w-4" />
              View text
            </Button>
          ) : null}
        </div>
      </div>

      <section className="rounded-xl border border-border bg-bg-card p-4 shadow-none">
        <div className="grid gap-4 lg:grid-cols-[minmax(220px,280px)_minmax(0,1fr)]">
          <label className="min-w-0 text-sm text-text-muted">
            <span className="mb-1 block text-xs font-medium uppercase tracking-wide">Profile</span>
            <select
              className="rumi-select h-10 w-full rounded-lg border border-border px-3 pr-9 text-sm"
              value={profileId}
              onChange={(event) => void loadProfile(event.target.value)}
            >
              {profiles.map((profile) => (
                <option key={profile.profile_id} value={profile.profile_id}>{profile.name}</option>
              ))}
            </select>
          </label>
          <label className="min-w-0 text-sm text-text-muted">
            <span className="mb-1 block text-xs font-medium uppercase tracking-wide">Preview message</span>
            <Input value={previewMessage} onChange={(event) => setPreviewMessage(event.target.value)} />
          </label>
        </div>
        <div className="mt-4 flex flex-col gap-3 border-t border-border pt-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="min-w-0 flex-1 break-all text-xs text-text-muted sm:break-normal">
            {selectedProfile ? `${selectedProfile.profile_id} -> ${visibleData?.model_input.provider || 'provider pending'} / ${visibleData?.model_input.model || 'model pending'}` : 'Select a profile to inspect model input wiring.'}
          </p>
          <div className="flex flex-wrap gap-2 sm:justify-end">
            <Button type="button" onClick={handlePreview} disabled={!profileId || previewing} className="min-w-[108px]">
              {previewing ? <TobkiriLoadingMark /> : <Sparkles className="h-4 w-4" />}
              Preview
            </Button>
            <Button type="button" variant="outline" onClick={handleApply} disabled={!dirty || saving} className="min-w-[96px]">
              {saving ? <TobkiriLoadingMark /> : <Save className="h-4 w-4" />}
              Apply
            </Button>
          </div>
        </div>
      </section>

      {loading ? (
        <TobkiriLoader className="min-h-[320px] rounded-2xl border border-border bg-bg-card/60" label="Loading AI input graph..." />
      ) : null}

      {!loading && visibleData ? (
        <div className="grid gap-5 xl:grid-cols-[320px_minmax(0,1fr)_320px]">
          <section className="space-y-4">
            <InspectorCard title="Token Heatmap" icon={BrainCircuit}>
              <div className="space-y-2">
                {heavyNodes.map((node) => (
                  <div key={node.id} className="rounded-xl border border-border/70 bg-bg-main p-3">
                    <div className="flex items-center justify-between gap-2 text-xs">
                      <span className="truncate font-mono text-text-main">{node.id}</span>
                      <Badge variant="outline">{node.tokens}</Badge>
                    </div>
                    <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-bg-hover">
                      <div
                        className="h-full rounded-full bg-accent"
                        style={{width: `${Math.min(100, (node.tokens / Math.max(1, visibleData.token_estimate.total)) * 100)}%`}}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </InspectorCard>

            <InspectorCard title="Edge Controls" icon={Scissors}>
              <select
                className="rumi-select h-10 w-full rounded-lg border border-border px-3 pr-9 text-xs"
                value={selectedEdgeId}
                onChange={(event) => setSelectedEdgeId(event.target.value)}
              >
                {visibleData.graph.edges.map((edge) => (
                  <option key={edge.id} value={edge.id}>{edge.id}</option>
                ))}
              </select>
              {selectedEdge ? (
                <div className="mt-3 rounded-xl border border-border/70 bg-bg-main p-3 text-xs text-text-muted">
                  <div className="font-mono text-text-main">{selectedEdge.from_id}</div>
                  <div>{selectedEdge.from_port || 'output'} -&gt; {selectedEdge.to_id}.{selectedEdge.to_port || 'input'}</div>
                </div>
              ) : null}
              <div className="mt-3 flex gap-2">
                <Button type="button" size="sm" variant="outline" onClick={() => handleToggleEdge(true)} disabled={!selectedEdgeId}>
                  Disable Edge
                </Button>
                <Button type="button" size="sm" variant="outline" onClick={() => handleToggleEdge(false)} disabled={!selectedEdgeId}>
                  Enable Edge
                </Button>
              </div>
              <div className="mt-4 space-y-2 rounded-xl border border-border/70 bg-bg-main p-3">
                <div className="text-xs font-medium uppercase tracking-wide text-text-muted">Insert Condition Gate</div>
                <div className="grid gap-2">
                  <select
                    className="rumi-select h-9 w-full rounded-lg border border-border px-3 pr-9 text-xs"
                    value={gateField}
                    onChange={(event) => setGateField(event.target.value)}
                  >
                    <option value="message">message</option>
                    <option value="user_intent">user_intent</option>
                    <option value="selected.tools">selected.tools</option>
                    <option value="profile_id">profile_id</option>
                  </select>
                  <select
                    className="rumi-select h-9 w-full rounded-lg border border-border px-3 pr-9 text-xs"
                    value={gateOperator}
                    onChange={(event) => setGateOperator(event.target.value)}
                  >
                    <option value="contains">contains</option>
                    <option value="eq">eq</option>
                    <option value="includes">includes</option>
                    <option value="truthy">truthy</option>
                    <option value="falsy">falsy</option>
                  </select>
                  <Input
                    value={gateValue}
                    onChange={(event) => setGateValue(event.target.value)}
                    placeholder="browser_automation"
                  />
                </div>
                <Button type="button" size="sm" variant="outline" onClick={handleInsertConditionGate} disabled={!selectedEdge}>
                  <GitBranch className="h-4 w-4" />
                  Insert Gate
                </Button>
              </div>
              <div className="mt-3 text-xs text-text-muted">
                Disabled: {draftConfig.disabled_edges.length ? draftConfig.disabled_edges.join(', ') : 'none'}
              </div>
              <div className="mt-2 text-xs text-text-muted">
                Gates: {Object.keys(draftConfig.gates).length} / inserted edges: {draftConfig.inserted_edges.length}
              </div>
            </InspectorCard>
          </section>

          <section className="space-y-4">
            <InspectorCard title="AI Input Graph" icon={GitBranch}>
              <div className="grid gap-3 md:grid-cols-2">
                {visibleData.graph.nodes.map((node) => (
                  <div key={node.id} className="rounded-2xl border border-border bg-bg-main p-3">
                    <div className="flex items-center justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-semibold text-text-main">{node.label || node.id}</div>
                        <div className="truncate font-mono text-[11px] text-text-muted">{node.id}</div>
                      </div>
                      <Badge variant="outline">{node.kind}</Badge>
                    </div>
                    <div className="mt-2 flex flex-wrap gap-1">
                      {node.input_ports.map((port) => <Badge key={`in:${port}`} variant="secondary">in:{port}</Badge>)}
                      {node.output_ports.map((port) => <Badge key={`out:${port}`} variant="outline">out:{port}</Badge>)}
                    </div>
                  </div>
                ))}
              </div>
            </InspectorCard>

            <InspectorCard title="Prompt Segments" icon={Sparkles}>
              <SegmentList segments={visibleData.effective_input.system_segments} emptyMessage="No active prompt segments." />
            </InspectorCard>

            <InspectorCard title="Context & Policy" icon={Eye}>
              <div className="space-y-4">
                <div>
                  <div className="mb-2 text-xs font-medium uppercase tracking-wide text-text-muted">Memory / Context</div>
                  <SegmentList
                    segments={visibleData.effective_input.context_segments}
                    emptyMessage="No context or memory segments reach the provider payload."
                  />
                </div>
                <div>
                  <div className="mb-2 text-xs font-medium uppercase tracking-wide text-text-muted">Policy / API Routes</div>
                  <SegmentList
                    segments={policySegments}
                    emptyMessage="No policy or API route segments reach the model input policy port."
                  />
                </div>
              </div>
            </InspectorCard>
          </section>

          <section className="space-y-4">
            <InspectorCard title="Tool Schemas" icon={BrainCircuit}>
              <ToolList segments={visibleData.effective_input.tool_schemas} />
            </InspectorCard>

            <InspectorCard title="Diagnostics" icon={Eye}>
              <div className="space-y-2 text-xs text-text-muted">
                {visibleData.gate_decisions.map((decision, index) => (
                  <div key={`${decision.gate_id || index}`} className="rounded-xl border border-border bg-bg-main p-3">
                    <div className="font-mono text-text-main">{String(decision.gate_id || 'gate')}</div>
                    <div>{String(decision.reason || '')}</div>
                  </div>
                ))}
                {visibleData.effective_input.disabled_segments.map((segment) => (
                  <div key={String(segment.id)} className="rounded-xl border border-border bg-bg-main p-3">
                    <div className="font-mono text-text-main">{String(segment.id)}</div>
                    <div>{String(segment.reason || '')}</div>
                  </div>
                ))}
                {!visibleData.gate_decisions.length && !visibleData.effective_input.disabled_segments.length ? (
                  <div>No disabled segments or gate decisions.</div>
                ) : null}
              </div>
            </InspectorCard>

            <InspectorCard title="Runtime Traces" icon={Eye}>
              <TraceList traces={traces} />
            </InspectorCard>
          </section>
        </div>
      ) : null}
    </div>
  );
}

function InspectorCard({
  title,
  icon: Icon,
  children,
}: {
  title: string;
  icon: LucideIcon;
  children: ReactNode;
}) {
  return (
    <section className="rounded-xl border border-border bg-bg-card p-4 shadow-none">
      <div className="mb-3 flex items-center gap-2">
        <Icon className="h-4 w-4 text-accent" />
        <h2 className="text-sm font-semibold text-text-main">{title}</h2>
      </div>
      {children}
    </section>
  );
}

function SegmentList({segments, emptyMessage}: {segments: ApiPromptSegment[]; emptyMessage: string}) {
  if (!segments.length) {
    return <div className="text-sm text-text-muted">{emptyMessage}</div>;
  }
  return (
    <div className="space-y-2">
      {segments.map((segment) => (
        <div key={segment.id} className="rounded-xl border border-border bg-bg-main p-3">
          <div className="flex items-center justify-between gap-2">
            <div className="truncate font-mono text-xs text-text-main">{segment.id}</div>
            <Badge variant="outline">{segment.tokens}</Badge>
          </div>
          <p className={cn('mt-2 text-xs text-text-muted', segment.text ? 'line-clamp-5' : '')}>
            {segment.text || segment.preview || 'Text hidden. Use View text to fetch full prompt text.'}
          </p>
        </div>
      ))}
    </div>
  );
}

function ToolList({segments}: {segments: ApiToolSchemaSegment[]}) {
  if (!segments.length) {
    return <div className="text-sm text-text-muted">No tool schemas reach the provider.</div>;
  }
  return (
    <div className="space-y-2">
      {segments.map((segment) => (
        <div key={segment.id} className="rounded-xl border border-border bg-bg-main p-3">
          <div className="flex items-center justify-between gap-2">
            <div className="truncate font-mono text-xs text-text-main">{segment.tool_id}</div>
            <Badge variant="outline">{segment.tokens}</Badge>
          </div>
          <div className="mt-1 text-xs text-text-muted">{segment.name}</div>
        </div>
      ))}
    </div>
  );
}

function TraceList({traces}: {traces: ApiAiInputTraceSummary[]}) {
  if (!traces.length) {
    return <div className="text-sm text-text-muted">No runtime AI input traces yet.</div>;
  }
  return (
    <div className="space-y-2">
      {traces.map((trace) => (
        <div key={trace.trace_id || `${trace.created_at}`} className="rounded-xl border border-border bg-bg-main p-3">
          <div className="flex items-center justify-between gap-2">
            <div className="truncate font-mono text-xs text-text-main">{trace.trace_id || 'trace'}</div>
            <Badge variant="outline">{trace.token_estimate?.total || 0}</Badge>
          </div>
          <div className="mt-1 text-xs text-text-muted">
            {formatTraceTime(trace.created_at)}
          </div>
          <div className="mt-2 grid grid-cols-2 gap-2 text-[11px] text-text-muted">
            <span>system {String(trace.provider_payload_summary?.system_segment_count || 0)}</span>
            <span>tools {String(trace.provider_payload_summary?.tool_schema_count || 0)}</span>
          </div>
          {trace.blocked_count ? (
            <div className="mt-2 text-[11px] text-warning">{trace.blocked_count} blocked event(s)</div>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function formatTraceTime(createdAt?: number | null): string {
  if (!createdAt) {
    return 'created time unknown';
  }
  return new Date(createdAt * 1000).toLocaleString();
}
