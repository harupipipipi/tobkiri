import type { DragEvent, MouseEvent, WheelEvent } from 'react';
import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import { useAppStore } from '@/src/store';
import { useT } from '@/src/lib/i18n';
import { cn } from '@/src/lib/utils';
import { Button } from '@/src/components/ui/Button';
import { Input } from '@/src/components/ui/Input';
import { InlineLoadError } from '@/src/components/ui/InlineLoadError';
import {
  Plus,
  ChevronDown,
  Play,
  Save,
  Trash2,
  FileText,
  CheckCircle2,
  Clock,
  Workflow,
  X,
  Box,
  PanelLeft,
} from 'lucide-react';
import CodeMirror from '@uiw/react-codemirror';
import { yaml } from '@codemirror/lang-yaml';
import {
  ReactFlow,
  Background,
  Controls,
  useNodesState,
  useEdgesState,
  ReactFlowProvider,
  SelectionMode,
} from '@xyflow/react';
import type { Edge, Node, ReactFlowInstance } from '@xyflow/react';

import { nodeTypes } from '@/src/components/flow/CustomNodes';
import { TobkiriLoadingMark } from '@/src/components/ui/TobkiriLoader';
import { nodesToYaml, yamlToNodes } from '@/src/lib/flowUtils';
import { useFlowHistory } from '@/src/hooks/useFlowHistory';
import { useFlowExecution } from '@/src/hooks/useFlowExecution';
import { useFlowKeyboard } from '@/src/hooks/useFlowKeyboard';
import { useFlowDragDrop } from '@/src/hooks/useFlowDragDrop';
import { useFlowEditor } from '@/src/hooks/useFlowEditor';
import type { AvailableStep, FlowDocumentMeta, FlowPort } from '@/src/lib/types';
import { fetchFlowDetail } from '@/src/lib/api';
import { transformFlowDetail } from '@/src/lib/transforms';
import {
  createDefaultFlowGraph,
  defaultPortsForStep,
  DEFAULT_BASE_PACK,
  normalizeContracts,
} from '@/src/lib/flowGraph';

function buildAvailableSteps(t: ReturnType<typeof useT>): AvailableStep[] {
  return [
    { id: 'mounts.init', name: 'mounts.init', pack: 'core', description: t('flows.step.mounts_init.desc'), ports: defaultPortsForStep('mounts.init') },
    { id: 'registry.load', name: 'registry.load', pack: 'core', description: t('flows.step.registry_load.desc'), ports: defaultPortsForStep('registry.load') },
    { id: 'check_profile', name: 'check_profile', pack: 'utils', description: t('flows.step.check_profile.desc'), ports: defaultPortsForStep('check_profile') },
    { id: 'emit', name: 'emit', pack: 'core', description: t('flows.step.emit.desc'), ports: defaultPortsForStep('emit') },
    { id: 'exec_py', name: 'exec_py', pack: 'python', description: t('flows.step.exec_py.desc'), ports: defaultPortsForStep('exec_py') },
    { id: 'http.get', name: 'http.get', pack: 'network', description: t('flows.step.http_get.desc'), ports: defaultPortsForStep('http.get') },
    { id: 'http.post', name: 'http.post', pack: 'network', description: t('flows.step.http_post.desc'), ports: defaultPortsForStep('http.post') },
    { id: 'log.info', name: 'log.info', pack: 'utils', description: t('flows.step.log_info.desc'), ports: defaultPortsForStep('log.info') },
  ];
}

function deriveFlowId(fileName: string): string {
  return fileName
    .replace(/\.flow\.ya?ml$/i, '')
    .replace(/\.ya?ml$/i, '')
    .trim() || 'untitled';
}

function FlowWorkspaceSkeleton() {
  return (
    <div className="flow-focus-shell flex h-full min-w-0 flex-1 gap-3 overflow-hidden p-3" role="status" aria-label="Loading flows">
      <div className="hidden w-64 animate-pulse rounded-2xl border border-border bg-bg-card min-[1000px]:block" />
      <div className="flex min-w-0 flex-1 flex-col gap-3 rounded-2xl border border-border bg-bg-card p-4">
        <div className="h-8 w-64 animate-pulse rounded bg-bg-hover" />
        <div className="h-10 w-full animate-pulse rounded-xl bg-bg-hover" />
        <div className="flex-1 animate-pulse rounded-[28px] border border-border bg-bg-main" />
      </div>
    </div>
  );
}

function FlowEditorInner() {
  const t = useT();
  const availableSteps = useMemo(() => buildAvailableSteps(t), [t]);
  const flows = useAppStore((state) => state.flows);
  const flowsLoading = useAppStore((state) => state.flowsLoading);
  const flowsError = useAppStore((state) => state.flowsError);
  const loadFlows = useAppStore((state) => state.loadFlows);
  const addFlow = useAppStore((state) => state.addFlow);
  const updateFlow = useAppStore((state) => state.updateFlow);
  const deleteFlow = useAppStore((state) => state.deleteFlow);
  const showDialog = useAppStore((state) => state.showDialog);
  const addToast = useAppStore((state) => state.addToast);
  const setSidebarOpen = useAppStore((state) => state.setSidebarOpen);
  const colorMode = useAppStore((state) => state.colorMode);

  const [selectedFlowId, setSelectedFlowId] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState(false);
  const [isFlowLibraryOpen, setIsFlowLibraryOpen] = useState(true);
  const [isConsoleOpen, setIsConsoleOpen] = useState(false);
  const [isPackDropdownOpen, setIsPackDropdownOpen] = useState(false);
  const [newFlowName, setNewFlowName] = useState('');
  const [activeTab, setActiveTab] = useState<'yaml' | 'result'>('yaml');
  const [selectedPack, setSelectedPack] = useState<string>('all');
  const [flowLoading, setFlowLoading] = useState(false);
  const [flowMeta, setFlowMeta] = useState<FlowDocumentMeta>(() => createDefaultFlowGraph().meta);

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [reactFlowInstance, setReactFlowInstance] = useState<ReactFlowInstance<Node, Edge> | null>(null);
  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const stepRailRef = useRef<HTMLDivElement>(null);
  const packDropdownRef = useRef<HTMLDivElement>(null);
  const flowRequestIdRef = useRef(0);
  const flowsRef = useRef(flows);
  flowsRef.current = flows;

  const selectedFlow = flows.find((flow) => flow.id === selectedFlowId);
  const packs = useMemo(() => ['all', ...Array.from(new Set(availableSteps.map((step) => step.pack)))], [availableSteps]);
  const filteredSteps = selectedPack === 'all' ? availableSteps : availableSteps.filter((step) => step.pack === selectedPack);

  const history = useFlowHistory(nodes, edges, setNodes, setEdges);
  const execution = useFlowExecution(nodes, edges, setNodes);

  const menuPosRef = useRef<((pos: { x: number; y: number } | null) => void) | null>(null);

  const keyboard = useFlowKeyboard({
    nodes,
    setNodes,
    saveHistory: history.saveHistory,
    undo: history.undo,
    redo: history.redo,
    execute: execution.execute,
    reactFlowInstance,
    setMenuPos: (pos) => {
      menuPosRef.current?.(pos);
    },
  });

  const editorHook = useFlowEditor({
    nodes,
    setNodes,
    edges,
    setEdges,
    saveHistory: history.saveHistory,
    reactFlowInstance,
    pressedKeys: keyboard.pressedKeys,
    onInvalidConnection: (reason) => addToast(reason, 'error'),
  });
  menuPosRef.current = editorHook.setMenuPos;

  const dragDrop = useFlowDragDrop({
    nodes,
    edges,
    setNodes,
    setEdges,
    saveHistory: history.saveHistory,
    reactFlowInstance,
    reactFlowWrapper,
  });

  useEffect(() => dragDrop.setupPointerTracking(), [dragDrop.setupPointerTracking]);
  useEffect(() => { void loadFlows(); }, [loadFlows]);
  useEffect(() => {
    // Flow is a focus workspace; collapse the global sidebar on entry so the canvas gets priority.
    setSidebarOpen(false);
  }, [setSidebarOpen]);

  useEffect(() => {
    const handleClick = (event: globalThis.MouseEvent) => {
      if (!packDropdownRef.current?.contains(event.target as globalThis.Node)) {
        setIsPackDropdownOpen(false);
      }
    };
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        setIsPackDropdownOpen(false);
      }
    };
    if (isPackDropdownOpen) {
      window.addEventListener('mousedown', handleClick as EventListener);
      window.addEventListener('keydown', handleKeyDown as EventListener);
      return () => {
        window.removeEventListener('mousedown', handleClick as EventListener);
        window.removeEventListener('keydown', handleKeyDown as EventListener);
      };
    }
  }, [isPackDropdownOpen]);

  useEffect(() => {
    if (flows.length > 0 && !selectedFlowId && !isCreating) {
      setSelectedFlowId(flows[0].id);
    }
  }, [flows, isCreating, selectedFlowId]);

  useEffect(() => {
    if (selectedFlowId || isCreating) {
      setIsFlowLibraryOpen(false);
    }
  }, [isCreating, selectedFlowId]);

  useEffect(() => {
    if (!selectedFlowId || isCreating) return;
    const requestId = flowRequestIdRef.current + 1;
    flowRequestIdRef.current = requestId;
    let cancelled = false;
    const fallbackFlow = flowsRef.current.find((flow) => flow.id === selectedFlowId);

    setFlowLoading(true);
    fetchFlowDetail(selectedFlowId)
      .then((detail) => {
        if (cancelled || flowRequestIdRef.current !== requestId) return;
        const flow = transformFlowDetail(detail);
        const parsed = yamlToNodes(flow.content, {
          flowId: selectedFlowId,
          name: flow.name,
          basePack: DEFAULT_BASE_PACK,
        });
        setNodes(parsed.nodes);
        setEdges(parsed.edges);
        setFlowMeta(parsed.meta);
        editorHook.setSelectedNode(null);
        execution.clearResult();
      })
      .catch((err) => {
        if (cancelled || flowRequestIdRef.current !== requestId) return;
        if (fallbackFlow) {
          const parsed = yamlToNodes(fallbackFlow.content, {
            flowId: fallbackFlow.id,
            name: fallbackFlow.name,
            basePack: DEFAULT_BASE_PACK,
          });
          setNodes(parsed.nodes);
          setEdges(parsed.edges);
          setFlowMeta(parsed.meta);
        }
        addToast(err instanceof Error ? err.message : 'Failed to load flow detail', 'error');
      })
      .finally(() => {
        if (cancelled || flowRequestIdRef.current !== requestId) return;
        setFlowLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [addToast, execution.clearResult, isCreating, selectedFlowId, setEdges, setNodes, editorHook.setSelectedNode]);

  const handleSelectFlow = (id: string) => {
    setSelectedFlowId(id);
    setIsCreating(false);
    setIsFlowLibraryOpen(false);
  };

  const handleCreateNew = () => {
    setIsCreating(true);
    setSelectedFlowId(null);
    setIsFlowLibraryOpen(false);
    setIsConsoleOpen(false);
    setNewFlowName('');
    execution.clearResult();
    const graph = createDefaultFlowGraph(DEFAULT_BASE_PACK);
    setNodes(graph.nodes);
    setEdges(graph.edges);
    setFlowMeta(graph.meta);
  };

  const generatedYaml = nodesToYaml(nodes, edges, flowMeta);
  const isExecuteDisabled = execution.isExecuting || (!selectedFlowId && !isCreating);

  const handleSave = async () => {
    if (isCreating) {
      if (!newFlowName.trim()) {
        addToast(t('flows.name_required'), 'error');
        return;
      }
      const fileName = newFlowName.endsWith('.yaml') ? newFlowName : `${newFlowName}.flow.yaml`;
      const flowId = deriveFlowId(fileName);
      const yamlContent = nodesToYaml(nodes, edges, {
        ...flowMeta,
        flowId,
        name: fileName,
      });
      if (!await addFlow({ id: flowId, name: fileName, content: yamlContent })) return;
      const created = useAppStore.getState().flows.find((flow) => flow.id === flowId);
      if (created) {
        setSelectedFlowId(created.id);
      }
      setFlowMeta((previous) => ({ ...previous, flowId, name: fileName }));
      setIsCreating(false);
      addToast(t('flows.created'), 'success');
      return;
    }

    if (selectedFlowId && await updateFlow(selectedFlowId, generatedYaml)) {
      addToast(t('flows.saved'), 'success');
    }
  };

  const handleDelete = () => {
    if (!selectedFlowId) return;
    showDialog({
      title: t('flows.delete_title'),
      message: t('flows.delete_message'),
      confirmText: t('flows.delete_confirm'),
      onConfirm: async () => {
        if (!await deleteFlow(selectedFlowId)) return;
        setSelectedFlowId(null);
        const graph = createDefaultFlowGraph(DEFAULT_BASE_PACK);
        setNodes(graph.nodes);
        setEdges(graph.edges);
        setFlowMeta(graph.meta);
        addToast(t('flows.deleted'), 'success');
      },
    });
  };

  const handleExecute = async () => {
    setActiveTab('result');
    setIsConsoleOpen(true);
    const result = await execution.execute();
    if (result) {
      addToast(t('flows.executed'), result.status === 'success' ? 'success' : 'error');
    }
  };

  const onDragStart = (event: DragEvent, step: AvailableStep) => {
    const ghost = new Image();
    ghost.src = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
    event.dataTransfer.setDragImage(ghost, 0, 0);
    event.dataTransfer.setData('application/reactflow', 'step');
    event.dataTransfer.setData('stepId', step.id);
    event.dataTransfer.setData('stepTitle', step.name);
    event.dataTransfer.setData('stepPorts', JSON.stringify(step.ports ?? []));
    event.dataTransfer.effectAllowed = 'move';
  };

  const handleStepMiddleClick = useCallback((event: MouseEvent, step: AvailableStep) => {
    if (event.button !== 1 || !reactFlowInstance) return;
    event.preventDefault();
    const wrapper = reactFlowWrapper.current;
    if (!wrapper) return;
    const bounds = wrapper.getBoundingClientRect();
    const position = reactFlowInstance.screenToFlowPosition({
      x: bounds.left + bounds.width / 2,
      y: bounds.top + bounds.height / 2,
    });
    history.saveHistory();
    setNodes((existing) => existing.concat({
      id: `step-${Date.now()}`,
      type: 'step',
      position,
      data: {
        id: step.id,
        title: step.name,
        type: 'action',
        description: step.description,
        ports: step.ports ?? [],
      },
    }));
  }, [history, reactFlowInstance, setNodes]);

  const handleStepRailWheel = useCallback((event: WheelEvent<HTMLDivElement>) => {
    const rail = stepRailRef.current;
    if (!rail || rail.scrollWidth <= rail.clientWidth) return;

    const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
    if (delta === 0) return;

    rail.scrollLeft += delta;
    event.preventDefault();
  }, []);

  const selectedPorts = (((editorHook.selectedNode?.data as { ports?: FlowPort[] } | undefined)?.ports) ?? []);

  const openFlowLibrary = () => {
    if (typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 999px)').matches) {
      editorHook.setSelectedNode(null);
    }
    setIsFlowLibraryOpen(true);
  };

  useEffect(() => {
    if (!reactFlowInstance || nodes.length === 0) return;
    const frame = window.requestAnimationFrame(() => {
      reactFlowInstance.fitView({ padding: 0.22, duration: 280, maxZoom: 1.05 });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [nodes.length, reactFlowInstance, selectedFlowId, isCreating]);

  if (flowsLoading && flows.length === 0) return <FlowWorkspaceSkeleton />;

  if (flowsError && flows.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center p-6">
        <div className="w-full max-w-xl">
          <InlineLoadError
            title="Flows could not be loaded"
            message={flowsError}
            onRetry={() => { void loadFlows(); }}
            retrying={flowsLoading}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="flow-focus-shell relative flex h-full min-w-0 flex-1 gap-3 overflow-hidden p-2 animate-in fade-in slide-in-from-bottom-4 sm:p-3">
      {isFlowLibraryOpen && (
        <div
          data-testid="flow-library"
          className="z-40 flex w-64 shrink-0 flex-col gap-3 rounded-2xl border border-border bg-bg-card p-3 shadow-lg min-[1000px]:relative max-[999px]:absolute max-[999px]:inset-y-2 max-[999px]:left-2"
        >
          <div className="flex items-center justify-between">
            <div className="text-[11px] font-semibold uppercase tracking-[0.24em] text-text-muted">{t('flows.flow_list')}</div>
            <button
              type="button"
              onClick={() => setIsFlowLibraryOpen(false)}
              className="rounded-xl border border-border bg-bg-main px-2 py-1 text-text-muted transition-colors hover:bg-bg-hover hover:text-text-main"
              title={t('flows.close_flow_list')}
            >
              <PanelLeft className="h-4 w-4" />
            </button>
          </div>
          <Button size="sm" onClick={handleCreateNew} variant={isCreating ? 'default' : 'outline'} className="w-full gap-1.5 text-xs">
            <Plus className="h-3.5 w-3.5" />
            {t('flows.new')}
          </Button>
          <div className="flex flex-col gap-1.5 overflow-y-auto scrollbar-dark">
            {flows.map((flow) => (
              <button
                key={flow.id}
                type="button"
                onClick={() => handleSelectFlow(flow.id)}
                className={cn(
                  'flex items-center gap-3 rounded-2xl px-3 py-3 text-left transition-colors',
                  selectedFlowId === flow.id && !isCreating
                    ? 'bg-accent text-accent-fg shadow-sm'
                    : 'text-text-main hover:bg-bg-hover',
                )}
              >
                <FileText className="h-4 w-4 shrink-0" />
                <span className="truncate text-sm font-medium">{flow.name}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      <div data-testid="flow-editor" className="relative flex min-w-0 flex-1 flex-col gap-3 overflow-hidden rounded-2xl border border-border bg-bg-card p-3 min-[1000px]:p-4">
        {!isFlowLibraryOpen && (
          <button
            type="button"
            data-testid="flow-library-toggle"
            onClick={openFlowLibrary}
            className="absolute left-4 top-4 z-30 inline-flex h-10 w-10 items-center justify-center rounded-2xl border border-border bg-bg-main/92 text-text-muted shadow-sm transition-colors hover:bg-bg-hover hover:text-text-main"
            title={t('flows.open_flow_list')}
          >
            <PanelLeft className="h-4 w-4 rotate-180" />
          </button>
        )}
        {isCreating || selectedFlowId ? (
          <>
            <div className={cn('flex min-w-0 flex-wrap items-center justify-between gap-2', !isFlowLibraryOpen && 'pl-12')}>
              <div className="min-w-0 flex-1">
                {isCreating ? (
                  <Input
                    placeholder={t('flows.name_placeholder')}
                    value={newFlowName}
                    onChange={(event) => setNewFlowName(event.target.value)}
                    className="max-w-sm"
                  />
                ) : (
                  <div className="min-w-0">
                    <h2 className="truncate text-lg font-bold text-text-main min-[1000px]:text-xl" title={selectedFlow?.name}>{selectedFlow?.name}</h2>
                    <div className="truncate text-xs text-text-muted">{t('flows.flow_id')}: {flowMeta.flowId}</div>
                  </div>
                )}
              </div>
              <div data-testid="flow-actions" className="flex flex-wrap items-center justify-end gap-2">
                {!isCreating && (
                  <Button variant="outline" onClick={handleExecute} disabled={isExecuteDisabled} className="gap-1.5 px-2 min-[1000px]:gap-2 min-[1000px]:px-4">
                    <Play className="h-4 w-4" />
                    {execution.isExecuting ? t('flows.executing') : t('flows.execute')}
                  </Button>
                )}
                <Button variant="outline" onClick={handleSave} className="gap-1.5 px-2 min-[1000px]:gap-2 min-[1000px]:px-4">
                  <Save className="h-4 w-4" />
                  {t('flows.save')}
                </Button>
                {!isCreating && (
                  <Button variant="destructive" onClick={handleDelete} className="gap-1.5 px-2 min-[1000px]:gap-2 min-[1000px]:px-4">
                    <Trash2 className="h-4 w-4" />
                    {t('flows.delete')}
                  </Button>
                )}
              </div>
            </div>

            <div className={cn('flex min-w-0 items-center gap-2 rounded-xl border border-border bg-bg-main px-2 py-2 min-[1000px]:gap-4 min-[1000px]:px-3', !isFlowLibraryOpen && 'ml-12')}>
              <div ref={packDropdownRef} className="relative">
                <Button
                  variant="outline"
                  size="sm"
                  className="h-8 gap-1.5 border-border bg-bg-card px-3 text-xs font-medium"
                  onClick={() => setIsPackDropdownOpen((open) => !open)}
                  aria-haspopup="listbox"
                  aria-expanded={isPackDropdownOpen}
                  aria-controls="flow-pack-selector-menu"
                >
                  {selectedPack === 'all' ? t('flows.all_packs') : selectedPack}
                  <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', isPackDropdownOpen && 'rotate-180')} />
                </Button>
                {isPackDropdownOpen && (
                  <div
                    id="flow-pack-selector-menu"
                    role="listbox"
                    aria-label={t('flows.pack_filter')}
                    className="absolute left-0 top-full z-50 mt-1 w-40 rounded-lg border border-border bg-bg-card py-1 shadow-lg"
                  >
                    {packs.map((pack) => (
                      <button
                        key={pack}
                        type="button"
                        role="option"
                        aria-selected={selectedPack === pack}
                        onClick={() => { setSelectedPack(pack); setIsPackDropdownOpen(false); }}
                        className={cn(
                          'w-full px-3 py-1.5 text-left text-xs transition-colors hover:bg-bg-hover',
                          selectedPack === pack ? 'bg-bg-hover text-text-main' : 'text-text-muted',
                        )}
                      >
                        {pack === 'all' ? t('flows.all_packs') : pack}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <div
                ref={stepRailRef}
                onWheel={handleStepRailWheel}
                className="scrollbar-hidden flex flex-1 items-center gap-2 overflow-x-auto pb-1 overscroll-contain"
              >
                {filteredSteps.map((step) => (
                  <div
                    key={step.id}
                    className="flex shrink-0 cursor-grab items-center gap-1.5 rounded-full border border-border bg-bg-card px-3 py-1.5 text-xs font-medium shadow-sm transition-colors hover:border-accent hover:text-accent"
                    draggable
                    onDragStart={(event) => onDragStart(event, step)}
                    onMouseDown={(event) => handleStepMiddleClick(event, step)}
                    onAuxClick={(event) => event.preventDefault()}
                    title={`${step.description} (${t('flows.pack')}: ${step.pack})`}
                  >
                    <Box className="h-3.5 w-3.5" />
                    {step.name}
                  </div>
                ))}
              </div>
            </div>

            <div
              ref={reactFlowWrapper}
              className="flow-canvas relative flex-1 overflow-hidden rounded-[28px] border border-border"
            >
              <ReactFlow<Node, Edge>
                nodes={nodes}
                edges={edges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onConnect={editorHook.onConnect}
                onNodeClick={editorHook.onNodeClick}
                onNodeDragStart={dragDrop.onNodeDragStart}
                onNodeDrag={dragDrop.onNodeDrag}
                onNodeDragStop={dragDrop.onNodeDragStop}
                onPaneClick={editorHook.onPaneClick}
                onPaneContextMenu={editorHook.onPaneContextMenu}
                onConnectEnd={editorHook.onConnectEnd}
                onEdgeClick={editorHook.onEdgeClick}
                onReconnect={editorHook.onReconnect}
                onEdgeDoubleClick={editorHook.onEdgeDoubleClick}
                onNodesDelete={editorHook.onNodesDelete}
                onEdgesDelete={editorHook.onEdgesDelete}
                onInit={(instance) => setReactFlowInstance(instance)}
                onDrop={dragDrop.onDrop}
                onDragOver={dragDrop.onDragOver}
                isValidConnection={editorHook.isValidConnection}
                nodeTypes={nodeTypes}
                panOnDrag={[1, 2]}
                selectionOnDrag
                selectionMode={SelectionMode.Partial}
                fitView
                className="flow-grid"
              >
                <Background color="var(--flow-grid-color)" gap={28} size={1.1} />
                <Controls className="bg-bg-card border-border fill-text-main" />
              </ReactFlow>

              <div
                className={cn(
                  'pointer-events-none absolute inset-0 z-20 flex items-center justify-center transition-opacity duration-150',
                  flowLoading ? 'opacity-100' : 'opacity-0',
                )}
              >
                <div className="rounded-full border border-border bg-bg-card/85 p-3 shadow-lg backdrop-blur-sm">
                  <TobkiriLoadingMark />
                </div>
              </div>

              <div
                className={cn(
                  'pointer-events-none absolute bottom-0 left-0 right-0 z-50 flex items-center justify-center border-t-2 border-dashed transition-all duration-200',
                  dragDrop.isDraggingNode ? 'h-20 opacity-100' : 'h-0 opacity-0',
                  dragDrop.isOverDeleteZone ? 'border-red-400 bg-red-500/30' : 'border-red-300/50 bg-red-500/10',
                )}
              >
                <div className={cn('flex items-center gap-2 text-sm font-medium', dragDrop.isOverDeleteZone ? 'scale-110 text-red-200' : 'text-red-300')}>
                  <Trash2 className="h-5 w-5" />
                  {dragDrop.isOverDeleteZone ? t('flows.release_to_delete') : t('flows.drop_to_delete')}
                </div>
              </div>

              {editorHook.menuPos && (
                <div
                  className="absolute z-50 flex w-72 flex-col rounded-xl border border-border bg-bg-card p-2 shadow-xl"
                  style={{ top: editorHook.menuPos.y, left: editorHook.menuPos.x }}
                  onClick={(event) => event.stopPropagation()}
                >
                  <div className="mb-2 flex items-center justify-between">
                    <span className="px-1 text-xs font-bold text-text-muted">{t('flows.add_node')}</span>
                    <button type="button" onClick={() => editorHook.setMenuPos(null)} className="text-text-muted hover:text-text-main">
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                  <Input
                    autoFocus
                    placeholder={t('flows.search_nodes')}
                    value={editorHook.menuFilter}
                    onChange={(event) => editorHook.setMenuFilter(event.target.value)}
                    className="mb-2 h-8 text-sm"
                  />
                  <div className="flex max-h-64 flex-col gap-1 overflow-y-auto scrollbar-dark">
                    {availableSteps
                      .filter((step) => step.name.toLowerCase().includes(editorHook.menuFilter.toLowerCase()) || step.description.toLowerCase().includes(editorHook.menuFilter.toLowerCase()))
                      .map((step) => (
                        <div
                          key={step.id}
                          className="flex cursor-pointer flex-col rounded px-2 py-2 text-sm hover:bg-bg-hover"
                          onClick={() => editorHook.handleAddNodeFromMenu({ id: step.id, title: step.name, ports: step.ports })}
                        >
                          <span className="font-medium">{step.name}</span>
                          <span className="text-[10px] text-text-muted">{step.description}</span>
                        </div>
                      ))}
                  </div>
                </div>
              )}

              {editorHook.selectedNode && (
                <div
                  data-testid="flow-inspector"
                  className="absolute inset-y-2 right-2 z-30 flex w-[min(20rem,calc(100%-4.5rem))] flex-col overflow-hidden rounded-xl border border-border bg-bg-card shadow-xl min-[1000px]:inset-y-auto min-[1000px]:right-4 min-[1000px]:top-4 min-[1000px]:max-h-[80%] min-[1000px]:w-80"
                >
                  <div className="flex items-center justify-between border-b border-border p-3">
                    <div>
                      <h3 className="text-sm font-semibold text-text-main">{t('flows.properties')}</h3>
                      <div className="text-[11px] text-text-muted">{editorHook.selectedNode.type}</div>
                    </div>
                    <button type="button" onClick={() => editorHook.setSelectedNode(null)} className="text-text-muted hover:text-text-main">
                      <X className="h-4 w-4" />
                    </button>
                  </div>
                  <div className="flex flex-1 flex-col gap-4 overflow-y-auto p-4">
                    {editorHook.selectedNode.type === 'trigger' && (
                      <>
                        <div className="space-y-2">
                          <label className="text-xs font-medium text-text-muted">{t('flows.start_type')}</label>
                          <Input
                            value={(editorHook.selectedNode.data.type as string) || ''}
                            onChange={(event) => editorHook.updateNodeData('type', event.target.value)}
                            className="h-8 text-sm"
                          />
                        </div>
                        <div className="space-y-2">
                          <label className="text-xs font-medium text-text-muted">{t('flows.base_pack')}</label>
                          <Input
                            value={(editorHook.selectedNode.data.basePack as string) || DEFAULT_BASE_PACK}
                            onChange={(event) => {
                              editorHook.updateNodeData('basePack', event.target.value);
                              setFlowMeta((previous) => ({ ...previous, basePack: event.target.value }));
                            }}
                            className="h-8 text-sm"
                          />
                        </div>
                      </>
                    )}
                    {editorHook.selectedNode.type === 'step' && (
                      <>
                        <div className="space-y-2">
                          <label className="text-xs font-medium text-text-muted">{t('flows.step_id')}</label>
                          <Input
                            value={(editorHook.selectedNode.data.id as string) || ''}
                            onChange={(event) => editorHook.updateNodeData('id', event.target.value)}
                            className="h-8 text-sm"
                          />
                        </div>
                        <div className="space-y-2">
                          <label className="text-xs font-medium text-text-muted">{t('flows.step_title')}</label>
                          <Input
                            value={(editorHook.selectedNode.data.title as string) || ''}
                            onChange={(event) => editorHook.updateNodeData('title', event.target.value)}
                            className="h-8 text-sm"
                          />
                        </div>
                        <div className="space-y-2">
                          <label className="text-xs font-medium text-text-muted">{t('flows.step_type')}</label>
                          <Input
                            value={(editorHook.selectedNode.data.type as string) || ''}
                            onChange={(event) => editorHook.updateNodeData('type', event.target.value)}
                            className="h-8 text-sm"
                          />
                        </div>
                        <div className="space-y-2">
                          <label className="text-xs font-medium text-text-muted">{t('flows.phase')}</label>
                          <Input
                            value={(editorHook.selectedNode.data.phase as string) || flowMeta.phases[0] || 'graph'}
                            onChange={(event) => editorHook.updateNodeData('phase', event.target.value)}
                            className="h-8 text-sm"
                          />
                        </div>
                      </>
                    )}

                    <div className="rounded-xl border border-border bg-bg-main p-3">
                      <div className="mb-3 flex items-center justify-between">
                        <div>
                          <div className="text-sm font-semibold text-text-main">{t('flows.ports')}</div>
                          <div className="text-[11px] text-text-muted">{t('flows.port_contract_help')}</div>
                        </div>
                        <div className="flex gap-2">
                          <Button type="button" variant="outline" size="sm" onClick={() => editorHook.addPortToSelectedNode('input')}>{t('flows.add_input')}</Button>
                          <Button type="button" variant="outline" size="sm" onClick={() => editorHook.addPortToSelectedNode('output')}>{t('flows.add_output')}</Button>
                        </div>
                      </div>
                      <div className="flex flex-col gap-3">
                        {selectedPorts.map((port) => (
                          <div key={port.id} className="rounded-xl border border-border bg-bg-card p-3">
                            <div className="mb-2 flex items-center justify-between">
                              <div className="text-xs font-semibold text-text-main">{port.id}</div>
                              <button type="button" className="text-xs text-rose-400 hover:text-rose-300" onClick={() => editorHook.removeSelectedNodePort(port.id)}>
                                {t('flows.remove')}
                              </button>
                            </div>
                            <div className="grid grid-cols-2 gap-2">
                              <Input
                                value={port.label}
                                onChange={(event) => editorHook.updateSelectedNodePort(port.id, { label: event.target.value })}
                                className="h-8 text-xs"
                              />
                              <div className="grid h-8 grid-cols-2 overflow-hidden rounded-md border border-border bg-bg-main p-0.5">
                                {(['input', 'output'] as const).map((direction) => (
                                  <button
                                    key={direction}
                                    type="button"
                                    onClick={() => editorHook.updateSelectedNodePort(port.id, { direction })}
                                    className={cn(
                                      'rounded-[5px] px-2 text-xs font-semibold transition-colors',
                                      port.direction === direction
                                        ? 'bg-accent text-white shadow-sm'
                                        : 'text-text-muted hover:bg-bg-hover hover:text-text-main',
                                    )}
                                  >
                                    {direction === 'input' ? t('flows.direction.short_input') : t('flows.direction.short_output')}
                                  </button>
                                ))}
                              </div>
                              <Input
                                value={port.id}
                                onChange={(event) => editorHook.updateSelectedNodePort(port.id, { id: event.target.value })}
                                className="h-8 text-xs"
                              />
                              <Input
                                value={port.contracts.join(', ')}
                                onChange={(event) => editorHook.updateSelectedNodePort(port.id, { contracts: normalizeContracts(event.target.value) })}
                                className="h-8 text-xs"
                              />
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>

                    <Button variant="destructive" size="sm" onClick={editorHook.deleteSelectedNode} className="mt-2">
                      <Trash2 className="mr-2 h-4 w-4" /> {t('flows.delete_node')}
                    </Button>
                  </div>
                </div>
              )}

              <div className="absolute bottom-4 right-4 z-30 flex flex-col items-end gap-3">
                {!isConsoleOpen && (
                  <button
                    type="button"
                    onClick={() => setIsConsoleOpen(true)}
                    className="inline-flex items-center gap-2 rounded-full border border-border bg-bg-card/92 px-4 py-2 text-sm font-medium text-text-main shadow-lg shadow-black/20 backdrop-blur-sm transition-colors hover:bg-bg-hover"
                  >
                    <FileText className="h-4 w-4" />
                    {t('flows.yaml_result')}
                  </button>
                )}

                {isConsoleOpen && (
                  <div className="flex h-[320px] w-[440px] max-w-[calc(100vw-4rem)] flex-col overflow-hidden rounded-[26px] border border-border bg-bg-card/96 shadow-[0_22px_60px_rgba(0,0,0,0.35)] backdrop-blur-sm">
                    <div className="flex items-center justify-between border-b border-border bg-bg-main/85 px-3 py-2">
                      <div className="flex">
                        <button
                          className={cn('px-4 py-2 text-sm font-medium transition-colors', activeTab === 'yaml' ? 'border-b-2 border-accent text-text-main' : 'text-text-muted hover:text-text-main')}
                          onClick={() => setActiveTab('yaml')}
                        >
                          {t('flows.yaml')}
                        </button>
                        <button
                          className={cn('px-4 py-2 text-sm font-medium transition-colors', activeTab === 'result' ? 'border-b-2 border-accent text-text-main' : 'text-text-muted hover:text-text-main')}
                          onClick={() => setActiveTab('result')}
                        >
                          {t('flows.result')}
                        </button>
                      </div>
                      <button
                        type="button"
                        onClick={() => setIsConsoleOpen(false)}
                        className="rounded-xl px-2 py-1 text-[11px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text-main"
                        title={t('flows.hide_yaml_result')}
                      >
                        <X className="h-4 w-4" />
                      </button>
                    </div>
                    <div className="flex-1 overflow-auto scrollbar-dark">
                      {activeTab === 'yaml' && (
                        <CodeMirror
                          value={generatedYaml}
                          height="100%"
                          extensions={[yaml()]}
                          theme={colorMode === 'dark' ? 'dark' : 'light'}
                          readOnly
                          className="h-full text-sm"
                        />
                      )}
                      {activeTab === 'result' && (
                        <div className="p-4">
                          {execution.isExecuting ? (
                            <div className="flex h-full items-center justify-center text-text-muted">
                              <TobkiriLoadingMark /> {t('flows.executing')}
                            </div>
                          ) : execution.executionResult ? (
                            <div className="flex flex-col gap-2">
                              {execution.executionResult.steps.map((step, index) => (
                                <div key={index} className="flex items-center justify-between rounded-xl border border-border bg-bg-main/80 p-2.5 text-sm">
                                  <div className="flex items-center gap-2">
                                    {step.status === 'success' ? <CheckCircle2 className="h-4 w-4 text-green-500" /> : <X className="h-4 w-4 text-red-500" />}
                                    <span className="font-medium text-text-main">{step.name}</span>
                                  </div>
                                  <div className="flex items-center gap-2 text-text-muted">
                                    <Clock className="h-3 w-3" />
                                    <span>{step.duration}</span>
                                  </div>
                                </div>
                              ))}
                            </div>
                          ) : (
                            <div className="flex h-full items-center justify-center text-sm text-text-muted">
                              {t('flows.no_result')}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </>
        ) : (
          <div className="relative flex h-full flex-col items-center justify-center overflow-hidden rounded-[28px] border border-border/70 bg-bg-main text-center">
            <div className="relative z-10 flex flex-col items-center">
              <Workflow className="mb-4 h-16 w-16 text-accent opacity-80" />
              <h3 className="mb-2 text-xl font-bold text-text-main">{t('flows.title')}</h3>
              <p className="max-w-sm text-sm text-text-muted">{t('flows.subtitle')}</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export function Flows() {
  return (
    <ReactFlowProvider>
      <FlowEditorInner />
    </ReactFlowProvider>
  );
}
