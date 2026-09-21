import type {ApiProfileGraphEdge, ApiProfileGraphNode} from './apiTypes';

export interface PositionedProfileGraphNode extends ApiProfileGraphNode {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface ProfileGraphDisplayPort {
  id: string;
  label: string;
  direction: 'input' | 'output';
}

const GROUP_ORDER = [
  'profile',
  'prompt',
  'tool',
  'webhook',
  'api',
  'frontend',
  'flow',
  'node',
  'storage',
] as const;

const GROUP_ANCHORS: Record<(typeof GROUP_ORDER)[number], {x: number; y: number}> = {
  profile: {x: 500, y: 320},
  prompt: {x: 500, y: 92},
  tool: {x: 170, y: 180},
  webhook: {x: 170, y: 420},
  api: {x: 830, y: 180},
  frontend: {x: 830, y: 420},
  flow: {x: 650, y: 588},
  node: {x: 350, y: 588},
  storage: {x: 500, y: 540},
};

const ROW_GAP = 96;
const COLUMN_GAP = 184;
const ROWS_PER_GROUP = 3;
const CANVAS_PADDING = 80;

export function layoutProfileGraph(
  nodes: ApiProfileGraphNode[],
  edges: ApiProfileGraphEdge[] = [],
): {nodes: PositionedProfileGraphNode[]; width: number; height: number} {
  const groups = new Map<(typeof GROUP_ORDER)[number], ApiProfileGraphNode[]>();
  for (const group of GROUP_ORDER) {
    groups.set(group, []);
  }

  for (const node of nodes) {
    groups.get(groupForNode(node))?.push(node);
  }

  const positioned: PositionedProfileGraphNode[] = [];
  for (const group of GROUP_ORDER) {
    const items = groups.get(group) || [];
    const anchor = GROUP_ANCHORS[group];
    items
      .sort((left, right) => (left.label || left.ref || left.id).localeCompare(right.label || right.ref || right.id))
      .forEach((node, index) => {
        const row = index % ROWS_PER_GROUP;
        const column = Math.floor(index / ROWS_PER_GROUP);
        const ports = profileGraphDisplayPorts(node, edges);
        const portRows = Math.max(
          ports.filter((port) => port.direction === 'input').length,
          ports.filter((port) => port.direction === 'output').length,
        );
        const width = group === 'profile' ? 220 : group === 'storage' ? 180 : 208;
        const height = Math.max(group === 'storage' ? 64 : 76, 62 + portRows * 18);
        positioned.push({
          ...node,
          x: anchor.x + column * COLUMN_GAP - width / 2,
          y: anchor.y + row * ROW_GAP - height / 2,
          width,
          height,
        });
      });
  }

  const maxX = Math.max(...positioned.map((node) => node.x + node.width), 0);
  const maxY = Math.max(...positioned.map((node) => node.y + node.height), 0);

  return {
    nodes: positioned,
    width: Math.max(1000, maxX + CANVAS_PADDING),
    height: Math.max(680, maxY + CANVAS_PADDING),
  };
}

export function edgePath(
  edge: ApiProfileGraphEdge,
  positions: Map<string, PositionedProfileGraphNode>,
  edges: ApiProfileGraphEdge[] = [],
): string {
  const from = positions.get(edge.from_id);
  const to = positions.get(edge.to_id);
  if (!from || !to) {
    return '';
  }
  const start = portAnchor(from, edge, 'output', edges);
  const end = portAnchor(to, edge, 'input', edges);
  const distance = Math.max(56, Math.abs(end.x - start.x) * 0.46);
  return `M ${start.x} ${start.y} C ${start.x + distance} ${start.y}, ${end.x - distance} ${end.y}, ${end.x} ${end.y}`;
}

function centerPoint(node: PositionedProfileGraphNode): {x: number; y: number} {
  return {
    x: node.x + node.width / 2,
    y: node.y + node.height / 2,
  };
}

function portAnchor(
  node: PositionedProfileGraphNode,
  edge: ApiProfileGraphEdge,
  direction: 'input' | 'output',
  edges: ApiProfileGraphEdge[],
): {x: number; y: number} {
  const ports = profileGraphDisplayPorts(node, edges).filter((port) => port.direction === direction);
  const edgePortId = direction === 'output' ? edge.from_port || edge.kind : edge.to_port || edge.kind;
  const foundIndex = ports.findIndex((port) => port.id === edgePortId);
  const index = foundIndex < 0 ? 0 : foundIndex;
  return {
    x: direction === 'output' ? node.x + node.width : node.x,
    y: ports.length ? node.y + 58 + index * 18 : centerPoint(node).y,
  };
}

export function profileGraphDisplayPorts(
  node: ApiProfileGraphNode,
  edges: ApiProfileGraphEdge[],
): ProfileGraphDisplayPort[] {
  const result: ProfileGraphDisplayPort[] = [];
  const seen = new Set<string>();
  const nestedMetadata = isRecord(node.metadata.metadata) ? node.metadata.metadata : {};
  const declared = Array.isArray(node.metadata.ports)
    ? node.metadata.ports
    : Array.isArray(nestedMetadata.ports)
      ? nestedMetadata.ports
      : [];

  declared.forEach((rawPort) => {
    if (!isRecord(rawPort)) return;
    const direction = rawPort.direction === 'input' ? 'input' : rawPort.direction === 'output' ? 'output' : null;
    if (!direction) return;
    const id = String(rawPort.id || rawPort.port_id || '').trim();
    if (!id) return;
    addPort(result, seen, {id, direction, label: localizedPortLabel(rawPort) || id});
  });

  edges.forEach((edge) => {
    if (edge.from_id === node.id) {
      const id = String(edge.from_port || edge.kind).trim();
      addPort(result, seen, {id, label: humanizePort(id), direction: 'output'});
    }
    if (edge.to_id === node.id) {
      const id = String(edge.to_port || edge.kind).trim();
      addPort(result, seen, {id, label: humanizePort(id), direction: 'input'});
    }
  });
  return result;
}

function addPort(result: ProfileGraphDisplayPort[], seen: Set<string>, port: ProfileGraphDisplayPort) {
  const key = `${port.direction}:${port.id}`;
  if (!port.id || seen.has(key)) return;
  seen.add(key);
  result.push(port);
}

function localizedPortLabel(port: Record<string, unknown>): string {
  if (typeof port.label === 'string') return port.label;
  if (!isRecord(port.display_name)) return '';
  const value = port.display_name.en || port.display_name.ja;
  return typeof value === 'string' ? value : '';
}

function humanizePort(value: string): string {
  return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function groupForNode(node: ApiProfileGraphNode): (typeof GROUP_ORDER)[number] {
  const prefix = node.id.split(':', 1)[0];
  if (prefix === 'profile') return 'profile';
  if (prefix === 'prompt') return 'prompt';
  if (prefix === 'tool') return 'tool';
  if (prefix === 'webhook') return 'webhook';
  if (prefix === 'api') return 'api';
  if (prefix === 'frontend') return 'frontend';
  if (prefix === 'flow') return 'flow';
  if (prefix === 'storage') return 'storage';
  if (prefix === 'node' && node.kind === 'storage') return 'storage';
  return 'node';
}
