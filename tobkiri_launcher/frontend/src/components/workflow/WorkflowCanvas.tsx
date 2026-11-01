import {RegisteredToolPicker} from './RegisteredToolPicker';
import type {WorkflowToolSnapshot} from '@/src/lib/workflowToolCatalog';
import {readWorkflowLayout,saveWorkflowLayout} from '@/src/lib/workflowLayout';
import {getBrowserStorage} from '@/src/lib/safeStorage';
import {WorkflowLiteralInput} from './WorkflowLiteralInput';
import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { Cable, Minus, Plus, Search, Trash2, Undo2, Redo2, Maximize2, X } from 'lucide-react';
import type { WorkflowPaletteOperation } from '@/src/lib/workflowAuthoring';
import { insertPaletteWorkflowStep } from '@/src/lib/workflowEditor';
import { boundedGraphHistory, workflowPortTypeLabel, workflowPortTitle, labelGraphNode, connectExecution, connectData, compatiblePorts, dataWires, schemaPorts, setGraphInput, isGraphRecord, connectionError, deleteGraphNode, disconnectExecution, exactGraphOperation, graphSteps, graphWires, type WorkflowPort } from '@/src/lib/workflowGraph';
import { Button } from '@/src/components/ui/Button';
import { RegisteredModelPicker } from './RegisteredModelPicker';
import {createVoiceWorkflow,voiceWorkflowOperations} from '@/src/lib/voiceWorkflowTemplate';
interface Point {
    x: number;
    y: number;
}
export interface WorkflowCanvasProps {
    document: Record<string, unknown>;
    operations: readonly WorkflowPaletteOperation[];
    disabled?: boolean;
    layoutKey?: string;
    profileId?: string;
    planDigest?: string;
    loadTools?: () => Promise<WorkflowToolSnapshot>;
    onChange: (document: Record<string, unknown>) => void;
}
export const canvasOperationKey = (operation: WorkflowPaletteOperation) => JSON.stringify([operation.contract_id, operation.contract_revision_digest, operation.operation_id, operation.function_principal_id]);
function operationTitle(operation: WorkflowPaletteOperation): string {
    const title = operation.input_schema?.title;
    return typeof title === 'string' && title.trim() && title.length <= 128 ? title : operation.operation_id;
}

const portTone = (port: WorkflowPort): string => port.valueType ?
    ['border-sky-500','border-violet-500','border-emerald-500','border-amber-500'][Array.from(port.valueType).reduce((hash,char)=>(hash*31+char.charCodeAt(0))>>>0,0)%4] : ({
    execution: 'border-accent', string: 'border-emerald-500',
    number: 'border-cyan-500', integer: 'border-cyan-500',
    boolean: 'border-rose-500', object: 'border-violet-500',
    array: 'border-amber-500', null:'border-slate-400',json:'border-zinc-400',union:'border-indigo-400', unknown: 'border-text-muted',
})[port.kind];

/** Pointer and keyboard editing of the canonical document; no runtime side effects. */
export function WorkflowCanvas({ document, operations, disabled = false, layoutKey, profileId, planDigest, loadTools, onChange }: WorkflowCanvasProps) {
    const [query, setQuery] = useState('');
    const toolContext = JSON.stringify([profileId,planDigest,layoutKey ?? operations.map(canvasOperationKey).join('|')]);
    const [toolState,setToolState] = useState<{context:string;snapshot:WorkflowToolSnapshot|null}|null>(null);
    const toolSnapshot = toolState?.context === toolContext ? toolState.snapshot : null;
    const [detailedPorts, setDetailedPorts] = useState(false);
    const [selected, setSelected] = useState<string | null>(null);
    const [pendingSource, setPendingSource] = useState<string | null>(null);
    const [pendingData, setPendingData] = useState<{
        step: string;
        port: WorkflowPort;
    } | null>(null);
    const [positions, setPositions] = useState<Record<string, Point>>(()=>readWorkflowLayout(getBrowserStorage('session'),layoutKey,new Set(graphSteps(document).map(step=>String(step.id)))));
    const positionsRef=useRef(positions);
    const persistLayout=()=>saveWorkflowLayout(getBrowserStorage('session'),layoutKey,positionsRef.current,new Set(graphSteps(document).map(step=>String(step.id))));
    const [zoom, setZoom] = useState(1);
    const viewport = useRef<HTMLDivElement>(null);
    const surface = useRef<HTMLDivElement>(null);
    const wireDrag = useRef<{pointerId: number; origin: Point; source: string; port?: WorkflowPort; start: Point; moved: boolean} | null>(null);
    const [wirePreview, setWirePreview] = useState<{start: Point; end: Point} | null>(null);
    const suppressPortClick = useRef(false);
    const cancelWireDrag = () => {
        wireDrag.current = null;
        setWirePreview(null);
        setPendingData(null);
        setPendingSource(null);
    };
    const beginWireDrag = (event: ReactPointerEvent<HTMLButtonElement>, source: string, port?: WorkflowPort) => {
        if (disabled || event.button !== 0 || wireDrag.current) return;
        suppressPortClick.current = false;
        const bounds = surface.current?.getBoundingClientRect();
        if (!bounds) return;
        wireDrag.current = {start: {x: (event.clientX - bounds.left) / zoom, y: (event.clientY - bounds.top) / zoom}, pointerId: event.pointerId, origin: {x: event.clientX, y: event.clientY}, source, port, moved: false};
        event.currentTarget.setPointerCapture?.(event.pointerId);
    };
    const consumePortClick = (detail: number) => {
        if (!suppressPortClick.current) return false;
        suppressPortClick.current = false;
        return detail > 0;
    };
    const [notice, setNotice] = useState('');
    const [past, setPast] = useState<Record<string, unknown>[]>([]);
    const [future, setFuture] = useState<Record<string, unknown>[]>([]);
    const trackedDocument = useRef(JSON.stringify(document));
    useEffect(() => {
        const next = JSON.stringify(document);
        if (trackedDocument.current !== next) {
            setPast([]);
            setFuture([]);
            setPendingSource(null);
            setPendingData(null);
            if (wireDrag.current?.moved) suppressPortClick.current = true;
            wireDrag.current = null;
            setWirePreview(null);
        }
        trackedDocument.current = next;
    }, [document]);
    const replace = (next: Record<string, unknown>) => { trackedDocument.current = JSON.stringify(next); onChange(next); };
    const drag = useRef<{
        id: string;
        origin: Point;
        cursor: Point;
    } | null>(null);
    const steps = useMemo(() => graphSteps(document), [document]);
    const wires = useMemo(() => graphWires(document), [document]);
    const bindings = useMemo(() => dataWires(document), [document]);
    const position = (id: string, index: number): Point => Object.hasOwn(positions, id) ? positions[id] : { x: 40 + (index % 3) * 300, y: 40 + Math.floor(index / 3) * rowHeight };
    const selectedStep = steps.find((step) => step.id === selected);
    const portsFor = (step: Record<string, unknown>, direction: 'input' | 'output', inspector = false) => {
        const operation = exactGraphOperation(step, operations);
        let schema = operation?.[direction === 'input' ? 'input_schema' : 'output_schema'];
        const request = isGraphRecord(step.request) ? step.request : {};
        const input = isGraphRecord(request.input) ? request.input : {};
        const properties = schema && isGraphRecord(schema.properties) ? schema.properties : {};
        const inputProperties = operation?.input_schema && isGraphRecord(operation.input_schema.properties) ? operation.input_schema.properties : {};
        const toolField = isGraphRecord(inputProperties.tool_id) ? inputProperties.tool_id : {};
        const tool = toolField['x-tobkiri-selector'] === 'tool-definition'
            ? toolSnapshot?.tools.find(item => item.toolId === input.tool_id && item.definitionHash === input.expected_definition_hash) : undefined;
        if (direction === 'input' && schema && tool && isGraphRecord(properties.arguments))
            schema = {...schema, properties:{...properties,arguments:{...tool.inputSchema,title:'引数','x-tobkiri-flow-role':'data'}}};
        if (direction === 'output' && schema && tool?.resultSchema && isGraphRecord(properties.result))
            schema = {...schema,properties:{...properties,result:{...tool.resultSchema,title:'結果','x-tobkiri-flow-role':'data'}}};
        const isConnected = (port: WorkflowPort) => bindings.some(wire => (
            direction === 'input'
                ? wire.target === step.id && JSON.stringify(wire.targetPath) === JSON.stringify(port.path)
                : wire.source === step.id && JSON.stringify(wire.sourcePath) === JSON.stringify(port.path)
        ));
        // Keep every existing wire visible even beyond the normal display cap.
        // schemaPorts itself is bounded, so connected ports cannot grow without limit.
        return schema ? schemaPorts(schema)
            .filter(port => port.path.length > 0 || operation?.whole_value_bindings === true)
            .filter(port => inspector ? port.role !== 'metadata' || port.required || detailedPorts : detailedPorts || (port.role === 'data' && (port.path.length <= 1 || (tool && port.path[0] === 'arguments' && port.path.length <= 2))) || isConnected(port))
            .filter((port, index) => index < 12 || isConnected(port)) : [];
    };
    const rowHeight = 210 + Math.max(0, ...steps.flatMap((step) => [portsFor(step, 'input').length, portsFor(step, 'output').length])) * 36;
    const selectedOperation = selectedStep ? exactGraphOperation(selectedStep, operations) : undefined;
    const commit = (next: Record<string, unknown>) => {
        if (disabled)
            return;
        setPast((items) => boundedGraphHistory([...items, document]));
        setFuture([]);
        setNotice('');
        replace(next);
    };
    const connect = (target: string) => {
        if (disabled || !pendingSource)
            return;
        try {
            commit(connectExecution(document, pendingSource, target));
            setPendingSource(null);
        }
        catch (error) {
            setNotice(error instanceof Error ? error.message : '接続できません');
        }
    };
    const wireData = (target: string, port: WorkflowPort) => {
        if (disabled || !pendingData)
            return;
        try {
            commit(connectData(document, pendingData.step, pendingData.port, target, port, Boolean(exactGraphOperation(steps.find(step=>step.id===pendingData.step)!,operations)?.json_pointer_bindings)));
            setPendingData(null);
        }
        catch (error) {
            setNotice(error instanceof Error ? error.message : '接続できません');
        }
    };
    const moveWireDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
        const current = wireDrag.current;
        if (!current || current.pointerId !== event.pointerId || disabled) return;
        if (!current.moved && Math.hypot(event.clientX - current.origin.x, event.clientY - current.origin.y) < 4) return;
        current.moved = true;
        if (current.port) { setPendingSource(null); setPendingData({step: current.source, port: current.port}); }
        else { setPendingData(null); setPendingSource(current.source); }
        const bounds = surface.current?.getBoundingClientRect();
        if (bounds) setWirePreview({
            start: current.start,
            end: {x: (event.clientX - bounds.left) / zoom, y: (event.clientY - bounds.top) / zoom},
        });
    };
    const finishWireDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
        const current = wireDrag.current;
        if (!current || current.pointerId !== event.pointerId) return;
        wireDrag.current = null;
        setWirePreview(null);
        if (!current.moved) return; // Preserve ordinary click and keyboard selection.
        suppressPortClick.current = true;
        setPendingData(null);
        setPendingSource(null);
        if (disabled) return;
        // Pointer capture keeps the event on the source. Hit-test the release
        // position instead, accepting only this canvas's declared input buttons.
        const target = event.currentTarget.ownerDocument.elementFromPoint?.(event.clientX, event.clientY)?.closest<HTMLButtonElement>('button[data-flow-input]');
        if (!target || !surface.current?.contains(target)) return;
        const targetId = target.dataset.flowStep;
        const step = steps.find(item => item.id === targetId);
        if (!step || !targetId) return;
        try {
            if (current.port && target.dataset.flowInput === 'data') {
                const port = portsFor(step, 'input').find(item => JSON.stringify(item.path) === target.dataset.flowPath);
                if (!port) { setNotice('接続先の端子が更新されました。もう一度選び直してください'); return; }
                if (!compatiblePorts(current.port, port)) { setNotice(`型が異なるため接続できません: ${workflowPortTypeLabel(current.port)} → ${workflowPortTypeLabel(port)}`); return; }
                const sourceStep = steps.find(item => item.id === current.source);
                if (!sourceStep) return;
                const sourcePort = portsFor(sourceStep, 'output').find(item => JSON.stringify(item.path) === JSON.stringify(current.port!.path));
                if (!sourcePort || !compatiblePorts(sourcePort, port)) { setNotice('端子の定義が更新されました。もう一度選び直してください'); return; }
                commit(connectData(document, current.source, sourcePort, targetId, port, Boolean(exactGraphOperation(sourceStep, operations)?.json_pointer_bindings)));
            } else if (!current.port && target.dataset.flowInput === 'execution') {
                commit(connectExecution(document, current.source, targetId));
            } else {
                setNotice('実行順序の端子とデータの端子は接続できません。同じ種類の端子を選んでください');
            }
        } catch (error) {
            setNotice(error instanceof Error ? error.message : '接続できません');
        }
    };
    const portCatalogKey = JSON.stringify([operations, toolSnapshot]);
    useEffect(() => { if (disabled) cancelWireDrag(); }, [disabled]);
    useEffect(() => {
        if (wireDrag.current?.moved) suppressPortClick.current = true;
        cancelWireDrag();
    }, [portCatalogKey]);
    const height = Math.max(560, ...steps.map((step, index) => position(String(step.id), index).y + 650));
    const width = Math.max(960, ...steps.map((step, index) => position(String(step.id), index).x + 290));
    const fitCanvas = () => {
        const available = viewport.current?.clientWidth ?? 0;
        if (available > 0) setZoom(Math.max(.35, Math.min(1, (available - 24) / width)));
        viewport.current?.scrollTo?.({ left: 0, top: 0 });
    };
    const filtered = operations.filter((operation) => `${operation.operation_id} ${operation.contract_id}`.toLowerCase().includes(query.toLowerCase()));
    return <section aria-label="Flow node editor" className="overflow-hidden rounded-xl border border-border bg-bg-main">
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border p-3">
      <div><h3 className="flex items-center gap-2 text-sm font-medium"><Cable className="h-4 w-4" aria-hidden="true"/>ノードをつないでFlowを作る</h3><p className="mt-1 text-xs text-text-muted">左から追加 → 出力端子から入力端子へドラッグ。順にクリックしても接続できます</p></div>
      <div className="flex max-w-full flex-wrap items-center gap-1" role="group" aria-label="Canvas tools">
        <Button size="icon" variant="ghost" aria-label="Undo graph edit" disabled={disabled || !past.length} onClick={() => { setFuture((items) => boundedGraphHistory([...items].reverse().concat(document)).reverse()); replace(past.at(-1)!); setPast((items) => items.slice(0, -1)); }}><Undo2 className="h-4 w-4"/></Button>
        <Button size="icon" variant="ghost" aria-label="Redo graph edit" disabled={disabled || !future.length} onClick={() => { setPast((items) => boundedGraphHistory([...items, document])); replace(future[0]); setFuture((items) => items.slice(1)); }}><Redo2 className="h-4 w-4"/></Button>
        <Button size="icon" variant="ghost" aria-label="Zoom out" disabled={zoom <= .35} onClick={() => setZoom((value) => Math.max(.35, value - .1))}><Minus className="h-4 w-4"/></Button>
        <output className="w-12 text-center text-xs" aria-label="Canvas zoom">{Math.round(zoom * 100)}%</output>
        <Button size="icon" variant="ghost" aria-label="Zoom in" disabled={zoom >= 1.5} onClick={() => setZoom((value) => Math.min(1.5, value + .1))}><Plus className="h-4 w-4"/></Button>
        <Button size="sm" variant="ghost" aria-label="Show detailed ports" aria-pressed={detailedPorts} onClick={() => setDetailedPorts(value => !value)}>詳細端子</Button>
        <Button size="sm" variant="ghost" aria-label="Fit canvas to view" onClick={fitCanvas}><Maximize2 className="h-4 w-4"/>全体を見る</Button>
        <Button size="sm" variant="ghost" aria-label="Reset canvas layout" onClick={() => { positionsRef.current = {}; setPositions({}); persistLayout(); setZoom(1); }}>整列</Button>
      </div>
    </div>
    <div className="flex flex-wrap gap-x-4 gap-y-1 border-b border-border px-4 py-2 text-xs text-text-muted" aria-label="Port type legend">
      <span>◇ 実行順序</span><span className="text-emerald-500">● 文字</span><span className="text-cyan-500">● 数値</span><span className="text-rose-500">● 真偽値</span><span className="text-violet-500">● オブジェクト</span><span className="text-amber-500">● 配列</span>
    </div>
    <div className={`grid min-h-[560px] ${selectedStep ? 'lg:grid-cols-[190px_minmax(0,1fr)_260px]' : 'lg:grid-cols-[190px_minmax(0,1fr)]'}`}>
      <aside className="border-b border-border p-3 lg:border-b-0 lg:border-r" aria-label="Node palette">
        <label className="mb-3 flex items-center gap-2 rounded-lg border border-border p-2"><Search className="h-4 w-4 shrink-0 text-text-muted" aria-hidden="true"/><input aria-label="Search nodes" value={query} onChange={(event) => setQuery(event.target.value)} className="min-w-0 w-full bg-transparent text-sm outline-none" placeholder="ノードを検索"/></label>
        <Button className="mb-3 w-full" variant="outline" size="sm" disabled={disabled || steps.length > 0 || !voiceWorkflowOperations(operations)} title="音声・テキスト・読み上げの操作がこのProfileに揃っている場合に使えます" onClick={() => commit(createVoiceWorkflow(operations))}>音声会話のFlowを作る</Button>
        {!voiceWorkflowOperations(operations) && <p className="mb-3 text-xs leading-5 text-text-muted">音声テンプレートには、このProfileで使える音声認識・テキスト生成・読み上げの3つの操作が必要です</p>}
        <div className="max-h-[480px] space-y-2 overflow-y-auto">
          {filtered.map((operation) => <button key={canvasOperationKey(operation)} disabled={disabled} type="button" className="w-full rounded-lg border border-border px-3 py-3 text-left hover:bg-bg-hover focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50" aria-label={`Add node ${operation.contract_id} / ${operation.operation_id}`} onClick={() => commit(insertPaletteWorkflowStep(document, operation))}>
            <span className="flex items-center justify-between gap-2 text-sm font-medium"><span className="break-all">{operationTitle(operation)}</span><Plus className="h-4 w-4 shrink-0" aria-hidden="true"/></span><span className="mt-1 block break-all text-xs text-text-muted">{operation.contract_id}</span>
          </button>)}
          {!filtered.length && <p className="text-sm text-text-muted">{operations.length ? '一致するノードがありません' : 'このProfileで利用できる操作を取得すると、ここに表示されます'}</p>}
        </div>
      </aside>
      <div ref={viewport} className="relative min-w-0 overflow-auto bg-bg-subtle" style={{ maxHeight: 720 }} aria-label="Node canvas" onKeyDown={(event) => { if (event.key === 'Escape') {
        cancelWireDrag();
        suppressPortClick.current = true;
        setNotice('');
    } }}>
        <div className="relative" style={{ width: width * zoom, height: height * zoom }}>
          <div ref={surface} className="absolute origin-top-left" style={{ width, height, transform: `scale(${zoom})`, backgroundImage: 'radial-gradient(var(--border) 1px, transparent 1px)', backgroundSize: '20px 20px' }} onPointerMove={(event) => { moveWireDrag(event); if (!drag.current)
        return; const move = drag.current; const next={ ...positionsRef.current, [move.id]: { x: Math.min(20000,Math.max(16, move.origin.x + (event.clientX - move.cursor.x) / zoom)), y: Math.min(20000,Math.max(16, move.origin.y + (event.clientY - move.cursor.y) / zoom)) } }; positionsRef.current=next;setPositions(next); }} onLostPointerCapture={(event) => { if (wireDrag.current?.pointerId === event.pointerId) { suppressPortClick.current = wireDrag.current.moved; cancelWireDrag(); } }} onPointerUp={(event) => { finishWireDrag(event); drag.current = null;persistLayout(); }} onPointerCancel={(event) => { if (wireDrag.current && wireDrag.current.pointerId !== event.pointerId) return; cancelWireDrag(); suppressPortClick.current = true; drag.current = null;persistLayout(); }}>
            <svg className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden="true">{wirePreview && <path data-flow-preview="true" d={`M ${wirePreview.start.x} ${wirePreview.start.y} L ${wirePreview.end.x} ${wirePreview.end.y}`} fill="none" stroke="var(--accent)" strokeWidth="3" strokeDasharray="6 4"/>}{wires.map((wire) => {
            const a = steps.findIndex((step) => step.id === wire.source), b = steps.findIndex((step) => step.id === wire.target);
            if (a < 0 || b < 0)
                return null;
            const start = position(wire.source, a), end = position(wire.target, b);
            return <path key={`${wire.source}:${wire.target}`} d={`M ${start.x + 250} ${start.y + 99} C ${start.x + 320} ${start.y + 99}, ${end.x - 70} ${end.y + 99}, ${end.x} ${end.y + 99}`} fill="none" stroke="var(--accent)" strokeWidth="2.5"/>;
        })}{bindings.map((wire) => {
            const a = steps.findIndex((step) => step.id === wire.source), b = steps.findIndex((step) => step.id === wire.target);
            if (a < 0 || b < 0)
                return null;
            const start = position(wire.source, a), end = position(wire.target, b);
            const out = portsFor(steps[a], 'output').findIndex((port) => JSON.stringify(port.path) === JSON.stringify(wire.sourcePath));
            const input = portsFor(steps[b], 'input').findIndex((port) => JSON.stringify(port.path) === JSON.stringify(wire.targetPath));
            if (out < 0 || input < 0)
                return null;
            const sy = start.y + 166 + out * 36, ty = end.y + 166 + input * 36;
            return <path key={JSON.stringify(wire)} d={`M ${start.x + 250} ${sy} C ${start.x + 310} ${sy}, ${end.x - 60} ${ty}, ${end.x} ${ty}`} fill="none" stroke="var(--success, #438c69)" strokeWidth="2.5"/>;
        })}</svg>
            {!steps.length && <div className="absolute left-10 top-12 max-w-sm rounded-xl border border-dashed border-border bg-bg-main p-6"><p className="text-base font-medium">最初のノードを追加しよう</p><p className="mt-2 text-sm leading-6 text-text-muted">使いたい処理を左から選び、端子をつないで順番を決めます。ノードを選ぶと右側に設定が表示されます</p></div>}
            {steps.map((step, index) => {
            const id = String(step.id), point = position(id, index), operation = exactGraphOperation(step, operations);
            const inputs = portsFor(step, 'input'), outputs = portsFor(step, 'output');
            const error = pendingSource ? connectionError(document, pendingSource, id) : null;
            return <article key={`${id}:${index}`} aria-label={`Node ${id}`} className={`absolute w-[250px] rounded-xl border bg-bg-main shadow-sm ${selected === id ? 'border-accent ring-1 ring-accent' : 'border-border'}`} style={{ left: point.x, top: point.y }}>
                <button type="button" className="w-full touch-none rounded-t-xl border-b border-border px-4 py-3 text-left focus-visible:ring-2 focus-visible:ring-accent" aria-label={`Select node ${id}`} onClick={() => setSelected(id)} onPointerDown={(event) => {
                    if (event.button !== 0)
                        return;
                    setSelected(id);
                    drag.current = { id, origin: point, cursor: { x: event.clientX, y: event.clientY } };
                    event.currentTarget.setPointerCapture?.(event.pointerId);
                }}><span className="block truncate text-sm font-semibold">{typeof step.label === 'string' ? step.label : operation ? operationTitle(operation) : id}</span><span className="mt-1 block truncate text-xs text-text-muted">{id}</span></button>
                <div className="flex items-center justify-between px-1 py-3">
                  <button type="button" className="flex min-h-11 items-center gap-2 rounded-md px-2 text-xs hover:bg-bg-hover disabled:opacity-40" data-flow-input="execution" data-flow-step={id} aria-label={`Connect execution to ${id}`} disabled={disabled || !pendingSource || Boolean(error)} title={error ?? '前のノードが終わったら実行'} onClick={() => connect(id)}><span className="h-3 w-3 rotate-45 border-2 border-accent"/>実行入力</button>
                  <button type="button" className="flex min-h-11 touch-none items-center gap-2 rounded-md px-2 text-xs hover:bg-bg-hover disabled:opacity-40" onPointerDown={(event) => beginWireDrag(event, id)} aria-label={`Connect execution from ${id}`} aria-pressed={pendingSource === id} disabled={disabled} onClick={(event) => { if (consumePortClick(event.detail)) return; setPendingData(null); setPendingSource(pendingSource === id ? null : id); setNotice(''); }}>完了<span className={`h-3 w-3 rotate-45 border-2 border-accent ${pendingSource === id ? 'bg-accent' : ''}`}/></button>
                </div>
                {(inputs.length > 0 || outputs.length > 0) && <div className="grid grid-cols-2 border-t border-border px-1 py-2">
                  <div>{inputs.map((port) => <button key={port.label} type="button" className="flex h-9 w-full items-center gap-1 px-1 text-left text-xs disabled:opacity-40" data-flow-input="data" data-flow-step={id} data-flow-path={JSON.stringify(port.path)} aria-label={`Connect data to ${id}.${port.label}`} title={`${port.label}: ${workflowPortTypeLabel(port)}`} disabled={disabled || !pendingData || !compatiblePorts(pendingData.port, port) || Boolean(connectionError(document, pendingData.step, id))} onClick={() => wireData(id, port)}><span className={`h-2.5 w-2.5 shrink-0 rounded-full border-2 ${portTone(port)}`}/><span className="truncate">{workflowPortTitle(port)}</span><span className="sr-only">{workflowPortTypeLabel(port)}</span></button>)}</div>
                  <div>{outputs.map((port) => <button key={port.label} type="button" className={`flex h-9 w-full touch-none items-center justify-end gap-1 rounded px-1 text-right text-xs disabled:opacity-40 ${pendingData?.step === id && pendingData.port.label === port.label ? 'bg-accent/15 ring-2 ring-inset ring-accent' : 'hover:bg-bg-hover'}`} onPointerDown={(event) => beginWireDrag(event, id, port)} aria-label={`Connect data from ${id}.${port.label}`} title={`${port.label}: ${workflowPortTypeLabel(port)}`} aria-pressed={pendingData?.step === id && pendingData.port.label === port.label} disabled={disabled || port.kind === 'unknown'} onClick={(event) => { if (consumePortClick(event.detail)) return; setPendingSource(null); setPendingData({ step: id, port }); setNotice(''); }}><span className="truncate">{workflowPortTitle(port)}</span><span className="sr-only">{workflowPortTypeLabel(port)}</span><span className={`h-2.5 w-2.5 shrink-0 rounded-full border-2 ${portTone(port)}`}/></button>)}</div>
                </div>}
                <p className="truncate px-4 pb-3 text-xs text-text-muted">{operation?.contract_id ?? '操作が現在のパレットにありません'}</p>
              </article>;
        })}
          </div>
        </div>
      </div>
      {selectedStep && <aside className="min-w-0 border-t border-border p-4 lg:border-l lg:border-t-0" aria-label="Node inspector">
        <Button size="icon" variant="ghost" className="float-right" aria-label="Close node inspector" onClick={() => setSelected(null)}><X className="h-4 w-4"/></Button>
        {selectedStep ? <><h4 className="break-all text-sm font-semibold">{String(selectedStep.id)}</h4><p className="mt-1 break-all text-xs text-text-muted">{selectedOperation?.contract_id ?? '未解決の操作'}</p>
          <label className="mt-4 block text-xs">ノード名<input aria-label="Node display name" className="mt-1 min-h-10 w-full rounded border border-border bg-bg-main px-2 text-sm" maxLength={128} disabled={disabled} value={typeof selectedStep.label === 'string' ? selectedStep.label : ''} placeholder={selectedOperation ? operationTitle(selectedOperation) : String(selectedStep.id)} onChange={event => commit(labelGraphNode(document, String(selectedStep.id), event.target.value))}/></label>
          <div className="mt-4 space-y-3">{portsFor(selectedStep, 'input', true).filter((port) => ['string', 'number', 'integer', 'boolean', 'object', 'array', 'json', 'union', 'null'].includes(port.kind)).map((port) => {
                let value: unknown = isGraphRecord(selectedStep.request) ? selectedStep.request.input : {};
                for (const part of port.path)
                    value = isGraphRecord(value) ? value[part] : undefined;
                const initialInput = typeof value === 'string' && value.startsWith('${inputs.');
                const bound = typeof value === 'string' && (value.startsWith('${steps.') || initialInput);
                return <label key={port.label} className="block text-xs">{workflowPortTitle(port)}{port.required ? ' *' : ''} · {port.role === 'configuration' ? 'ノード設定' : workflowPortTypeLabel(port)}
              {bound ? <><span className="mt-1 block break-all rounded border border-border p-2 text-text-muted">{initialInput ? '開始時の入力から設定: ' : ''}{String(value)}</span><button type="button" className="mt-1 min-h-9 text-accent" disabled={disabled} onClick={() => commit(setGraphInput(document, String(selectedStep.id), port.path, undefined))}>データ接続を解除</button></> : port.schema['x-tobkiri-selector'] === 'tool-definition' && port.path.length === 1 && port.path[0] === 'tool_id' ? <RegisteredToolPicker profileId={profileId} planDigest={planDigest} load={loadTools} value={typeof value === 'string' ? value : ''} expectedHash={isGraphRecord(selectedStep.request) && isGraphRecord(selectedStep.request.input) && typeof selectedStep.request.input.expected_definition_hash === 'string' ? selectedStep.request.input.expected_definition_hash : undefined} disabled={disabled} contextKey={toolContext + canvasOperationKey(selectedOperation!)} onSnapshot={snapshot=>setToolState({context:toolContext,snapshot})} onChange={tool=>{
                  let next = setGraphInput(document,String(selectedStep.id),port.path,tool?.toolId);
                  // Only the compound top-level tool selector owns these
                  // sibling pins. A nested selector edits its own scalar field.
                  if (port.path.length === 1 && port.path[0] === 'tool_id') {
                    next = setGraphInput(next,String(selectedStep.id),['expected_definition_hash'],tool?.definitionHash);
                    const request = isGraphRecord(selectedStep.request) ? selectedStep.request : {};
                    const input = isGraphRecord(request.input) ? request.input : {};
                    if (tool && !input.tool_call_id) next = setGraphInput(next,String(selectedStep.id),['tool_call_id'],`flow.${String(selectedStep.id)}`);
                  }
                  commit(next);
              }}/> : (port.schema['x-tobkiri-selector'] === 'model-profile' || (Array.isArray(port.schema['x-tobkiri-selector']) && port.schema['x-tobkiri-selector'].includes('model-profile'))) ? <RegisteredModelPicker value={typeof value === 'string' ? value : ''} disabled={disabled} contextKey={canvasOperationKey(selectedOperation!)} onChange={(id) => commit(setGraphInput(document, String(selectedStep.id), port.path, id))}/> : <WorkflowLiteralInput port={port} value={value} disabled={disabled} onChange={next => commit(setGraphInput(document, String(selectedStep.id), port.path, next))}/>}</label>;
            })}</div>
          <div className="mt-5 space-y-2"><p className="text-xs font-medium">実行順序の接続</p>{wires.filter((wire) => wire.target === selected || wire.source === selected).map((wire) => <div key={`${wire.source}:${wire.target}`} className="flex items-center gap-1 text-xs"><span className="min-w-0 flex-1 break-all">{wire.source} → {wire.target}</span><Button size="icon" variant="ghost" aria-label={`Disconnect ${wire.source} to ${wire.target}`} disabled={disabled} onClick={() => commit(disconnectExecution(document, wire.source, wire.target))}><Minus className="h-3 w-3"/></Button></div>)}</div>
          <Button className="mt-5" variant="destructive" size="sm" disabled={disabled} onClick={() => { commit(deleteGraphNode(document, String(selectedStep.id))); setSelected(null); setPendingSource(null); }}><Trash2 className="h-3 w-3"/>ノードを削除</Button>
        </> : <p className="text-sm leading-6 text-text-muted">ノードを選ぶと設定と接続が表示されます。見出しをドラッグすると移動できます</p>}
      </aside>}
    </div>
    <div className="min-h-11 border-t border-border px-4 py-3 text-xs text-text-muted" role="status">{notice || (pendingData ? `${pendingData.step}.${pendingData.port.label} (${workflowPortTypeLabel(pendingData.port)}) → 対応する入力端子を選択` : pendingSource ? `${pendingSource}から接続する入力端子を選択。Escでキャンセル` : `${steps.length}ノード · ${wires.length}接続 · 実行にはHostの検証と承認が必要です`)}</div>
  </section>;
}
