import type { ReactNode } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { Play, Settings2, Square, CheckCircle2, XCircle, Loader2, Waypoints } from 'lucide-react';
import { cn } from '@/src/lib/utils';
import { useT } from '@/src/lib/i18n';
import type {
  EndNode as EndNodeType,
  ExecutionStatus,
  FlowPort,
  StepNode as StepNodeType,
  TriggerNode as TriggerNodeType,
} from '@/src/lib/types';

function statusAccent(status: ExecutionStatus): string {
  if (status === 'running') return 'is-running';
  if (status === 'success') return 'is-success';
  if (status === 'error') return 'is-error';
  return 'is-idle';
}

function localizePortLabel(port: FlowPort, t: ReturnType<typeof useT>): string {
  const keyById: Record<string, string> = {
    'start-out': 'flows.port.boot',
    'boot-in': 'flows.port.boot',
    'profile-in': 'flows.port.boot',
    'end-in': 'flows.port.done',
    'mounts-in': 'flows.port.mounts',
    'mounts-out': 'flows.port.mounts',
    'registry-in': 'flows.port.registry',
    'registry-out': 'flows.port.registry',
    'profile-out': 'flows.port.profile',
    'event-in': 'flows.port.event',
    'event-out': 'flows.port.signal',
    'exec-in': 'flows.port.command',
    'exec-out': 'flows.port.result',
    'http-in': 'flows.port.request',
    'http-out': 'flows.port.response',
    'http-post-in': 'flows.port.request',
    'http-post-out': 'flows.port.response',
    'log-in': 'flows.port.text',
    'log-out': 'flows.port.signal',
    'input-main': 'flows.port.input',
    'output-main': 'flows.port.output',
  };

  const translationKey = keyById[port.id];
  return translationKey ? t(translationKey) : port.label;
}

function localizeNodeTitle(value: string | undefined, fallbackKey: string, t: ReturnType<typeof useT>): string {
  if (!value) {
    return t(fallbackKey);
  }

  if (fallbackKey === 'flows.node.start_title' && value === 'rumi_start') {
    return t(fallbackKey);
  }
  if (fallbackKey === 'flows.node.finish_title' && value === 'finish') {
    return t(fallbackKey);
  }
  return value;
}

function localizeNodeKind(value: string | undefined, t: ReturnType<typeof useT>): string {
  if (!value || value === 'action') {
    return t('flows.node.action');
  }
  return value;
}

function PortHandle({ port, side }: { port: FlowPort; side: 'left' | 'right' }) {
  const t = useT();
  const position = side === 'left' ? Position.Left : Position.Right;
  const type = side === 'left' ? 'target' : 'source';

  return (
    <div className={cn('flow-port-item', side === 'left' ? 'flow-port-item-left' : 'flow-port-item-right')}>
      {side === 'left' && (
        <Handle
          id={port.id}
          type={type}
          position={position}
          className="flow-port-handle !border-2 !shadow-none"
        />
      )}
      <div className="flow-port-tag">
        <span className="truncate">{localizePortLabel(port, t)}</span>
      </div>
      {side === 'right' && (
        <Handle
          id={port.id}
          type={type}
          position={position}
          className="flow-port-handle !border-2 !shadow-none"
        />
      )}
    </div>
  );
}

function PortStack({ ports, side }: { ports: FlowPort[]; side: 'left' | 'right' }) {
  if (ports.length === 0) return null;

  return (
    <div className={cn('flow-port-stack', side === 'left' ? 'flow-port-stack-left' : 'flow-port-stack-right')}>
      {ports.map((port) => (
        <PortHandle key={port.id} port={port} side={side} />
      ))}
    </div>
  );
}

function NodeShell({
  title,
  subtitle,
  tokenTitle,
  tokenSubtitle,
  status,
  selected,
  icon,
  ports,
}: {
  title: string;
  subtitle?: string;
  tokenTitle: string;
  tokenSubtitle?: string;
  status: ExecutionStatus;
  selected: boolean;
  icon: ReactNode;
  ports: FlowPort[];
}) {
  const inputPorts = ports.filter((port) => port.direction === 'input');
  const outputPorts = ports.filter((port) => port.direction === 'output');

  return (
    <div
      className={cn(
        'flow-node-shell min-w-[220px] transition-transform duration-150',
        selected && 'is-selected scale-[1.01]',
        statusAccent(status),
      )}
    >
      <div className="flow-node-card">
        <div className="flow-node-cap">
          <div className="flex items-center gap-2 truncate">
            {icon}
            <span className="truncate">{title}</span>
          </div>
          <span className="flow-node-status-dot" aria-hidden="true" />
        </div>
        <PortStack ports={inputPorts} side="left" />
        <PortStack ports={outputPorts} side="right" />

        <div className="flow-node-body">
          <div className="flow-node-core">
            {subtitle ? <div className="flow-node-subtitle">{subtitle}</div> : null}
            <div className="flow-node-token">
              <div className="flow-node-token-title">{tokenTitle}</div>
              {tokenSubtitle ? <div className="flow-node-token-subtext">{tokenSubtitle}</div> : null}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export function TriggerNode({ data, selected }: NodeProps<TriggerNodeType>) {
  const t = useT();
  const status = data.executionStatus;
  const icon = status === 'running'
    ? <Loader2 className="h-4 w-4 animate-spin" />
    : status === 'success'
      ? <CheckCircle2 className="h-4 w-4" />
      : <Play className="h-4 w-4" />;
  const startPort = (data.ports || []).find((port) => port.direction === 'output');

  return (
    <NodeShell
      title={localizeNodeTitle(data.title, 'flows.node.start_title', t)}
      subtitle={`${t('flows.node.basepack')}: ${(data.basePack || 'basepack').toUpperCase()}`}
      tokenTitle={startPort ? localizePortLabel(startPort, t) : t('flows.port.boot')}
      tokenSubtitle={startPort?.contracts[0] || String(data.type || 'flow.start')}
      status={status}
      selected={selected}
      icon={icon}
      ports={data.ports || []}
    />
  );
}

export function StepNode({ data, selected }: NodeProps<StepNodeType>) {
  const t = useT();
  if (data.type === 'reroute') {
    return (
      <div className={cn(
        'flow-reroute-node rounded-full border p-2',
        selected && 'ring-2',
      )}>
        <Handle id="reroute-in" type="target" position={Position.Left} className="flow-port-handle !border-2 !shadow-none" />
        <Waypoints className="h-4 w-4" />
        <Handle id="reroute-out" type="source" position={Position.Right} className="flow-port-handle !border-2 !shadow-none" />
      </div>
    );
  }

  let icon: ReactNode = <Settings2 className="h-4 w-4" />;
  if (data.executionStatus === 'running') icon = <Loader2 className="h-4 w-4 animate-spin" />;
  if (data.executionStatus === 'success') icon = <CheckCircle2 className="h-4 w-4" />;
  if (data.executionStatus === 'error') icon = <XCircle className="h-4 w-4" />;

  return (
    <NodeShell
      title={data.title || data.id || 'step'}
      subtitle={`${localizeNodeKind(data.type, t)}${data.phase ? ` / ${data.phase}` : ''}`}
      tokenTitle={data.id || 'step'}
      tokenSubtitle={data.description || t('flows.node.configured_step')}
      status={data.executionStatus}
      selected={selected}
      icon={icon}
      ports={data.ports || []}
    />
  );
}

export function EndNode({ data, selected }: NodeProps<EndNodeType>) {
  const t = useT();
  const icon = data.executionStatus === 'success'
    ? <CheckCircle2 className="h-4 w-4" />
    : <Square className="h-4 w-4" />;
  const endPort = (data.ports || []).find((port) => port.direction === 'input');

  return (
    <NodeShell
      title={localizeNodeTitle(data.title, 'flows.node.finish_title', t)}
      subtitle={t('flows.node.terminal')}
      tokenTitle={endPort ? localizePortLabel(endPort, t) : t('flows.port.done')}
      tokenSubtitle="flow.complete"
      status={data.executionStatus}
      selected={selected}
      icon={icon}
      ports={data.ports || []}
    />
  );
}

export const nodeTypes = {
  trigger: TriggerNode,
  step: StepNode,
  end: EndNode,
} as const;
