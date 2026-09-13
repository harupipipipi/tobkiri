import {
  Bot,
  BookOpen,
  ChevronDown,
  ClipboardCheck,
  Code2,
  Crown,
  FileText,
  FlaskConical,
  FolderTree,
  Hash,
  History,
  Inbox,
  MessageCircle,
  MessageSquare,
  Network,
  RefreshCw,
  Send,
  Search,
  ShieldCheck,
  UserRound,
  UsersRound,
  type LucideIcon,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";

import {
  api,
  arrayFromRecord,
  type CompanyAgent,
  type CompanyChannel,
  type CompanyInboxItem,
  type CompanyMessage,
  type CompanyRecord,
  type CompanyRunLink,
  type CompanyTask,
  type SubagentTeamCreatorSettings,
  type SubagentTeamCreatorTestResponse,
  type SubagentTeamDecisionPreviewResponse,
} from "../lib/api";
import { cn } from "../lib/cn";
import { ErrorNotice } from "../components/ErrorNotice";
import {
  agentName,
  agentRoleKey,
  agentShortId,
  buildAgentActivity,
  channelMemberCount,
  channelName,
  channelUnreadCount,
  fallbackSubagentOpenPreview,
  fallbackSubagentTreeState,
  hasSubagentTeamWorkspaceMarker,
  messageTime,
  messageTimestamp,
  normalizeSubagentOpenPreview,
  normalizeSubagentTreeResponse,
  previewAgents,
  previewChannels,
  previewCompany,
  previewInbox,
  previewMessages,
  previewRuns,
  previewTasks,
  removeReconciledLocalSubagentMessages,
  shortId,
  subagentTeamPreviewDataReason,
  subagentTreeItemsForMode,
  subagentTeamWorkspaceMetadata,
  type AgentActivity,
  type SubagentOpenPreview,
  type SubagentThread,
  type SubagentTreeItem,
  type SubagentTreeMode,
  type SubagentTreeState,
} from "./subagentTeamData";

type TreeMode = SubagentTreeMode | null;
type DecisionStatus = "waiting" | "approved" | "revision";

type SubagentTeamWorkspaceProps = {
  activeConversationId?: string | null;
  activeConversationTitle?: string | null;
};

const DEFAULT_CHANNEL_ID = "ship-room";

function effectiveThreadId(thread: SubagentThread): string {
  return thread.type === "dm" ? `dm-${thread.id}` : thread.id;
}

function createClientMessageId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `subagent-${crypto.randomUUID()}`;
  }
  return `subagent-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

const ROLE_ICON_REGISTRY: Record<string, LucideIcon> = {
  pm: Crown,
  coder: Code2,
  qa: FlaskConical,
  reviewer: Search,
  researcher: BookOpen,
  creator: Network,
  agent: Bot,
  user: UserRound,
};

const fallbackCreatorSettings: SubagentTeamCreatorSettings = {
  enabled: true,
  model: "decision-layer",
  lifecycle_only: true,
  can_manage_agents: true,
  can_enable_rich: false,
  rich_gate_message: "Creator cannot enable elevated modes. User action is required.",
};

function roleIconForAgent(agent: CompanyAgent | undefined | null, fallbackId?: string | null): LucideIcon {
  if (fallbackId === "you" || fallbackId === "user") return ROLE_ICON_REGISTRY.user;
  return ROLE_ICON_REGISTRY[agentRoleKey(agent, fallbackId)] ?? ROLE_ICON_REGISTRY.agent;
}

function creatorSettingsFromResponse(payload: unknown): SubagentTeamCreatorSettings {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return fallbackCreatorSettings;
  const record = payload as Record<string, unknown>;
  const nested = record.settings ?? record.creator;
  return {
    ...record,
    ...(nested && typeof nested === "object" && !Array.isArray(nested) ? nested as Record<string, unknown> : {}),
  } as SubagentTeamCreatorSettings;
}

function decisionTaskFromPreview(payload: SubagentTeamDecisionPreviewResponse, fallback: CompanyTask | null): CompanyTask | null {
  const task = payload.task && typeof payload.task === "object" ? payload.task : payload;
  const title = typeof task.title === "string" && task.title.trim() ? task.title : fallback?.title;
  if (!title) return fallback;
  const targetAgentIds = Array.isArray(task.target_agent_ids)
    ? task.target_agent_ids.filter((value): value is string => typeof value === "string")
    : fallback?.target_agent_ids;
  return {
    id: typeof task.id === "string" ? task.id : fallback?.id ?? "creator-decision-preview",
    company_id: typeof task.company_id === "string" ? task.company_id : fallback?.company_id ?? "subagent-team",
    title,
    description: typeof task.description === "string" ? task.description : typeof payload.summary === "string" ? payload.summary : fallback?.description,
    target_agent_ids: targetAgentIds,
    source: typeof task.source === "string" ? task.source : "creator-preview",
    status: typeof task.status === "string" ? task.status : typeof payload.status === "string" ? payload.status : fallback?.status ?? "pending_decision",
    metadata: task.metadata && typeof task.metadata === "object" && !Array.isArray(task.metadata) ? task.metadata as Record<string, unknown> : fallback?.metadata,
    created_at: typeof task.created_at === "string" ? task.created_at : fallback?.created_at,
    updated_at: typeof task.updated_at === "string" ? task.updated_at : fallback?.updated_at,
  };
}

function statusClassName(status: string): string {
  const value = status.toLowerCase();
  if (/(active|running|review|routing)/.test(value)) return "border-emerald-500/25 bg-emerald-500/10 text-emerald-200";
  if (/(queued|pending|waiting)/.test(value)) return "border-amber-500/25 bg-amber-500/10 text-amber-200";
  if (/(error|failed|blocked)/.test(value)) return "border-red-500/25 bg-red-500/10 text-red-200";
  return "border-zinc-800 bg-zinc-950/50 text-zinc-500";
}

function mentionIdsFromText(text: string, agents: CompanyAgent[]): string[] {
  const lower = text.toLowerCase();
  return agents
    .filter((agent) => {
      const ids = [agent.agent_id, agent.display_name, agent.agent_name, ...(agent.aliases ?? [])]
        .map((value) => String(value ?? "").trim().toLowerCase())
        .filter(Boolean);
      return ids.some((id) => lower.includes(`@${id}`));
    })
    .map((agent) => agent.agent_id);
}

function compactCount(value: number): string {
  if (value > 99) return "99+";
  return String(Math.max(0, value));
}

function TreePreview({
  mode,
  treeState,
  activePreview,
  openingNodeId,
  treeError,
  onOpenNode,
  onClose,
}: {
  mode: Exclude<TreeMode, null>;
  treeState: SubagentTreeState;
  activePreview: SubagentOpenPreview | null;
  openingNodeId: string | null;
  treeError: string | null;
  onOpenNode: (item: SubagentTreeItem) => void;
  onClose: () => void;
}) {
  const items = subagentTreeItemsForMode(treeState, mode);
  const preview = activePreview?.mode === mode ? activePreview : null;
  return (
    <div className="mx-2 mb-2 rounded-lg border border-zinc-800/80 bg-zinc-950/65 p-2 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5 text-[11px] font-medium text-zinc-200">
          {mode === "files" ? <FolderTree size={13} className="text-sky-300" /> : <History size={13} className="text-amber-300" />}
          <span className="truncate">{mode === "files" ? "File tree" : "History tree"}</span>
          <span className={cn("rounded px-1 py-0.5 text-[9px]", treeState.source === "api" ? "bg-emerald-500/10 text-emerald-200" : "bg-zinc-900 text-zinc-500")}>
            {treeState.source}
          </span>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded border border-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500 hover:border-zinc-700 hover:text-zinc-200"
        >
          Close
        </button>
      </div>
      {treeError && treeState.source === "fallback" && (
        <ErrorNotice
          className="mb-1.5 px-1.5 py-1 text-[10px]"
          copyLabel="サブエージェントツリーエラーをコピー"
          message={treeError}
          severity="warning"
        />
      )}
      <div className="space-y-0.5" data-testid={`subagent-${mode}-tree`}>
        {items.map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => onOpenNode(item)}
            data-testid={`subagent-tree-node-${item.nodeId}`}
            className="flex h-6 w-full items-center gap-1.5 rounded px-1 text-left text-[11px] text-zinc-400 hover:bg-zinc-900/80 hover:text-zinc-100"
            style={{ paddingLeft: `${4 + item.depth * 12}px` }}
          >
            {item.kind === "folder" ? <FolderTree size={11} className="shrink-0 text-sky-400/80" /> : <FileText size={11} className="shrink-0 text-zinc-500" />}
            <span className="truncate">{item.label}</span>
            {openingNodeId === item.nodeId && <RefreshCw size={10} className="ml-auto shrink-0 animate-spin text-zinc-600" />}
          </button>
        ))}
        {items.length === 0 && (
          <p className="rounded border border-zinc-800 bg-zinc-950/50 px-2 py-2 text-[10px] text-zinc-600">No nodes</p>
        )}
      </div>
      {preview && (
        <div className="mt-2 border-t border-zinc-800/70 pt-2" data-testid="subagent-tree-preview">
          <div className="mb-1 flex min-w-0 items-center justify-between gap-2">
            <p className="min-w-0 truncate text-[11px] font-semibold text-zinc-200">{preview.title}</p>
            {preview.source === "fallback" && (
              <span className="shrink-0 rounded bg-zinc-900 px-1 py-0.5 text-[9px] text-zinc-500">fallback preview</span>
            )}
          </div>
          {preview.path && <p className="mb-1 truncate font-mono text-[10px] text-zinc-600">{preview.path}</p>}
          {preview.messages?.length ? (
            <div className="space-y-1">
              {preview.messages.slice(0, 4).map((message) => (
                <div key={`${message.channel_id}-${message.id}`} className="rounded bg-zinc-900/55 px-2 py-1">
                  <div className="mb-0.5 flex items-center gap-1.5 text-[9px] text-zinc-600">
                    <span className="font-semibold text-zinc-400">{message.sender_id}</span>
                    <span>#{shortId(message.id)}</span>
                  </div>
                  <p className="line-clamp-2 text-[11px] leading-relaxed text-zinc-300">{message.content}</p>
                </div>
              ))}
            </div>
          ) : preview.content ? (
            <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded bg-zinc-900/55 px-2 py-1.5 font-mono text-[10px] leading-relaxed text-zinc-300">
              {preview.content}
            </pre>
          ) : (
            <p className="rounded bg-zinc-900/55 px-2 py-1.5 text-[10px] text-zinc-500">
              {preview.error || "No preview content"}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function SignalBanner({
  icon,
  label,
  text,
}: {
  icon: ReactNode;
  label: string;
  text: string;
}) {
  return (
    <div className="rounded-lg border border-amber-500/20 bg-amber-500/10 px-2.5 py-2 text-amber-100">
      <div className="mb-0.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide">
        <span className="shrink-0">{icon}</span>
        <span>{label}</span>
      </div>
      <p className="line-clamp-2 text-[11px] leading-relaxed text-zinc-300">{text}</p>
    </div>
  );
}

function CreatorDecisionPreview({
  task,
  status,
}: {
  task: CompanyTask | null;
  status: DecisionStatus;
}) {
  const title = task?.title || "PM routing plan preview";
  const targetAgents = task?.target_agent_ids?.length ? task.target_agent_ids.join(", ") : "pm, frontend, qa";
  return (
    <div className="mx-2 mb-2 rounded-lg border border-zinc-800/80 bg-[#0d0d11] p-2.5">
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
            <ClipboardCheck size={12} className="text-emerald-300" />
            <span>Creator decision preview</span>
          </div>
          <p className="mt-1 line-clamp-2 text-[12px] font-medium leading-snug text-zinc-100">{title}</p>
        </div>
        <span className={cn("shrink-0 rounded border px-1.5 py-0.5 text-[9px]", statusClassName(status))}>
          {status}
        </span>
      </div>
      <div className="grid gap-1.5 text-[10px] text-zinc-500">
        <div className="rounded border border-zinc-800 bg-zinc-950/50 px-2 py-1.5">
          <span className="text-zinc-400">Route:</span> {targetAgents}
        </div>
        <div className="rounded border border-zinc-800 bg-zinc-950/50 px-2 py-1.5">
          <span className="text-zinc-400">Creator sees:</span> PM summary, changed files, and latest channel blockers.
        </div>
      </div>
      <div
        role="note"
        data-testid="subagent-creator-decision-readonly"
        className="mt-2 flex items-start gap-1.5 rounded-md border border-sky-500/20 bg-sky-500/10 px-2 py-1.5 text-[10px] leading-4 text-sky-100"
      >
        <ShieldCheck size={12} className="mt-0.5 shrink-0" />
        <span>Read-only preview. No approval or revision is recorded from this card.</span>
      </div>
    </div>
  );
}

function MessageRow({
  message,
  sender,
}: {
  message: CompanyMessage;
  sender?: CompanyAgent;
}) {
  const name = message.sender_id === "user" || message.sender_id === "you" ? "You" : agentName(sender, message.sender_id);
  const senderShortId = message.sender_id === "user" || message.sender_id === "you" ? "main" : agentShortId(sender, message.sender_id);
  return (
    <article className="group flex gap-2 rounded-lg px-2 py-2 hover:bg-zinc-900/45">
      <AgentAvatar agent={sender} fallbackId={message.sender_id} />
      <div className="min-w-0 flex-1">
        <div className="mb-0.5 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5">
          <span className="truncate text-[12px] font-semibold text-zinc-100">{name}</span>
          <span className="font-mono text-[9px] text-sky-300/80">@{senderShortId}</span>
          <span className="font-mono text-[9px] text-zinc-600">msg #{shortId(message.id)}</span>
          <span className="text-[9px] text-zinc-700">{messageTime(message.created_at)}</span>
        </div>
        <p className="whitespace-pre-wrap break-words text-[12px] leading-relaxed text-zinc-300">{message.content}</p>
        {message.attachments && message.attachments.length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-1">
            {message.attachments.slice(0, 3).map((attachment, index) => (
              <span
                key={`${message.id}-${attachment.path ?? attachment.name ?? index}`}
                className="inline-flex max-w-full items-center gap-1 rounded border border-zinc-800 bg-zinc-950/70 px-1.5 py-0.5 text-[10px] text-zinc-400"
              >
                <FileText size={10} className="shrink-0" />
                <span className="truncate">{attachment.name || attachment.path || "attachment"}</span>
              </span>
            ))}
          </div>
        )}
      </div>
    </article>
  );
}

export function ChannelButton({
  channel,
  active,
  memberCount,
  unreadCount,
  expanded = false,
  onClick,
  onToggleExpand,
}: {
  channel: CompanyChannel;
  active: boolean;
  memberCount: number;
  unreadCount: number;
  expanded?: boolean;
  onClick: () => void;
  onToggleExpand?: () => void;
}) {
  return (
    <div
      data-testid={`subagent-channel-${channel.id}`}
      className={cn(
        "group flex w-full items-center gap-1.5 rounded-md px-1.5 py-1.5 text-left transition-colors",
        active ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:bg-zinc-900 hover:text-zinc-300",
      )}
      title={channel.description || channelName(channel)}
    >
      <button type="button" onClick={onClick} className="flex min-w-0 flex-1 items-center gap-1.5 text-left">
        <Hash size={12} className="shrink-0" />
        <span className="min-w-0 flex-1 truncate text-[11px] font-medium">
          {channelName(channel)} <span className={active ? "text-zinc-400" : "text-zinc-600"}>({memberCount})</span>
        </span>
      </button>
      {unreadCount > 0 && (
        <span
          data-testid={`subagent-channel-unread-${channel.id}`}
          className={cn("shrink-0 rounded px-1 py-0.5 text-[9px] font-semibold", active ? "bg-zinc-950 text-zinc-200" : "bg-zinc-800 text-zinc-400")}
        >
          {compactCount(unreadCount)}
        </span>
      )}
      {onToggleExpand && (
        <button
          type="button"
          onClick={onToggleExpand}
          className="flex h-5 w-5 shrink-0 items-center justify-center rounded text-zinc-600 hover:bg-zinc-950 hover:text-zinc-200"
          aria-expanded={expanded}
          title={`${expanded ? "Collapse" : "Expand"} ${channelName(channel)}`}
        >
          <ChevronDown size={12} className={cn("transition-transform", expanded && "rotate-180")} />
        </button>
      )}
    </div>
  );
}

function AgentAvatar({
  agent,
  fallbackId,
  active = false,
}: {
  agent?: CompanyAgent;
  fallbackId?: string | null;
  active?: boolean;
}) {
  const Icon = roleIconForAgent(agent, fallbackId ?? agent?.agent_id);
  return (
    <span
      className={cn(
        "flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border",
        active ? "border-sky-400/40 bg-sky-500/15 text-sky-100" : "border-zinc-800 bg-zinc-950 text-zinc-500",
      )}
    >
      <Icon size={14} />
    </span>
  );
}

function AgentDisclosure({
  agent,
  activity,
  expanded,
  activeDm,
  onToggle,
  onOpenDm,
}: {
  agent: CompanyAgent;
  activity?: AgentActivity;
  expanded: boolean;
  activeDm: boolean;
  onToggle: () => void;
  onOpenDm: () => void;
}) {
  const status = activity?.status || agent.status || "idle";
  const readableId = agentShortId(agent);
  return (
    <div className={cn("rounded-md border", activeDm ? "border-sky-500/30 bg-sky-500/10" : "border-zinc-800/70 bg-zinc-950/35")}>
      <div className="flex items-center gap-1 p-1">
        <button type="button" onClick={onOpenDm} className="flex min-w-0 flex-1 items-center gap-1.5 rounded px-1 py-1 text-left hover:bg-zinc-900/80">
          <AgentAvatar agent={agent} active={activeDm} />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[11px] font-medium text-zinc-200">{agentName(agent)}</span>
            <span className="block truncate font-mono text-[9px] text-zinc-600">@{readableId}</span>
          </span>
          {activity?.openInboxCount ? (
            <span className="rounded bg-sky-500/15 px-1 text-[9px] font-semibold text-sky-200">{compactCount(activity.openInboxCount)}</span>
          ) : null}
        </button>
        <button
          type="button"
          onClick={onToggle}
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-zinc-500 hover:bg-zinc-900 hover:text-zinc-200"
          aria-expanded={expanded}
          title={`Expand ${agentName(agent)}`}
        >
          <ChevronDown size={12} className={cn("transition-transform", expanded && "rotate-180")} />
        </button>
      </div>
      {expanded && (
        <div className="border-t border-zinc-800/70 px-2 py-1.5 text-[10px] text-zinc-500">
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className={cn("rounded border px-1 py-0.5", statusClassName(status))}>{status}</span>
            <span className="font-mono">@{readableId}</span>
          </div>
          <p className="truncate font-mono text-zinc-500">{agent.model || "stub/default"}</p>
          {agent.allowed_tools && agent.allowed_tools.length > 0 && (
            <p className="mt-1 line-clamp-2">tools: {agent.allowed_tools.slice(0, 3).join(", ")}</p>
          )}
        </div>
      )}
    </div>
  );
}

function ChannelMemberRow({
  agentId,
  agent,
  active,
  onOpenDm,
}: {
  agentId: string;
  agent?: CompanyAgent;
  active: boolean;
  onOpenDm: () => void;
}) {
  const pseudoAgent = agent ?? { agent_id: agentId, role_key: agentRoleKey(null, agentId), display_name: agentId };
  return (
    <button
      type="button"
      onClick={onOpenDm}
      className={cn(
        "ml-3 flex w-[calc(100%-0.75rem)] min-w-0 items-center gap-2 rounded px-1.5 py-1 text-left",
        active ? "bg-sky-500/15 text-sky-100" : "text-zinc-500 hover:bg-zinc-900/80 hover:text-zinc-200",
      )}
    >
      <AgentAvatar agent={pseudoAgent} fallbackId={agentId} active={active} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[11px] font-medium">{agentName(agent, agentId)}</span>
        <span className="block truncate font-mono text-[9px] text-zinc-600">@{agentShortId(agent, agentId)}</span>
      </span>
    </button>
  );
}

function CreatorSettingsCard({
  settings,
  source,
  error,
  busy,
  testResult,
  onTest,
}: {
  settings: SubagentTeamCreatorSettings;
  source: "api" | "preview";
  error: string | null;
  busy: boolean;
  testResult: SubagentTeamCreatorTestResponse | null;
  onTest: () => void;
}) {
  const lifecycleOnly = Boolean(settings.lifecycle_only ?? settings.lifecycleOnly ?? true);
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950/55 p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
            <Network size={12} className="text-emerald-300" />
            <span>Creator</span>
          </div>
          <p className="mt-1 truncate font-mono text-[10px] text-zinc-500">{settings.model || "decision-layer"}</p>
        </div>
        <span className={cn("shrink-0 rounded border px-1.5 py-0.5 text-[9px]", lifecycleOnly ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-200" : "border-amber-500/25 bg-amber-500/10 text-amber-200")}>
          {lifecycleOnly ? "lifecycle" : "mixed"}
        </span>
      </div>
      <div className="mt-2 space-y-1 text-[10px] text-zinc-500">
        <p className="rounded border border-zinc-800 bg-black/20 px-2 py-1.5">
          Agent lifecycle: {settings.can_manage_agents ?? settings.canManageAgents ?? true ? "enabled" : "restricted"}
        </p>
      </div>
      {error && (
        <ErrorNotice
          className="mt-2 px-1.5 py-1 text-[10px]"
          copyLabel="Creator 設定エラーをコピー"
          message={error}
          severity="warning"
        />
      )}
      {testResult && (
        testResult.ok === false ? (
          <ErrorNotice
            className="mt-2 px-2 py-1.5 text-[10px]"
            copyLabel="Creator テストエラーをコピー"
            errorIcon="creator-test"
            message={testResult.message || testResult.summary || testResult.status || "Creator test failed."}
          />
        ) : (
          <p className="mt-2 line-clamp-3 rounded border border-zinc-800 bg-black/20 px-2 py-1.5 text-[10px] text-zinc-400">
            {testResult.message || testResult.summary || testResult.status || "Creator test passed."}
          </p>
        )
      )}
      <button
        type="button"
        onClick={onTest}
        disabled={busy || source !== "api"}
        className="mt-2 flex h-8 w-full items-center justify-center gap-1.5 rounded-md border border-zinc-800 px-2 text-[11px] font-semibold text-zinc-300 hover:border-zinc-700 hover:bg-zinc-900 disabled:cursor-not-allowed disabled:text-zinc-700"
      >
        <MessageSquare size={12} />
        <span>{busy ? "Testing..." : "Test Creator"}</span>
      </button>
      <p className="mt-1 font-mono text-[9px] text-zinc-700">source:{source}</p>
    </div>
  );
}

function AgentDetailCard({ agent, activity }: { agent?: CompanyAgent | null; activity?: AgentActivity }) {
  if (!agent) {
    return (
      <div className="rounded-lg border border-zinc-800 bg-zinc-950/55 p-3 text-[11px] text-zinc-500">
        Select an agent or DM to inspect status.
      </div>
    );
  }
  const status = activity?.status || agent.status || "idle";
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950/55 p-3">
      <div className="flex min-w-0 items-start gap-2">
        <AgentAvatar agent={agent} active />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[12px] font-semibold text-zinc-100">{agentName(agent)}</p>
          <p className="truncate font-mono text-[10px] text-sky-300/80">@{agentShortId(agent)}</p>
        </div>
        <span className={cn("shrink-0 rounded border px-1.5 py-0.5 text-[9px]", statusClassName(status))}>{status}</span>
      </div>
      <div className="mt-2 space-y-1 text-[10px] text-zinc-500">
        <p className="truncate rounded border border-zinc-800 bg-black/20 px-2 py-1.5 font-mono">{agent.model || "stub/default"}</p>
        <p className="rounded border border-zinc-800 bg-black/20 px-2 py-1.5">role: {agentRoleKey(agent)}</p>
        {activity?.latestRun?.agent_run?.result_preview && (
          <p className="line-clamp-3 rounded border border-zinc-800 bg-black/20 px-2 py-1.5">{activity.latestRun.agent_run.result_preview}</p>
        )}
        {activity?.latestInbox && (
          <p className="line-clamp-3 rounded border border-zinc-800 bg-black/20 px-2 py-1.5">{activity.latestInbox.content}</p>
        )}
      </div>
    </div>
  );
}

function TaskListCard({
  tasks,
  agentsById,
}: {
  tasks: CompanyTask[];
  agentsById: Map<string, CompanyAgent>;
}) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950/55 p-3">
      <div className="mb-2 flex items-center justify-between gap-2 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
        <span>Tasks</span>
        <span>{tasks.length}</span>
      </div>
      <div className="space-y-1.5">
        {tasks.slice(0, 5).map((task) => (
          <div key={task.id} className="rounded border border-zinc-800 bg-black/20 px-2 py-1.5">
            <div className="flex min-w-0 items-center justify-between gap-2">
              <p className="min-w-0 truncate text-[11px] font-medium text-zinc-200">{task.title}</p>
              <span className="shrink-0 rounded bg-zinc-900 px-1 py-0.5 text-[9px] text-zinc-500">{task.status || "open"}</span>
            </div>
            {task.target_agent_ids?.length ? (
              <p className="mt-1 truncate font-mono text-[9px] text-zinc-600">
                {task.target_agent_ids.map((id) => `@${agentShortId(agentsById.get(id), id)}`).join(" ")}
              </p>
            ) : null}
          </div>
        ))}
        {tasks.length === 0 && <p className="rounded border border-zinc-800 bg-black/20 px-2 py-2 text-[10px] text-zinc-600">No active tasks</p>}
      </div>
    </div>
  );
}

function ApprovalCard({
  task,
  status,
  source,
  error,
}: {
  task: CompanyTask | null;
  status: DecisionStatus;
  source: "api" | "preview";
  error: string | null;
}) {
  const previewMessage = source === "preview"
    ? "Fallback preview data is read-only and cannot be approved."
    : "Decision preview only. Open the authoritative pending request to approve, reject, or request revision.";
  return (
    <div className="rounded-lg border border-amber-500/20 bg-amber-500/10 p-3">
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-amber-200">
            <ClipboardCheck size={12} />
            <span>Decision preview</span>
          </div>
          <p className="mt-1 line-clamp-2 text-[12px] font-medium text-zinc-100">{task?.title || "PM goal gate preview"}</p>
        </div>
        <span className={cn("shrink-0 rounded border px-1.5 py-0.5 text-[9px]", statusClassName(status))}>{status}</span>
      </div>
      <p className="line-clamp-3 text-[11px] leading-relaxed text-zinc-300">
        {task?.description || "PM and Creator review channel context before subagents proceed with larger fanout or /goal execution."}
      </p>
      {error && (
        <ErrorNotice
          className="mt-2 px-1.5 py-1 text-[10px]"
          copyLabel="決定プレビューエラーをコピー"
          message={error}
          severity="warning"
        />
      )}
      <div
        role="note"
        data-testid="subagent-approval-preview-readonly"
        className="mt-2 rounded-md border border-amber-400/25 bg-black/20 px-2 py-1.5 text-[10px] leading-4 text-amber-50"
      >
        {previewMessage}
      </div>
      <p className="mt-1 font-mono text-[9px] text-amber-100/50">source:{source}</p>
    </div>
  );
}

export function SubagentTeamWorkspace({
  activeConversationId = null,
  activeConversationTitle = null,
}: SubagentTeamWorkspaceProps) {
  const [companies, setCompanies] = useState<CompanyRecord[]>([]);
  const [activeCompanyId, setActiveCompanyId] = useState<string | null>(null);
  const [company, setCompany] = useState<CompanyRecord | null>(null);
  const [agents, setAgents] = useState<CompanyAgent[]>([]);
  const [channels, setChannels] = useState<CompanyChannel[]>([]);
  const [messages, setMessages] = useState<CompanyMessage[]>([]);
  const [tasks, setTasks] = useState<CompanyTask[]>([]);
  const [runs, setRuns] = useState<CompanyRunLink[]>([]);
  const [inboxItems, setInboxItems] = useState<CompanyInboxItem[]>([]);
  const [localMessages, setLocalMessages] = useState<CompanyMessage[]>([]);
  const [activeThread, setActiveThread] = useState<SubagentThread>({ type: "channel", id: DEFAULT_CHANNEL_ID });
  const [expandedAgentIds, setExpandedAgentIds] = useState<Set<string>>(() => new Set(["creator", "pm"]));
  const [expandedChannelIds, setExpandedChannelIds] = useState<Set<string>>(() => new Set([DEFAULT_CHANNEL_ID]));
  const [isAgentsOpen, setIsAgentsOpen] = useState(true);
  const [treeMode, setTreeMode] = useState<TreeMode>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [decisionPreviewTask, setDecisionPreviewTask] = useState<CompanyTask | null>(null);
  const [decisionPreviewSource, setDecisionPreviewSource] = useState<"api" | "preview">("preview");
  const [decisionPreviewError, setDecisionPreviewError] = useState<string | null>(null);
  const [creatorSettings, setCreatorSettings] = useState<SubagentTeamCreatorSettings>(() => fallbackCreatorSettings);
  const [creatorSettingsSource, setCreatorSettingsSource] = useState<"api" | "preview">("preview");
  const [creatorSettingsError, setCreatorSettingsError] = useState<string | null>(null);
  const [creatorTestBusy, setCreatorTestBusy] = useState(false);
  const [creatorTestResult, setCreatorTestResult] = useState<SubagentTeamCreatorTestResponse | null>(null);
  const [treeState, setTreeState] = useState<SubagentTreeState>(() => fallbackSubagentTreeState());
  const [treeError, setTreeError] = useState<string | null>(null);
  const [openingNodeId, setOpeningNodeId] = useState<string | null>(null);
  const [openPreview, setOpenPreview] = useState<SubagentOpenPreview | null>(null);

  const loadSubagentTree = useCallback(async (companyId?: string | null) => {
    if (!companyId && !activeConversationId) {
      setTreeState(fallbackSubagentTreeState());
      setTreeError(null);
      setOpenPreview(null);
      return;
    }
    try {
      const result = await api.getSubagentTeamFileTree({
        companyId,
        conversationId: activeConversationId,
        limit: 240,
        includeGit: true,
      });
      setTreeState(normalizeSubagentTreeResponse(result));
      setTreeError(null);
      setOpenPreview(null);
    } catch (treeLoadError) {
      setTreeState(fallbackSubagentTreeState());
      setTreeError(treeLoadError instanceof Error ? treeLoadError.message : "File tree API unavailable.");
    }
  }, [activeConversationId]);

  const ensureSubagentCompanyMarker = useCallback(async (record: CompanyRecord | null): Promise<CompanyRecord | null> => {
    if (!activeConversationId || !record?.id || hasSubagentTeamWorkspaceMarker(record.metadata)) return record;
    try {
      return await api.updateSubagentTeamWorkspaceMetadata({
        companyId: record.id,
        conversationId: activeConversationId,
        metadata: subagentTeamWorkspaceMetadata(record.metadata ?? {}),
      });
    } catch {
      return record;
    }
  }, [activeConversationId]);

  const loadWorkspace = useCallback(async (requestedCompanyId?: string | null) => {
    setBusy(true);
    setError(null);
    try {
      const companyListResult = await api.listCompanies({ limit: 8 }).catch(() => ({ companies: [], total: 0 }));
      const listedCompanies = companyListResult.companies;
      let statusCompany: CompanyRecord | null = null;
      let selectedId = requestedCompanyId ?? null;

      if (activeConversationId) {
        const status = await api.getCompanyStatus({ conversationId: activeConversationId, bootstrap: false });
        statusCompany = status.company ?? null;
        selectedId = requestedCompanyId ?? statusCompany?.id ?? null;
      } else if (!selectedId) {
        selectedId = listedCompanies[0]?.id ?? null;
      }

      setCompanies(listedCompanies);
      setActiveCompanyId(selectedId);
      const selectedCompany = await ensureSubagentCompanyMarker(
        statusCompany ?? (selectedId ? listedCompanies.find((item) => item.id === selectedId) ?? null : null),
      );
      setCompany(selectedCompany);

      if (!selectedId) {
        setAgents([]);
        setChannels([]);
        setMessages([]);
        setTasks([]);
        setRuns([]);
        setInboxItems([]);
        return;
      }

      const [agentResult, channelResult, taskResult, messageResult, runResult] = await Promise.allSettled([
        api.listCompanyAgents(selectedId),
        api.listCompanyChannels(selectedId),
        api.listCompanyTasks(selectedId),
        api.listCompanyMessages(selectedId, { limit: 100 }),
        api.listCompanyRuns(selectedId, { limit: 80 }),
      ]);

      const nextAgents = agentResult.status === "fulfilled" ? agentResult.value.agents : arrayFromRecord(statusCompany?.agents);
      const nextChannels = channelResult.status === "fulfilled" ? channelResult.value.channels : arrayFromRecord(statusCompany?.channels);
      const nextTasks = taskResult.status === "fulfilled" ? taskResult.value.tasks : arrayFromRecord(statusCompany?.tasks);
      const nextMessages = messageResult.status === "fulfilled" ? messageResult.value.messages : arrayFromRecord(statusCompany?.messages);
      const nextRuns = runResult.status === "fulfilled" ? runResult.value.runs : [];

      setAgents(nextAgents);
      setChannels(nextChannels);
      setTasks(nextTasks);
      setMessages(nextMessages);
      setLocalMessages((current) => removeReconciledLocalSubagentMessages(current, nextMessages));
      setRuns(nextRuns);

      const inboxResults = await Promise.allSettled(
        nextAgents.map((agent) => api.listCompanyAgentInbox(selectedId, agent.agent_id, { limit: 20 })),
      );
      setInboxItems(inboxResults.flatMap((result) => result.status === "fulfilled" ? result.value.inbox : []));

      setActiveThread((current) => {
        if (current.type === "dm" && nextAgents.some((agent) => agent.agent_id === current.id)) return current;
        if (current.type === "channel" && nextChannels.some((channel) => channel.id === current.id)) return current;
        return { type: "channel", id: nextChannels[0]?.id ?? DEFAULT_CHANNEL_ID };
      });

      const firstRejected = [agentResult, channelResult, messageResult].find((result) => result.status === "rejected") as PromiseRejectedResult | undefined;
      if (firstRejected) {
        setError(firstRejected.reason instanceof Error ? firstRejected.reason.message : "Some team workspace data could not be loaded.");
      }
    } catch (workspaceError) {
      setError(workspaceError instanceof Error ? workspaceError.message : "Team workspace APIs are unavailable.");
      setCompanies([]);
      setActiveCompanyId(null);
      setCompany(null);
      setAgents([]);
      setChannels([]);
      setMessages([]);
      setTasks([]);
      setRuns([]);
      setInboxItems([]);
      setTreeState(fallbackSubagentTreeState());
      setTreeError(null);
      setOpenPreview(null);
    } finally {
      setBusy(false);
    }
  }, [activeConversationId, ensureSubagentCompanyMarker]);

  useEffect(() => {
    setActiveCompanyId(null);
    setLocalMessages([]);
    void loadWorkspace(null);
  }, [activeConversationId, loadWorkspace]);

  const previewDataReason = subagentTeamPreviewDataReason({
    activeCompanyId,
    company,
    agents,
    channels,
    messages,
    tasks,
    runs,
    inboxItems,
  });
  const isPreviewWorkspace = previewDataReason === "preview_workspace";
  const isUsingPreviewFallbackData = previewDataReason !== null;
  const visibleCompany = company ?? companies.find((item) => item.id === activeCompanyId) ?? previewCompany;
  const visibleAgents = isUsingPreviewFallbackData ? previewAgents : agents;
  const visibleChannels = isUsingPreviewFallbackData ? previewChannels : channels;
  const visibleMessages = isUsingPreviewFallbackData ? previewMessages : messages;
  const visibleTasks = isUsingPreviewFallbackData ? previewTasks : tasks;
  const visibleRuns = isUsingPreviewFallbackData ? previewRuns : runs;
  const visibleInbox = isUsingPreviewFallbackData ? previewInbox : inboxItems;
  const allMessages = useMemo(
    () => [
      ...visibleMessages,
      ...removeReconciledLocalSubagentMessages(localMessages, visibleMessages),
    ].sort((left, right) => messageTimestamp(left.created_at) - messageTimestamp(right.created_at)),
    [localMessages, visibleMessages],
  );
  const agentsById = useMemo(() => new Map(visibleAgents.map((agent) => [agent.agent_id, agent])), [visibleAgents]);
  const channelsById = useMemo(() => new Map(visibleChannels.map((channel) => [channel.id, channel])), [visibleChannels]);
  const activityByAgent = useMemo(() => buildAgentActivity(visibleAgents, visibleInbox, visibleRuns), [visibleAgents, visibleInbox, visibleRuns]);
  const activeChannel = activeThread.type === "channel" ? channelsById.get(activeThread.id) : null;
  const activeAgent = activeThread.type === "dm" ? agentsById.get(activeThread.id) : null;
  const latestDecisionTask = visibleTasks.find((task) => /decision|approve|review/i.test(`${task.title} ${task.status ?? ""}`)) ?? visibleTasks[0] ?? null;
  const effectiveDecisionTask = decisionPreviewTask ?? latestDecisionTask;
const decisionStatus: DecisionStatus = (() => {
  const value = String(effectiveDecisionTask?.status ?? "").trim().toLowerCase();
  if (["approved", "completed", "done"].includes(value)) return "approved";
  if (["revision", "changes_requested", "blocked"].includes(value)) return "revision";
  return "waiting";
})();

  const loadSubagentTeamControls = useCallback(async () => {
    if (!activeCompanyId && !activeConversationId) {
      setCreatorSettings(fallbackCreatorSettings);
      setCreatorSettingsSource("preview");
      setCreatorSettingsError(null);
      setDecisionPreviewTask(null);
      setDecisionPreviewSource("preview");
      setDecisionPreviewError(null);
      return;
    }
    const context = {
      companyId: activeCompanyId,
      conversationId: activeConversationId,
    };

    const [creatorResult, decisionResult] = await Promise.allSettled([
      api.getSubagentTeamCreatorSettings(context),
      api.getSubagentTeamCreatorDecisionPreview({
        ...context,
        channelId: activeThread.type === "channel" ? activeThread.id : null,
      }),
    ]);

    if (creatorResult.status === "fulfilled") {
      setCreatorSettings(creatorSettingsFromResponse(creatorResult.value));
      setCreatorSettingsSource("api");
      setCreatorSettingsError(null);
    } else {
      setCreatorSettings(fallbackCreatorSettings);
      setCreatorSettingsSource("preview");
      setCreatorSettingsError(creatorResult.reason instanceof Error ? creatorResult.reason.message : "Creator settings API unavailable.");
    }

    if (decisionResult.status === "fulfilled") {
      setDecisionPreviewTask(decisionTaskFromPreview(decisionResult.value, latestDecisionTask));
      setDecisionPreviewSource("api");
      setDecisionPreviewError(null);
    } else {
      setDecisionPreviewTask(null);
      setDecisionPreviewSource("preview");
      setDecisionPreviewError(decisionResult.reason instanceof Error ? decisionResult.reason.message : "Creator decision preview API unavailable.");
    }
  }, [activeCompanyId, activeConversationId, activeThread, latestDecisionTask]);

  useEffect(() => {
    void loadSubagentTeamControls();
  }, [loadSubagentTeamControls]);

  useEffect(() => {
    if (!treeMode) return;
    void loadSubagentTree(activeCompanyId);
  }, [activeCompanyId, loadSubagentTree, treeMode]);

  const threadMessages = useMemo(() => {
    if (activeThread.type === "channel") {
      return allMessages.filter((message) => message.channel_id === activeThread.id);
    }
    const dmChannelId = effectiveThreadId(activeThread);
    const agentId = activeThread.id;
    const agentInboxMessages = visibleInbox
      .filter((item) => item.agent_id === agentId)
      .map((item): CompanyMessage => ({
        id: `inbox-${item.inbox_id}`,
        company_id: item.company_id,
        channel_id: dmChannelId,
        sender_id: item.agent_id,
        content: item.content,
        created_at: item.created_at,
        metadata: item.metadata,
      }));
    const agentRunMessages = visibleRuns
      .filter((run) => run.agent_id === agentId && (run.agent_run?.result_preview || run.agent_run?.error))
      .map((run): CompanyMessage => ({
        id: `run-${run.run_id}`,
        company_id: run.company_id,
        channel_id: dmChannelId,
        sender_id: run.agent_id,
        content: run.agent_run?.result_preview || run.agent_run?.error || run.status,
        created_at: run.agent_run?.updated_at ?? run.updated_at ?? run.created_at,
        metadata: run.metadata,
      }));
    return [
      ...allMessages.filter((message) => (
        message.channel_id === dmChannelId
        || message.sender_id === agentId
        || message.mentions?.includes(agentId)
        || message.handoff?.target_agent_id === agentId
      )),
      ...agentInboxMessages,
      ...agentRunMessages,
    ].sort((left, right) => messageTimestamp(left.created_at) - messageTimestamp(right.created_at));
  }, [activeThread, allMessages, visibleInbox, visibleRuns]);

  const toggleExpandedAgent = (agentId: string) => {
    setExpandedAgentIds((current) => {
      const next = new Set(current);
      if (next.has(agentId)) next.delete(agentId);
      else next.add(agentId);
      return next;
    });
  };

  const toggleExpandedChannel = (channelId: string) => {
    setExpandedChannelIds((current) => {
      const next = new Set(current);
      if (next.has(channelId)) next.delete(channelId);
      else next.add(channelId);
      return next;
    });
  };

  const bootstrapWorkspace = async () => {
    if (!activeConversationId || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.bootstrapSubagentTeamWorkspace(
        subagentTeamWorkspaceMetadata({
          source: "webapp",
          name: "Subagent Team",
          conversation_id: activeConversationId,
          scope: "conversation",
        }),
        { conversationId: activeConversationId, scope: "conversation" },
      );
      setCompany(result.company);
      setActiveCompanyId(result.company.id);
      await loadWorkspace(result.company.id);
    } catch (bootstrapError) {
      setError(bootstrapError instanceof Error ? bootstrapError.message : "Could not create team workspace. Preview remains available.");
    } finally {
      setBusy(false);
    }
  };

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const content = draft.trim();
    if (!content || busy) return;
    const now = new Date().toISOString();
    const clientMessageId = createClientMessageId();
    const targetChannelId = activeThread.type === "channel" ? activeThread.id : visibleChannels[0]?.id ?? DEFAULT_CHANNEL_ID;
    const mentions = activeThread.type === "dm" ? [activeThread.id] : mentionIdsFromText(content, visibleAgents);
    const optimistic: CompanyMessage = {
      id: `local-${clientMessageId}`,
      company_id: visibleCompany.id,
      channel_id: activeThread.type === "dm" ? effectiveThreadId(activeThread) : targetChannelId,
      sender_id: "you",
      content,
      mentions,
      created_at: now,
      metadata: subagentTeamWorkspaceMetadata({
        source: "subagent_team_ui",
        client_message_id: clientMessageId,
        ...(activeThread.type === "dm" ? { dm_agent_id: activeThread.id } : {}),
      }),
    };
    setLocalMessages((current) => [...current, optimistic]);
    setDraft("");

    if (isPreviewWorkspace || !activeCompanyId) return;
    setBusy(true);
    setError(null);
    try {
      const sentMessage = await api.sendSubagentTeamMessage({
        companyId: activeCompanyId,
        conversationId: activeConversationId,
        content,
        channel_id: targetChannelId,
        sender_id: "user",
        mentions,
        client_message_id: clientMessageId,
        metadata: optimistic.metadata,
      });
      setLocalMessages((current) => removeReconciledLocalSubagentMessages(current, [sentMessage], clientMessageId));
      await loadWorkspace(activeCompanyId);
    } catch (sendError) {
      setError(sendError instanceof Error ? sendError.message : "Message was kept locally, but backend send failed.");
    } finally {
      setBusy(false);
    }
  };

  const handleOpenTreeNode = async (item: SubagentTreeItem) => {
    setOpeningNodeId(item.nodeId);
    try {
      const result = await api.openSubagentTeamFileTreeNode({
        nodeId: item.nodeId,
        companyId: activeCompanyId,
        conversationId: activeConversationId,
      });
      setOpenPreview(normalizeSubagentOpenPreview(result, item));
    } catch (openError) {
      setOpenPreview(fallbackSubagentOpenPreview(
        item,
        openError instanceof Error ? openError.message : "Could not open tree node.",
      ));
    } finally {
      setOpeningNodeId(null);
    }
  };

  const handleCreatorTest = async () => {
    if (creatorSettingsSource !== "api" || creatorTestBusy) return;
    setCreatorTestBusy(true);
    setCreatorSettingsError(null);
    try {
      const result = await api.testSubagentTeamCreator({
        companyId: activeCompanyId,
        conversationId: activeConversationId,
        channel_id: activeThread.type === "channel" ? activeThread.id : undefined,
        agent_id: activeThread.type === "dm" ? activeThread.id : undefined,
        prompt: draft.trim() || "Validate team workspace routing, elevated-mode gates, and Creator lifecycle boundaries.",
        metadata: subagentTeamWorkspaceMetadata({ source: "subagent_team_ui" }),
      });
      setCreatorTestResult(result);
    } catch (creatorError) {
      setCreatorSettingsError(creatorError instanceof Error ? creatorError.message : "Creator test API unavailable.");
    } finally {
      setCreatorTestBusy(false);
    }
  };

  const detailAgent = activeAgent
    ?? (activeChannel?.members ?? []).map((agentId) => agentsById.get(agentId)).find(Boolean)
    ?? visibleAgents[0]
    ?? null;
  const detailTasks = activeThread.type === "dm"
    ? visibleTasks.filter((task) => task.target_agent_ids?.includes(activeThread.id))
    : visibleTasks;

  const threadTitle = activeThread.type === "dm"
    ? agentName(activeAgent, activeThread.id)
    : channelName(activeChannel, activeThread.id);
  const threadSubtitle = activeThread.type === "dm"
    ? `${activityByAgent.get(activeThread.id)?.status ?? activeAgent?.status ?? "idle"} agent DM`
    : activeChannel?.description || "team channel";

  return (
    <section className="flex h-full min-h-0 flex-col bg-[#0a0a0c] text-zinc-300" aria-label="Subagents team workspace">
      <div className="border-b border-zinc-800/60 px-3 py-2">
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0">
            <div className="flex min-w-0 items-center gap-1.5">
              <UsersRound size={14} className="shrink-0 text-sky-300" />
              <p className="truncate text-[13px] font-semibold text-zinc-100">Subagents / Teams</p>
            </div>
            <p className="truncate text-[10px] text-zinc-600">
              {activeConversationTitle || visibleCompany.name || activeConversationId || "preview workspace"}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {activeConversationId && isPreviewWorkspace && (
              <button
                type="button"
                onClick={bootstrapWorkspace}
                disabled={busy}
                className="h-7 rounded-md bg-zinc-100 px-2 text-[10px] font-semibold text-zinc-950 hover:bg-white disabled:opacity-40"
              >
                Start
              </button>
            )}
            <button
              type="button"
              onClick={() => void loadWorkspace(activeCompanyId)}
              disabled={busy}
              className="flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-40"
              title="Refresh team workspace"
            >
              <RefreshCw size={13} className={cn(busy && "animate-spin")} />
            </button>
          </div>
        </div>
      </div>

      {error && (
        <ErrorNotice
          className="m-2 px-2 py-1.5 text-[11px]"
          copyLabel="サブエージェントワークスペースエラーをコピー"
          message={error}
          severity="warning"
        />
      )}

      <div className="flex min-h-0 flex-1 flex-col overflow-hidden lg:flex-row">
        <aside className="flex min-h-[240px] shrink-0 flex-col border-b border-zinc-800/60 bg-[#09090b] lg:min-h-0 lg:w-[300px] lg:border-b-0 lg:border-r xl:w-[320px]" aria-label="Team channels">
          <div className="min-h-0 flex-1 overflow-y-auto p-3">
            <div className="mb-3">
              <div className="mb-1 flex items-center justify-between px-1 text-[10px] font-semibold uppercase tracking-wide text-zinc-600">
                <span>Channels</span>
                <span>{visibleChannels.length}</span>
              </div>
              <div className="space-y-1">
                {visibleChannels.map((channel) => {
                  const expanded = expandedChannelIds.has(channel.id);
                  return (
                    <div key={channel.id} className="space-y-0.5">
                      <ChannelButton
                        channel={channel}
                        active={activeThread.type === "channel" && activeThread.id === channel.id}
                        memberCount={channelMemberCount(channel)}
                        unreadCount={channelUnreadCount(channel, allMessages)}
                        expanded={expanded}
                        onClick={() => setActiveThread({ type: "channel", id: channel.id })}
                        onToggleExpand={() => toggleExpandedChannel(channel.id)}
                      />
                      {expanded && (
                        <div className="space-y-0.5">
                          {(channel.members ?? []).map((agentId) => (
                            <ChannelMemberRow
                              key={`${channel.id}-${agentId}`}
                              agentId={agentId}
                              agent={agentsById.get(agentId)}
                              active={activeThread.type === "dm" && activeThread.id === agentId}
                              onOpenDm={() => setActiveThread({ type: "dm", id: agentId })}
                            />
                          ))}
                          {channel.members?.length === 0 && (
                            <p className="ml-3 rounded border border-zinc-800 bg-zinc-950/40 px-2 py-1 text-[10px] text-zinc-600">No agents</p>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
                {visibleChannels.length === 0 && (
                  <p className="rounded-md border border-zinc-800 bg-zinc-950/40 px-2 py-2 text-[10px] text-zinc-600">No channels</p>
                )}
              </div>
            </div>

            <div className="mb-3">
              <div className="mb-1 flex items-center justify-between px-1 text-[10px] font-semibold uppercase tracking-wide text-zinc-600">
                <span>DMs</span>
                <span>{visibleAgents.length}</span>
              </div>
              <div className="space-y-1">
                {visibleAgents.map((agent) => {
                  const activity = activityByAgent.get(agent.agent_id);
                  const active = activeThread.type === "dm" && activeThread.id === agent.agent_id;
                  return (
                    <button
                      key={agent.agent_id}
                      type="button"
                      data-testid={`subagent-dm-${agent.agent_id}`}
                      onClick={() => setActiveThread({ type: "dm", id: agent.agent_id })}
                      className={cn(
                        "flex w-full items-center gap-1.5 rounded-md px-1.5 py-1.5 text-left",
                        active ? "bg-sky-500/15 text-sky-100 ring-1 ring-sky-500/25" : "text-zinc-500 hover:bg-zinc-900 hover:text-zinc-300",
                      )}
                    >
                      <AgentAvatar agent={agent} active={active} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[11px] font-medium">{agentName(agent)}</span>
                        <span className="block truncate font-mono text-[9px] text-zinc-600">@{agentShortId(agent)}</span>
                      </span>
                      {activity?.openInboxCount ? (
                        <span className="rounded bg-sky-500/15 px-1 text-[9px] font-semibold text-sky-200">{compactCount(activity.openInboxCount)}</span>
                      ) : null}
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="mb-3">
              <div className="mb-1 px-1 text-[10px] font-semibold uppercase tracking-wide text-zinc-600">Files / History</div>
              <div className="grid grid-cols-2 gap-1.5">
                <button
                  type="button"
                  onClick={() => setTreeMode((current) => current === "files" ? null : "files")}
                  data-testid="subagent-open-files"
                  className={cn("flex h-8 items-center justify-center gap-1.5 rounded-md border px-2 text-[11px]", treeMode === "files" ? "border-sky-500/30 bg-sky-500/10 text-sky-200" : "border-zinc-800 bg-zinc-950/40 text-zinc-500 hover:bg-zinc-900 hover:text-zinc-200")}
                  aria-pressed={treeMode === "files"}
                >
                  <FolderTree size={13} />
                  <span>Files</span>
                </button>
                <button
                  type="button"
                  onClick={() => setTreeMode((current) => current === "history" ? null : "history")}
                  data-testid="subagent-open-history"
                  className={cn("flex h-8 items-center justify-center gap-1.5 rounded-md border px-2 text-[11px]", treeMode === "history" ? "border-amber-500/30 bg-amber-500/10 text-amber-200" : "border-zinc-800 bg-zinc-950/40 text-zinc-500 hover:bg-zinc-900 hover:text-zinc-200")}
                  aria-pressed={treeMode === "history"}
                >
                  <History size={13} />
                  <span>History</span>
                </button>
              </div>
            </div>

            <div>
              <button
                type="button"
                onClick={() => setIsAgentsOpen((value) => !value)}
                className="mb-1 flex w-full items-center justify-between rounded px-1 py-1 text-[10px] font-semibold uppercase tracking-wide text-zinc-600 hover:bg-zinc-900 hover:text-zinc-300"
                aria-expanded={isAgentsOpen}
              >
                <span>Agents</span>
                <ChevronDown size={12} className={cn("transition-transform", isAgentsOpen && "rotate-180")} />
              </button>
              {isAgentsOpen && (
                <div className="space-y-1">
                  {visibleAgents.map((agent) => (
                    <AgentDisclosure
                      key={agent.agent_id}
                      agent={agent}
                      activity={activityByAgent.get(agent.agent_id)}
                      expanded={expandedAgentIds.has(agent.agent_id)}
                      activeDm={activeThread.type === "dm" && activeThread.id === agent.agent_id}
                      onToggle={() => toggleExpandedAgent(agent.agent_id)}
                      onOpenDm={() => setActiveThread({ type: "dm", id: agent.agent_id })}
                    />
                  ))}
                </div>
              )}
            </div>
          </div>
        </aside>

        <main className="flex min-h-[360px] min-w-0 flex-1 flex-col border-b border-zinc-800/60 lg:min-h-0 lg:border-b-0 lg:border-r">
          <div className="border-b border-zinc-800/60 px-2.5 py-2">
            <div className="flex min-w-0 items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="flex min-w-0 items-center gap-1.5">
                  {activeThread.type === "dm" ? <MessageCircle size={14} className="shrink-0 text-sky-300" /> : <Hash size={14} className="shrink-0 text-zinc-400" />}
                  <h2 className="truncate text-[13px] font-semibold text-zinc-100">{threadTitle}</h2>
                </div>
                <p className="mt-0.5 line-clamp-1 text-[10px] text-zinc-600">{threadSubtitle}</p>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <span className="rounded border border-zinc-800 bg-zinc-950/50 px-1.5 py-0.5 text-[10px] text-zinc-500">
                  {activeThread.type === "dm" ? `@${agentShortId(activeAgent, activeThread.id)}` : `${channelMemberCount(activeChannel ?? { id: activeThread.id, members: [] })} agents`}
                </span>
              </div>
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="grid gap-1.5 p-2">
              <SignalBanner
                icon={<ShieldCheck size={12} />}
                label="PM"
                text="PM summarizes handoffs and queues Creator decisions before subagents fan out."
              />
            </div>
            <div className="px-1 pb-2">
              {threadMessages.map((message) => (
                <MessageRow key={`${message.channel_id}-${message.id}`} message={message} sender={agentsById.get(message.sender_id)} />
              ))}
              {threadMessages.length === 0 && (
                <div className="mx-1 rounded-lg border border-zinc-800/70 bg-zinc-950/45 px-3 py-5 text-center">
                  <Inbox size={18} className="mx-auto mb-2 text-zinc-600" />
                  <p className="text-[12px] font-medium text-zinc-300">No messages yet</p>
                  <p className="mt-1 text-[10px] text-zinc-600">Send a note or DM an agent to start this lane.</p>
                </div>
              )}
            </div>
          </div>

          <form className="border-t border-zinc-800/60 p-2" onSubmit={handleSubmit}>
            <div className="flex items-end gap-1.5 rounded-lg border border-zinc-800 bg-zinc-950/65 p-1.5 focus-within:border-zinc-600">
              <textarea
                value={draft}
                data-testid="subagent-message-input"
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    event.currentTarget.form?.requestSubmit();
                  }
                }}
                placeholder={activeThread.type === "dm" ? `DM ${threadTitle}` : `Message #${threadTitle}`}
                className="max-h-24 min-h-8 min-w-0 flex-1 resize-none bg-transparent px-1 py-1 text-[12px] leading-relaxed text-zinc-200 outline-none placeholder:text-zinc-700"
                rows={1}
                disabled={busy}
              />
              <button
                type="submit"
                disabled={busy || !draft.trim()}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-zinc-100 text-zinc-950 hover:bg-white disabled:opacity-30"
                title="Send team message"
              >
                <Send size={13} />
              </button>
            </div>
          </form>
        </main>

        <aside className="flex min-h-[360px] shrink-0 flex-col bg-[#09090b] lg:min-h-0 lg:w-[340px] xl:w-[360px]" aria-label="Team detail">
          <div className="border-b border-zinc-800/60 px-3 py-2">
            <div className="flex min-w-0 items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate text-[12px] font-semibold text-zinc-100">Agent / task detail</p>
                <p className="truncate text-[10px] text-zinc-600">{threadTitle}</p>
              </div>
              <span className="rounded border border-zinc-800 bg-zinc-950/50 px-1.5 py-0.5 text-[9px] text-zinc-500">
                {decisionPreviewSource}
              </span>
            </div>
          </div>
          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
            <AgentDetailCard agent={detailAgent} activity={detailAgent ? activityByAgent.get(detailAgent.agent_id) : undefined} />
            <ApprovalCard
              task={effectiveDecisionTask}
              status={decisionStatus}
              source={decisionPreviewSource}
              error={decisionPreviewError}
            />
            <CreatorDecisionPreview task={effectiveDecisionTask} status={decisionStatus} />
            <CreatorSettingsCard
              settings={creatorSettings}
              source={creatorSettingsSource}
              error={creatorSettingsError}
              busy={creatorTestBusy}
              testResult={creatorTestResult}
              onTest={handleCreatorTest}
            />
            {treeMode && (
              <TreePreview
                mode={treeMode}
                treeState={treeState}
                activePreview={openPreview}
                openingNodeId={openingNodeId}
                treeError={treeError}
                onOpenNode={(item) => void handleOpenTreeNode(item)}
                onClose={() => setTreeMode(null)}
              />
            )}
            <TaskListCard tasks={detailTasks} agentsById={agentsById} />
          </div>
        </aside>
      </div>
    </section>
  );
}
