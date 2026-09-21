import type {ComponentType} from 'react';
import {
  Bot,
  Boxes,
  Braces,
  Database,
  GitBranch,
  Globe2,
  LayoutTemplate,
  MessageSquareText,
  Radio,
  Workflow,
} from 'lucide-react';

import type {ApiProfileGraphEdge, ApiProfileGraphNode} from '@/src/lib/apiTypes';
import {graphNodeKindLabel} from '@/src/lib/profileGraph';
import {edgePath, layoutProfileGraph, profileGraphDisplayPorts} from '@/src/lib/profileGraphLayout';
import {cn} from '@/src/lib/utils';

interface ProfileGraphCanvasProps {
  nodes: ApiProfileGraphNode[];
  edges: ApiProfileGraphEdge[];
  selectedNodeId?: string | null;
  onSelectNode?: (nodeId: string) => void;
  emptyMessage?: string;
}

type NodeVisual = {icon: ComponentType<{className?: string}>; accent: string; dot: string};

const NODE_VISUALS: Record<string, NodeVisual> = {
  profile: {icon: Bot, accent: 'border-border bg-bg-card', dot: 'bg-accent'},
  prompt: {icon: MessageSquareText, accent: 'border-border bg-bg-card', dot: 'bg-fuchsia-400'},
  tool: {icon: Boxes, accent: 'border-border bg-bg-card', dot: 'bg-emerald-400'},
  webhook: {icon: Radio, accent: 'border-border bg-bg-card', dot: 'bg-orange-400'},
  api: {icon: Globe2, accent: 'border-border bg-bg-card', dot: 'bg-amber-400'},
  frontend: {icon: LayoutTemplate, accent: 'border-border bg-bg-card', dot: 'bg-sky-400'},
  flow: {icon: Workflow, accent: 'border-border bg-bg-card', dot: 'bg-teal-400'},
  storage: {icon: Database, accent: 'border-border bg-bg-card', dot: 'bg-slate-400'},
  node: {icon: Braces, accent: 'border-border bg-bg-card', dot: 'bg-text-muted'},
};

export function ProfileGraphCanvas({
  nodes,
  edges,
  selectedNodeId,
  onSelectNode,
  emptyMessage = 'No nodes selected yet. Use the + buttons to wire this profile.',
}: ProfileGraphCanvasProps) {
  if (!nodes.length) {
    return (
      <div className="flex min-h-[520px] flex-col items-center justify-center rounded-xl border border-dashed border-border bg-bg-card/60 p-6 text-center text-sm text-text-muted">
        <span className="mb-4 grid h-11 w-11 place-items-center rounded-lg border border-border bg-bg-main"><GitBranch className="h-5 w-5" /></span>
        {emptyMessage}
      </div>
    );
  }

  const layout = layoutProfileGraph(nodes, edges);
  const positions = new Map(layout.nodes.map((node) => [node.id, node]));
  const selectedEdgeIds = new Set(edges
    .filter((edge) => edge.from_id === selectedNodeId || edge.to_id === selectedNodeId)
    .map((edge) => edge.id));
  const connectedNodeIds = new Set(edges.flatMap((edge) => {
    if (edge.from_id === selectedNodeId) return [edge.to_id];
    if (edge.to_id === selectedNodeId) return [edge.from_id];
    return [];
  }));
  return (
    <div className="group relative h-full min-h-[360px] overflow-auto rounded-xl border border-border bg-bg-main">
      <div className="pointer-events-none sticky left-0 top-0 z-10 flex h-0 w-full justify-between px-3 pt-3">
        <div className="flex items-center gap-2 rounded-lg border border-border bg-bg-card px-3 py-2 text-[10px] text-text-muted shadow-sm">
          <GitBranch className="h-3.5 w-3.5 text-accent" />
          <span>{nodes.length} nodes</span><span className="text-border">·</span><span>{edges.filter((edge) => edge.active).length} active edges</span>
        </div>
        <div className="hidden items-center gap-2 rounded-lg border border-border bg-bg-card px-3 py-2 text-[10px] text-text-muted shadow-sm sm:flex">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" /> Tool
          <span className="h-1.5 w-1.5 rounded-full bg-sky-400" /> UI
          <span className="h-1.5 w-1.5 rounded-full bg-fuchsia-400" /> Prompt
        </div>
      </div>
      <div
        className="relative bg-[radial-gradient(circle,color-mix(in_srgb,var(--text-muted)_18%,transparent)_1px,transparent_1px)] [background-size:24px_24px]"
        style={{width: layout.width, height: layout.height}}
        onClick={(event) => {
          if (event.target === event.currentTarget) onSelectNode?.('');
        }}
      >
        <svg width={layout.width} height={layout.height} viewBox={`0 0 ${layout.width} ${layout.height}`} className="pointer-events-none absolute left-0 top-0 overflow-visible">
          <defs>
            <filter id="edge-glow" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="2.4" result="blur" /><feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge></filter>
          </defs>
          {edges.map((edge) => {
            const path = edgePath(edge, positions, edges);
            if (!path) return null;
            const selected = selectedEdgeIds.has(edge.id);
            const dimmed = Boolean(selectedNodeId) && !selected;
            return (
              <g key={edge.id} opacity={dimmed ? .12 : edge.active ? .82 : .26} className="transition-opacity duration-200">
                <path d={path} fill="none" stroke="var(--bg-main)" strokeWidth={selected ? 7 : 5} />
                <path
                  d={path}
                  fill="none"
                  stroke={edgeStroke(edge)}
                  strokeWidth={selected ? 2.8 : 1.8}
                  strokeDasharray={edge.kind === 'fallback' ? '5 5' : undefined}
                  strokeLinecap="round"
                  filter={selected ? 'url(#edge-glow)' : undefined}
                />
              </g>
            );
          })}
        </svg>

        {layout.nodes.map((node) => {
          const visual = nodeVisual(node);
          const Icon = visual.icon;
          const selected = selectedNodeId === node.id;
          const dimmed = Boolean(selectedNodeId) && !selected && !connectedNodeIds.has(node.id);
          const ports = profileGraphDisplayPorts(node, edges);
          const inputPorts = ports.filter((port) => port.direction === 'input');
          const outputPorts = ports.filter((port) => port.direction === 'output');
          return (
            <button
              key={node.id}
              type="button"
              className={cn(
                'absolute overflow-visible rounded-xl border px-3 py-2.5 text-left shadow-sm transition-colors duration-150 hover:border-text-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                visual.accent,
                selected && 'z-[2] border-accent/60 ring-2 ring-accent/30 shadow-md',
                dimmed && 'opacity-35 saturate-50',
              )}
              style={{left: node.x, top: node.y, width: node.width, height: node.height}}
              onClick={() => onSelectNode?.(node.id)}
            >
              <div className="flex items-center gap-2.5">
                <span className={cn('grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-current/15 bg-bg-main/55', selected ? 'text-accent' : 'text-text-muted')}><Icon className="h-4 w-4" /></span>
                <span className="min-w-0">
                  <span className="block truncate text-[9px] font-semibold uppercase tracking-[0.12em] text-text-muted">{graphNodeKindLabel(node)}</span>
                  <span className="mt-0.5 block truncate text-xs font-semibold text-text-main">{node.label || node.ref || node.id}</span>
                  <span className="block truncate font-mono text-[9px] text-text-muted/80">{node.ref || node.id}</span>
                </span>
              </div>
              {inputPorts.map((port, index) => (
                <span key={`in:${port.id}`} className="absolute left-0 flex max-w-[48%] -translate-x-1.5 items-center gap-1.5" style={{top: 52 + index * 18}}>
                  <span className="h-2.5 w-2.5 shrink-0 rounded-full border-2 border-bg-main bg-text-muted" />
                  <span className="truncate rounded bg-bg-card px-1 text-[8px] text-text-muted">{port.label}</span>
                </span>
              ))}
              {outputPorts.map((port, index) => (
                <span key={`out:${port.id}`} className="absolute right-0 flex max-w-[48%] translate-x-1.5 items-center justify-end gap-1.5" style={{top: 52 + index * 18}}>
                  <span className="truncate rounded bg-bg-card px-1 text-[8px] text-text-muted">{port.label}</span>
                  <span className={cn('h-2.5 w-2.5 shrink-0 rounded-full border-2 border-bg-main', visual.dot)} />
                </span>
              ))}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function nodeVisual(node: ApiProfileGraphNode): NodeVisual {
  const prefix = node.id.split(':', 1)[0];
  if (prefix === 'storage' || node.kind === 'storage') return NODE_VISUALS.storage;
  return NODE_VISUALS[prefix] || NODE_VISUALS.node;
}

function edgeStroke(edge: ApiProfileGraphEdge): string {
  if (edge.kind === 'uses_prompt') return '#d78cff';
  if (edge.kind === 'selects' || edge.kind === 'executes') return '#4adea8';
  if (edge.kind === 'receives_from' || edge.kind === 'delivers_to') return '#fb9b55';
  if (edge.kind === 'allows_api' || edge.kind === 'handled_by') return '#f7c74d';
  if (edge.kind === 'uses_frontend') return '#64b5ff';
  if (edge.kind === 'launches_flow' || edge.kind === 'launches_graph') return '#3dd6c2';
  if (edge.kind === 'fallback') return '#fb7185';
  return '#82909b';
}
