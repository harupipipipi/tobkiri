import { Bot, Plus, Save, Wrench } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { CompanyAgent, CompanyInboxItem, CompanyRunLink } from "../../lib/api";
import { CompanyRunConversation } from "./CompanyRunConversation";

export function CompanyAgentList({
  agents,
  runs = [],
  inboxItems = [],
  expectedAgentCount,
  busy = false,
  onUpsertAgent,
}: {
  agents: CompanyAgent[];
  runs?: CompanyRunLink[];
  inboxItems?: CompanyInboxItem[];
  expectedAgentCount?: number;
  busy?: boolean;
  onUpsertAgent?: (agent: Partial<CompanyAgent>) => void;
}) {
  const [modelDrafts, setModelDrafts] = useState<Record<string, string>>({});
  const [newAgentId, setNewAgentId] = useState("");
  const [newAgentModel, setNewAgentModel] = useState("stub/default");

  useEffect(() => {
    setModelDrafts(Object.fromEntries(agents.map((agent) => [agent.agent_id, agent.model ?? "stub/default"])));
  }, [agents]);

  const activityByAgent = useMemo(() => {
    const map = new Map<string, { latestRun?: CompanyRunLink; openInboxCount: number; latestInbox?: CompanyInboxItem }>();
    for (const run of runs) {
      if (!map.has(run.agent_id)) map.set(run.agent_id, { openInboxCount: 0 });
      const activity = map.get(run.agent_id);
      if (activity && !activity.latestRun) activity.latestRun = run;
    }
    for (const item of inboxItems) {
      if (!map.has(item.agent_id)) map.set(item.agent_id, { openInboxCount: 0 });
      const activity = map.get(item.agent_id);
      if (!activity) continue;
      if (item.status !== "consumed") activity.openInboxCount += 1;
      if (!activity.latestInbox) activity.latestInbox = item;
    }
    return map;
  }, [inboxItems, runs]);
  const displayedAgentCount = Math.max(agents.length, expectedAgentCount ?? 0);

  return (
    <section className="space-y-2 p-2">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Main Agent &amp; Subagents</h4>
        <span className="text-[10px] text-zinc-600">{displayedAgentCount}</span>
      </div>
      {onUpsertAgent && (
        <form
          className="grid grid-cols-[minmax(0,0.75fr)_minmax(0,1fr)_28px] gap-1.5"
          onSubmit={(event) => {
            event.preventDefault();
            const agentId = newAgentId.trim();
            if (!agentId) return;
            onUpsertAgent({
              agent_id: agentId,
              role_key: agentId,
              agent_name: agentId,
              display_name: agentId,
              model: newAgentModel.trim() || "stub/default",
              allowed_tools: [],
              agent_kind: "subagent",
              runtime_kind: "agent_run",
              subagent_role: "custom",
              placement_id: `${agentId}-subagent`,
              system_prompt:
                "You are a custom Subagent delegated by Tobkiri's Main Agent. Treat the assigned task as a bounded user instruction and use only the capabilities in your Effective Subagent Plan.",
            });
            setNewAgentId("");
          }}
        >
          <input
            value={newAgentId}
            onChange={(event) => setNewAgentId(event.target.value)}
            disabled={busy}
            placeholder="subagent id"
            className="h-8 min-w-0 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[12px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
          />
          <input
            value={newAgentModel}
            onChange={(event) => setNewAgentModel(event.target.value)}
            disabled={busy}
            placeholder="model"
            className="h-8 min-w-0 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[11px] text-zinc-300 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
          />
          <button
            type="submit"
            disabled={busy || !newAgentId.trim()}
            className="flex h-8 w-8 items-center justify-center rounded-md bg-zinc-100 text-zinc-950 hover:bg-white disabled:opacity-30"
            title="Create Subagent"
            aria-label="Create Subagent"
          >
            <Plus size={13} />
          </button>
        </form>
      )}
      <div className="space-y-1.5">
        {agents.map((agent) => {
          const activity = activityByAgent.get(agent.agent_id);
          const status = activity?.latestRun?.status ?? agent.status ?? "idle";
          const modelDraft = modelDrafts[agent.agent_id] ?? agent.model ?? "stub/default";
          const modelChanged = modelDraft !== (agent.model ?? "stub/default");
          const activityMessage = activity?.latestRun?.agent_run?.result_preview || activity?.latestRun?.agent_run?.error;
          return (
            <div key={agent.agent_id} className="rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-1.5">
              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  <Bot size={13} className="flex-shrink-0 text-zinc-500" />
                  <span className="truncate text-[12px] font-medium text-zinc-200">
                    {agent.display_name || agent.agent_name || agent.agent_id}
                  </span>
                  <span className="flex-shrink-0 rounded border border-zinc-800 px-1 py-0.5 text-[8px] uppercase tracking-wide text-zinc-500">
                    {agent.agent_kind === "main" ? "Main" : "Subagent"}
                  </span>
                </div>
                <span className="flex-shrink-0 rounded border border-zinc-800 px-1.5 py-0.5 text-[9px] text-zinc-500">
                  {status}
                </span>
              </div>
              <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-zinc-500">
                {onUpsertAgent ? (
                  <div className="flex min-w-0 flex-1 items-center gap-1">
                    <input
                      value={modelDraft}
                      onChange={(event) => setModelDrafts((current) => ({ ...current, [agent.agent_id]: event.target.value }))}
                      disabled={busy}
                      className="h-7 min-w-0 flex-1 rounded border border-zinc-800 bg-zinc-950 px-1.5 font-mono text-[10px] text-zinc-300 outline-none focus:border-zinc-600"
                      aria-label={`Model for ${agent.agent_id}`}
                    />
                    <button
                      type="button"
                      disabled={busy || !modelChanged || !modelDraft.trim()}
                      onClick={() => onUpsertAgent({ ...agent, model: modelDraft.trim() })}
                      className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded border border-zinc-800 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-30"
                      title="Save agent model"
                      aria-label={`Save model for ${agent.agent_id}`}
                    >
                      <Save size={11} />
                    </button>
                  </div>
                ) : (
                  <span className="truncate font-mono">{agent.model ?? "stub/default"}</span>
                )}
                <span className="flex flex-shrink-0 items-center gap-1">
                  <Wrench size={10} />
                  {(agent.allowed_tools ?? []).length}
                </span>
              </div>
              {(activity?.latestRun || activity?.openInboxCount) && (
                <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-zinc-500">
                  <span className="truncate">{activity.latestRun?.run_id ?? activity.latestInbox?.kind}</span>
                  <span className="flex-shrink-0 rounded border border-zinc-800 px-1 py-0.5">inbox {activity.openInboxCount}</span>
                </div>
              )}
              <CompanyRunConversation
                messages={activity?.latestRun?.agent_run?.conversation}
                fallback={activityMessage}
                fallbackError={Boolean(activity?.latestRun?.agent_run?.error && !activity?.latestRun?.agent_run?.result_preview)}
              />
              {agent.aliases && agent.aliases.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-1">
                  {agent.aliases.slice(0, 4).map((alias) => (
                    <span key={alias} className="rounded border border-zinc-800 bg-zinc-900/60 px-1 py-0.5 text-[9px] text-zinc-500">
                      @{alias}
                    </span>
                  ))}
                </div>
              )}
            </div>
          );
        })}
        {agents.length === 0 && (
          <div className="rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-2 text-[11px] text-zinc-500">
            {displayedAgentCount > 0
              ? `${displayedAgentCount} Agents configured. Refreshing Placement details...`
              : "No Subagents configured."}
          </div>
        )}
      </div>
    </section>
  );
}
