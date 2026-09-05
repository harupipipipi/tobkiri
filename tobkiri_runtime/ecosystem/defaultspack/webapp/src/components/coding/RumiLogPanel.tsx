import { AtSign, History, ListTodo, MessageSquare, RefreshCw, Send, Sparkles } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import type { RumiLogEvent, RumiLogSummary } from "../../lib/api";
import { cn } from "../../lib/cn";
import { codingResources } from "../../features/coding/resources/codingResources";
import { ErrorNotice } from "../ErrorNotice";

type LogFilter = "all" | "conversation" | "task" | "git";
type SummaryKey = "commit_count" | "push_count" | "task_count" | "conversation_count" | "mention_count";

type RumiTask = {
  taskId: string;
  title: string;
  status: string;
  owner: string;
};

const CONVERSATION_KINDS = new Set(["agent.message", "agent.note"]);
const TASK_KINDS = new Set(["task.created", "task.updated", "agent.assigned", "plan.created"]);
const GIT_KINDS = new Set(["git.commit", "git.push"]);
const MENTION_PATTERN = /(^|\s)(@[A-Za-z0-9_-]+)/g;
const TASK_ID_PATTERN = /\bT-\d{2,4}\b/i;

function eventLabel(kind: string): string {
  const labels: Record<string, string> = {
    "agent.assigned": "agent",
    "agent.message": "chat",
    "agent.note": "note",
    "git.commit": "commit",
    "git.push": "push",
    "plan.created": "plan",
    "task.created": "task",
    "task.updated": "task",
  };
  return labels[kind] ?? kind;
}

function eventAccent(kind: string): string {
  if (kind === "agent.message") return "border-sky-500/30 bg-sky-500/10 text-sky-200";
  if (kind === "git.commit") return "border-emerald-500/30 bg-emerald-500/10 text-emerald-200";
  if (kind === "git.push") return "border-cyan-500/30 bg-cyan-500/10 text-cyan-200";
  if (kind === "plan.created" || kind.startsWith("task.")) return "border-amber-500/30 bg-amber-500/10 text-amber-200";
  return "border-zinc-700 bg-zinc-900/80 text-zinc-300";
}

function shortHash(value?: string): string {
  return value ? value.slice(0, 8) : "";
}

function compactDate(value?: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function summaryValue(summary: RumiLogSummary | null, key: SummaryKey): number {
  return Number(summary?.[key] ?? 0);
}

function normalizedLogEvents(value: unknown): RumiLogEvent[] {
  return Array.isArray(value) ? value : [];
}

function normalizedLogSummary(value: unknown): RumiLogSummary | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as RumiLogSummary : null;
}

function filterKind(filter: LogFilter): string | string[] | null {
  if (filter === "conversation") return ["agent.message", "agent.note"];
  if (filter === "task") return ["task.created", "task.updated", "agent.assigned", "plan.created"];
  if (filter === "git") return ["git.commit", "git.push"];
  return null;
}

function metadataString(event: RumiLogEvent, key: string): string {
  const value = event.metadata?.[key];
  return typeof value === "string" ? value : "";
}

function metadataList(event: RumiLogEvent, key: string): string[] {
  const value = event.metadata?.[key];
  if (Array.isArray(value)) return value.map((item) => String(item)).filter(Boolean);
  if (typeof value === "string" && value.trim()) return [value.trim()];
  return [];
}

function extractMentionsFromText(value: string): string[] {
  const mentions: string[] = [];
  const seen = new Set<string>();
  for (const match of value.matchAll(MENTION_PATTERN)) {
    const mention = match[2];
    if (mention && !seen.has(mention)) {
      seen.add(mention);
      mentions.push(mention);
    }
  }
  return mentions;
}

function eventMentions(event: RumiLogEvent): string[] {
  const fromMetadata = metadataList(event, "mentions");
  if (fromMetadata.length) return fromMetadata;
  return extractMentionsFromText(event.message ?? "");
}

function eventTask(event: RumiLogEvent): RumiTask | null {
  const taskId = metadataString(event, "task_id");
  if (!taskId) return null;
  return {
    taskId,
    title: metadataString(event, "task_title") || event.message || "Local coding task",
    status: metadataString(event, "task_status") || event.status || "open",
    owner: event.actor_id || metadataString(event, "owner") || "local",
  };
}

function extractTaskId(value: string): string | null {
  const match = value.match(TASK_ID_PATTERN);
  return match ? match[0].toUpperCase() : null;
}

function isConversationEvent(event: RumiLogEvent): boolean {
  return CONVERSATION_KINDS.has(event.kind);
}

function statLabel(value: string): string {
  return value.toUpperCase();
}

function MentionChips({ mentions }: { mentions: string[] }) {
  if (!mentions.length) return null;
  return (
    <div className="flex min-w-0 flex-wrap gap-1">
      {mentions.map((mention) => (
        <span
          key={mention}
          className="inline-flex max-w-full items-center gap-1 rounded border border-sky-500/30 bg-sky-500/10 px-1.5 py-0.5 font-mono text-[11px] text-sky-200"
        >
          <AtSign size={10} />
          <span className="truncate">{mention.slice(1)}</span>
        </span>
      ))}
    </div>
  );
}

function ConversationItem({ event, featured = false }: { event: RumiLogEvent; featured?: boolean }) {
  const mentions = eventMentions(event);
  const taskId = metadataString(event, "task_id");
  const taskTitle = metadataString(event, "task_title");
  return (
    <article className="border-l-2 border-sky-400/60 bg-zinc-950/50 py-2 pl-3 pr-2">
      <div className="mb-1.5 flex min-w-0 items-center gap-2">
        <span className="min-w-0 truncate font-mono text-xs font-semibold text-zinc-100">{event.actor_id || "local"}</span>
        {event.agent_role && <span className="truncate text-[11px] text-zinc-500">{event.agent_role}</span>}
        <span className="ml-auto flex-shrink-0 text-[10px] text-zinc-600">{compactDate(event.created_at)}</span>
      </div>
      <p className={cn("whitespace-pre-wrap break-words text-zinc-100", featured ? "text-sm leading-6" : "text-[13px] leading-5")}>
        {event.message || "No message"}
      </p>
      {(mentions.length > 0 || taskId) && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {taskId && (
            <span className="rounded border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 font-mono text-[11px] text-amber-200">
              {taskId}
            </span>
          )}
          <MentionChips mentions={mentions} />
        </div>
      )}
      {taskTitle && <p className="mt-1 break-words text-[11px] leading-4 text-zinc-500">{taskTitle}</p>}
    </article>
  );
}

function TaskItem({ task }: { task: RumiTask }) {
  return (
    <div className="border border-zinc-800 bg-zinc-950/40 px-2 py-1.5">
      <div className="mb-1 flex min-w-0 items-center gap-2">
        <span className="flex-shrink-0 font-mono text-[11px] font-semibold text-amber-200">{task.taskId}</span>
        <span className="min-w-0 truncate text-[11px] text-zinc-500">{task.owner}</span>
        <span className="ml-auto flex-shrink-0 rounded border border-zinc-800 px-1 py-0.5 text-[10px] text-zinc-500">
          {task.status}
        </span>
      </div>
      <p className="break-words text-xs leading-5 text-zinc-200">{task.title}</p>
    </div>
  );
}

export function RumiLogPanel({ workspaceId }: { workspaceId?: string | null }) {
  const [events, setEvents] = useState<RumiLogEvent[]>([]);
  const [summary, setSummary] = useState<RumiLogSummary | null>(null);
  const [note, setNote] = useState("");
  const [filter, setFilter] = useState<LogFilter>("all");
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadLogs = useCallback(async () => {
    setStatus(null);
    setError(null);
    try {
      const result = await codingResources.listRumiLogs({
        workspace_id: workspaceId,
        limit: 30,
        kind: filterKind(filter),
      });
      setEvents(normalizedLogEvents(result.events));
      setSummary(normalizedLogSummary(result.summary));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [filter, workspaceId]);

  useEffect(() => {
    void loadLogs();
  }, [loadLogs]);

  const seedPlan = async () => {
    if (busy) return;
    setBusy(true);
    setStatus(null);
    setError(null);
    try {
      const result = await codingResources.seedRumiLogPlan({ workspace_id: workspaceId });
      setEvents(normalizedLogEvents(result.events));
      setSummary(normalizedLogSummary(result.summary));
      setFilter("all");
      setStatus(result.created ? "agent room created" : "agent room already exists");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const sendMessage = async () => {
    const message = note.trim();
    if (!message || busy) return;
    const mentions = extractMentionsFromText(message);
    const taskId = extractTaskId(message);
    setBusy(true);
    setStatus(null);
    setError(null);
    try {
      const result = await codingResources.appendRumiLog({
        workspace_id: workspaceId,
        kind: "agent.message",
        actor_id: "ui-widget",
        agent_role: "operator",
        status: "said",
        message,
        metadata: {
          mentions,
          ...(taskId ? { task_id: taskId } : {}),
        },
      });
      setEvents(normalizedLogEvents(result.events));
      setSummary(normalizedLogSummary(result.summary));
      setFilter("all");
      setNote("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const agentsLabel = useMemo(() => {
    const agents = summary?.agent_ids ?? [];
    if (!agents.length) return "0";
    return String(agents.length);
  }, [summary]);

  const conversationEvents = useMemo(() => events.filter(isConversationEvent), [events]);
  const featuredConversation = conversationEvents[0] ?? null;
  const tasks = useMemo(() => {
    const byId = new Map<string, RumiTask>();
    for (const event of [...events].reverse()) {
      const task = eventTask(event);
      if (task) byId.set(task.taskId, task);
    }
    return Array.from(byId.values()).reverse().slice(0, 4);
  }, [events]);

  const filterOptions: Array<{ value: LogFilter; label: string }> = [
    { value: "all", label: "All" },
    { value: "conversation", label: "Chat" },
    { value: "task", label: "Tasks" },
    { value: "git", label: "Git" },
  ];

  const stats: Array<{ label: string; value: string | number }> = [
    { label: "chat", value: summaryValue(summary, "conversation_count") },
    { label: "mentions", value: summaryValue(summary, "mention_count") },
    { label: "tasks", value: summaryValue(summary, "task_count") || tasks.length },
    { label: "commits", value: summaryValue(summary, "commit_count") },
    { label: "pushes", value: summaryValue(summary, "push_count") },
  ];

  return (
    <section className="border-b border-zinc-800/60 p-3" aria-label="Rumi local log">
      <div className="mb-3 flex items-center gap-2">
        <History size={14} className="text-amber-300" />
        <h2 className="truncate text-xs font-semibold uppercase text-zinc-400">.rumi</h2>
        {summary?.last_commit_hash && (
          <span className="hidden min-w-0 truncate font-mono text-[10px] text-zinc-600 sm:inline">
            {shortHash(summary.last_commit_hash)}
          </span>
        )}
        <button
          type="button"
          onClick={() => void loadLogs()}
          className="ml-auto flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100"
          title="Refresh .rumi log"
        >
          <RefreshCw size={13} />
        </button>
        <button
          type="button"
          onClick={() => void seedPlan()}
          disabled={busy}
          className="flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100 disabled:cursor-not-allowed disabled:text-zinc-700"
          title="Create local agent room"
        >
          <Sparkles size={13} />
        </button>
      </div>

      <div className="mb-3 grid grid-cols-5 gap-1">
        {stats.map((stat) => (
          <div key={stat.label} className="border border-zinc-800 bg-zinc-950/40 px-1.5 py-1">
            <p className="truncate text-[9px] text-zinc-600">{statLabel(stat.label)}</p>
            <p className="font-mono text-sm text-zinc-200">{stat.value}</p>
          </div>
        ))}
      </div>

      <div className="mb-3">
        <div className="mb-2 flex items-center gap-2">
          <MessageSquare size={15} className="text-sky-300" />
          <h3 className="text-base font-semibold text-zinc-100">AI conversation</h3>
          <span className="ml-auto font-mono text-[11px] text-zinc-600">{summaryValue(summary, "conversation_count")}</span>
        </div>
        <div className="space-y-2">
          {featuredConversation ? (
            <ConversationItem event={featuredConversation} featured />
          ) : (
            <p className="border border-zinc-800 bg-zinc-950/40 px-2 py-3 text-center text-[11px] text-zinc-600">
              No agent conversation yet
            </p>
          )}
        </div>
      </div>

      <div className="mb-3">
        <div className="mb-2 flex items-center gap-2">
          <ListTodo size={15} className="text-amber-300" />
          <h3 className="text-sm font-semibold text-zinc-100">Concrete tasks</h3>
          <span className="ml-auto font-mono text-[11px] text-zinc-600">{summaryValue(summary, "task_count") || tasks.length}</span>
        </div>
        <div className="space-y-1.5">
          {tasks.length > 0 ? (
            tasks.map((task) => <TaskItem key={task.taskId} task={task} />)
          ) : (
            <p className="border border-zinc-800 bg-zinc-950/40 px-2 py-3 text-center text-[11px] text-zinc-600">
              No concrete tasks yet
            </p>
          )}
        </div>
      </div>

      <div className="mb-2 grid grid-cols-4 gap-1 border border-zinc-800 bg-zinc-950/40 p-1">
        {filterOptions.map((option) => (
          <button
            key={option.value}
            type="button"
            onClick={() => setFilter(option.value)}
            className={cn(
              "h-6 rounded text-[10px]",
              filter === option.value
                ? "bg-zinc-100 text-zinc-950"
                : "text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200",
            )}
          >
            {option.label}
          </button>
        ))}
      </div>

      <div className="mb-2 flex items-center gap-1.5">
        <input
          value={note}
          onChange={(event) => setNote(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void sendMessage();
          }}
          className="h-8 min-w-0 flex-1 rounded-md border border-zinc-800 bg-zinc-950/40 px-2 text-[11px] text-zinc-300 outline-none"
          placeholder="message @commit-a1 T-105"
          disabled={busy}
        />
        <button
          type="button"
          onClick={() => void sendMessage()}
          disabled={busy || !note.trim()}
          className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-md bg-zinc-100 text-zinc-950 hover:bg-white disabled:cursor-not-allowed disabled:bg-zinc-800 disabled:text-zinc-600"
          title="Append agent message"
        >
          <Send size={13} />
        </button>
      </div>

      {error ? (
        <ErrorNotice
          className="mb-2 px-2 py-1 text-[11px]"
          copyLabel="Rumi ログエラーをコピー"
          message={error}
        />
      ) : status ? (
        <p role="status" className="mb-2 rounded border border-zinc-700 bg-zinc-900/70 px-2 py-1 text-[11px] text-zinc-300">{status}</p>
      ) : null}

      <div className="mb-2 flex items-center gap-2">
        <h3 className="text-xs font-semibold uppercase text-zinc-500">History</h3>
        <span className="ml-auto font-mono text-[10px] text-zinc-700">{agentsLabel} agents</span>
      </div>
      <div className="space-y-1.5">
        {events.map((event) => {
          const mentions = eventMentions(event);
          const taskId = metadataString(event, "task_id");
          return (
            <div key={event.event_id} className="border border-zinc-800 bg-zinc-950/40 px-2 py-1.5">
              <div className="flex items-center gap-2">
                <span className={cn("flex-shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-semibold", eventAccent(event.kind))}>
                  {eventLabel(event.kind)}
                </span>
                <span className="min-w-0 truncate font-mono text-[11px] text-zinc-300">{event.actor_id || "local"}</span>
                {taskId && <span className="flex-shrink-0 font-mono text-[10px] text-amber-300">{taskId}</span>}
                <span className="ml-auto flex-shrink-0 text-[10px] text-zinc-600">{compactDate(event.created_at)}</span>
              </div>
              {(event.message || event.commit_hash) && (
                <p className="mt-1 break-words text-[11px] leading-4 text-zinc-500">
                  {shortHash(event.commit_hash) ? `${shortHash(event.commit_hash)} - ` : ""}
                  {event.message}
                </p>
              )}
              {mentions.length > 0 && (
                <div className="mt-1.5">
                  <MentionChips mentions={mentions} />
                </div>
              )}
            </div>
          );
        })}
        {events.length === 0 && <p className="py-3 text-center text-[11px] text-zinc-600">No local .rumi events</p>}
      </div>
    </section>
  );
}
