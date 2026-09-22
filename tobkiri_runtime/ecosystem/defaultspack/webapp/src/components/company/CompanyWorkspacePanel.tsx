import { AlertTriangle, Bot, ClipboardList, MessageSquare, Route, Settings, Share2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import type {
  CompanyAgent,
  CompanyChannel,
  CompanyInboxItem,
  CompanyInboundRoute,
  CompanyMessage,
  CompanyRecord,
  CompanyRunLink,
  CompanyTask,
  P2PIdentity,
  P2PPeer,
  P2PStatusResponse,
} from "../../lib/api";
import { arrayFromRecord, companyResources } from "../../features/company/resources/companyResources";
import { CompanyAgentList } from "./CompanyAgentList";
import { CompanyChannelView } from "./CompanyChannelView";
import { CompanyInboundRoutesPanel } from "./CompanyInboundRoutesPanel";
import { CompanyP2PPanel } from "./CompanyP2PPanel";
import { CompanySettingsPanel } from "./CompanySettingsPanel";
import { CompanyTaskBoard } from "./CompanyTaskBoard";
import { CompanyTree } from "./CompanyTree";

type CompanyTab = "tasks" | "channels" | "agents" | "routes" | "settings" | "p2p";

const TABS: Array<{ id: CompanyTab; label: string; icon: typeof ClipboardList }> = [
  { id: "tasks", label: "Tasks", icon: ClipboardList },
  { id: "channels", label: "Channels", icon: MessageSquare },
  { id: "agents", label: "Agents", icon: Bot },
  { id: "routes", label: "Routes", icon: Route },
  { id: "settings", label: "Settings", icon: Settings },
  { id: "p2p", label: "P2P", icon: Share2 },
];

const PRIMARY_TAB_IDS = new Set<CompanyTab>(["tasks", "channels", "agents"]);
const PRIMARY_TABS = TABS.filter((tab) => PRIMARY_TAB_IDS.has(tab.id));
const OVERFLOW_TABS = TABS.filter((tab) => !PRIMARY_TAB_IDS.has(tab.id));
export const MIMO_CODING_COMPANY_ID = "mimo-coding-company";
export const OPERATIONS_COMPANY_ID = "operations-company";

type P2PDetailResources = Pick<typeof companyResources, "getP2PIdentity" | "listP2PPeers">;

export async function loadEnabledP2PDetails(
  status: P2PStatusResponse | null,
  resources: P2PDetailResources = companyResources,
): Promise<{ identity: P2PIdentity | null; peers: P2PPeer[] }> {
  if (!status?.p2p?.enabled) return { identity: null, peers: [] };
  const [identityResult, peersResult] = await Promise.allSettled([
    resources.getP2PIdentity(),
    resources.listP2PPeers(),
  ]);
  return {
    identity: identityResult.status === "fulfilled" ? identityResult.value.identity : null,
    peers: peersResult.status === "fulfilled" ? peersResult.value.peers : [],
  };
}

function textValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function companyIdFromHint(value: unknown): string | null {
  const text = textValue(value);
  if (!text) return null;
  const companyPrefix = "company:";
  if (text.toLowerCase().startsWith(companyPrefix)) {
    return text.slice(companyPrefix.length).trim() || null;
  }
  return text;
}

export function companyIdFromGroupHint(value: unknown): string | null {
  const text = textValue(value);
  if (!text) return null;
  const companyPrefix = "company:";
  if (text.toLowerCase().startsWith(companyPrefix)) {
    return text.slice(companyPrefix.length).trim() || null;
  }
  return null;
}

export function companyIdFromConversationTitle(value: unknown): string | null {
  const text = textValue(value).toLowerCase();
  if (!text) return null;
  const withoutGroupPrefix = text.startsWith("company:") ? text.slice("company:".length).trim() : text;
  const normalized = withoutGroupPrefix.startsWith("[stale] ") ? withoutGroupPrefix.slice("[stale] ".length).trim() : withoutGroupPrefix;
  if (normalized === "mimo coding company" || normalized.startsWith("mimo coding company:")) {
    return MIMO_CODING_COMPANY_ID;
  }
  if (normalized === "operations company" || normalized.startsWith("operations company:")) {
    return OPERATIONS_COMPANY_ID;
  }
  return null;
}

export function resolveCompanyWorkspaceHint({
  companyId,
  groupId,
  conversationKind,
  profileId,
  tags,
}: {
  companyId?: unknown;
  groupId?: unknown;
  conversationKind?: unknown;
  profileId?: unknown;
  tags?: unknown;
}): string | null {
  const directCompanyId = companyIdFromHint(companyId);
  if (directCompanyId) return directCompanyId;
  const groupedCompanyId = companyIdFromGroupHint(groupId);
  if (groupedCompanyId) return groupedCompanyId;

  const kind = textValue(conversationKind);
  const profile = textValue(profileId);
  const tagList = Array.isArray(tags) ? tags.map((tag) => textValue(tag)).filter(Boolean) : [];
  if (kind === "mimo_coding_company" || profile === "defaultspack.mimo_coding_company" || tagList.includes(MIMO_CODING_COMPANY_ID)) {
    return MIMO_CODING_COMPANY_ID;
  }
  if (kind === "operations_company" || profile === "defaultspack.operations_company" || tagList.includes(OPERATIONS_COMPANY_ID)) {
    return OPERATIONS_COMPANY_ID;
  }
  return null;
}

type CompanyHintChat = {
  companyId?: unknown;
  conversationKind?: unknown;
  metadata?: Record<string, unknown> | null;
  tags?: unknown;
};

type CompanyHintGroup = {
  id?: unknown;
  sourceGroupId?: unknown;
  chats?: CompanyHintChat[];
  subGroups?: CompanyHintGroup[];
};

export function resolveCompanyWorkspaceHintFromGroup(group: CompanyHintGroup | null | undefined): string | null {
  if (!group) return null;
  const groupCompanyId = companyIdFromGroupHint(group.sourceGroupId) ?? companyIdFromGroupHint(group.id);
  if (groupCompanyId) return groupCompanyId;

  for (const chat of group.chats ?? []) {
    const metadata = chat.metadata && typeof chat.metadata === "object" ? chat.metadata : {};
    const resolved = resolveCompanyWorkspaceHint({
      companyId: chat.companyId ?? metadata.company_id ?? metadata.companyId,
      groupId: metadata.group_id ?? metadata.groupId,
      conversationKind: chat.conversationKind,
      profileId: metadata.profile_id,
      tags: chat.tags,
    });
    if (resolved) return resolved;
  }

  for (const subGroup of group.subGroups ?? []) {
    const resolved = resolveCompanyWorkspaceHintFromGroup(subGroup);
    if (resolved) return resolved;
  }
  return null;
}

export function resolveActiveChannelId(
  currentChannelId: string | null | undefined,
  channels: Pick<CompanyChannel, "id">[],
  fallbackChannelId = "ops-company",
): string {
  const current = textValue(currentChannelId);
  if (current && channels.some((channel) => channel.id === current)) return current;
  if (channels.some((channel) => channel.id === fallbackChannelId)) return fallbackChannelId;
  return channels[0]?.id || fallbackChannelId;
}

export function resolveCompanyMessageListOptions(
  channels: Pick<CompanyChannel, "id">[],
  resolvedChannelId: string,
): { channel_id?: string; limit: number; order: "desc" } {
  const options = { limit: 80, order: "desc" as const };
  if (channels.some((channel) => channel.id === resolvedChannelId)) {
    return { ...options, channel_id: resolvedChannelId };
  }
  return options;
}

export function resolveSelectedCompanyId({
  activeConversationId,
  activeCompanyId,
  hintedCompanyId,
  statusCompany,
  statusCompanyId,
  companies,
}: {
  activeConversationId?: string | null;
  activeCompanyId?: string | null;
  hintedCompanyId?: string | null;
  statusCompany?: CompanyRecord | null;
  statusCompanyId?: string | null;
  companies: CompanyRecord[];
}): string | null {
  const normalizedHint = companyIdFromHint(hintedCompanyId);
  if (normalizedHint) return normalizedHint;
  const statusCompanyTitleHint = companyIdFromConversationTitle(statusCompany?.name);
  if (statusCompanyTitleHint) return statusCompanyTitleHint;
  const normalizedStatusCompanyId = companyIdFromHint(statusCompanyId);
  if (normalizedStatusCompanyId) return normalizedStatusCompanyId;
  if (activeConversationId) return statusCompany?.id ?? null;
  return activeCompanyId ?? companies[0]?.id ?? statusCompany?.id ?? null;
}

export function resolveEffectiveCompanies({
  activeConversationId,
  activeCompanyIdHint,
  activeCompany,
  companies,
}: {
  activeConversationId?: string | null;
  activeCompanyIdHint?: string | null;
  activeCompany?: CompanyRecord | null;
  companies: CompanyRecord[];
}): CompanyRecord[] {
  const normalizedHint = companyIdFromHint(activeCompanyIdHint);
  if (!activeCompany) return companies;
  const ordered = [activeCompany, ...companies.filter((item) => item.id !== activeCompany.id)];
  if (activeConversationId && !normalizedHint) return ordered;
  return ordered;
}

function researchSources(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item))).slice(0, 5);
}

function researchTaskDescription(query: string, sources: Array<Record<string, unknown>>): string {
  const lines = [
    "Deep research request delegated from the Main Agent chat.",
    `Search query: ${query}`,
  ];
  if (sources.length > 0) {
    lines.push("", "DuckDuckGo sources:");
    sources.forEach((source, index) => {
      lines.push(`${index + 1}. ${textValue(source.title) || textValue(source.url) || "Untitled source"}`);
      if (textValue(source.url)) lines.push(`   ${textValue(source.url)}`);
      if (textValue(source.summary)) lines.push(`   ${textValue(source.summary)}`);
    });
  } else {
    lines.push("", "DuckDuckGo returned no sources. Continue with explicit uncertainty.");
  }
  return lines.join("\n");
}

function settledErrorMessage(label: string, result: PromiseSettledResult<unknown>): string | null {
  if (result.status !== "rejected") return null;
  const detail = result.reason instanceof Error ? result.reason.message : "unavailable";
  return `${label}: ${detail}`;
}

function preferLoadedCompanyResource<T>(loaded: T[] | null, fallback: T[]): T[] {
  if (loaded && (loaded.length > 0 || fallback.length === 0)) return loaded;
  return fallback;
}

function mergeCompanyRecords(primary: CompanyRecord | null, fallback: CompanyRecord | null): CompanyRecord | null {
  if (!primary) return fallback;
  if (!fallback) return primary;
  return {
    ...fallback,
    ...primary,
    agents: primary.agents ?? fallback.agents,
    channels: primary.channels ?? fallback.channels,
    messages: primary.messages ?? fallback.messages,
    tasks: primary.tasks ?? fallback.tasks,
    inbound_routes: primary.inbound_routes ?? fallback.inbound_routes,
    settings: primary.settings ?? fallback.settings,
    metadata: {
      ...(fallback.metadata ?? {}),
      ...(primary.metadata ?? {}),
    },
  };
}

function companyNameFromId(companyId: string): string {
  if (companyId === MIMO_CODING_COMPANY_ID) return "MiMo Coding Company";
  if (companyId === OPERATIONS_COMPANY_ID) return "Tobkiri Operations Team";
  return companyId
    .split("-")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ") || companyId;
}

export function resolveSelectedCompanyRecord({
  selectedId,
  selectedCompanyDetails,
  statusCompany,
  listedSelectedCompany,
}: {
  selectedId?: string | null;
  selectedCompanyDetails?: CompanyRecord | null;
  statusCompany?: CompanyRecord | null;
  listedSelectedCompany?: CompanyRecord | null;
}): CompanyRecord | null {
  if (!selectedId) return null;
  return mergeCompanyRecords(selectedCompanyDetails ?? null, statusCompany ?? listedSelectedCompany ?? null) ?? {
    id: selectedId,
    name: companyNameFromId(selectedId),
  };
}

export function enrichCompanyRecordWithLoadedResources(company: CompanyRecord, {
  agents,
  channels,
  tasks,
  routes,
}: {
  agents?: CompanyAgent[];
  channels?: CompanyChannel[];
  tasks?: CompanyTask[];
  routes?: CompanyInboundRoute[];
}): CompanyRecord {
  return {
    ...company,
    ...(agents ? { agents, agent_count: Math.max(company.agent_count ?? 0, agents.length) } : {}),
    ...(channels ? { channels, channel_count: Math.max(company.channel_count ?? 0, channels.length) } : {}),
    ...(tasks ? { tasks, task_count: Math.max(company.task_count ?? 0, tasks.length) } : {}),
    ...(routes ? { inbound_routes: routes } : {}),
  };
}

export function CompanyWorkspacePanel({
  activeConversationId = null,
  activeConversationTitle = null,
  activeCompanyIdHint = null,
}: {
  activeConversationId?: string | null;
  activeConversationTitle?: string | null;
  activeCompanyIdHint?: string | null;
}) {
  const [companies, setCompanies] = useState<CompanyRecord[]>([]);
  const [activeCompanyId, setActiveCompanyId] = useState<string | null>(() => companyIdFromHint(activeCompanyIdHint));
  const [company, setCompany] = useState<CompanyRecord | null>(null);
  const [agents, setAgents] = useState<CompanyAgent[]>([]);
  const [channels, setChannels] = useState<CompanyChannel[]>([]);
  const [messages, setMessages] = useState<CompanyMessage[]>([]);
  const [tasks, setTasks] = useState<CompanyTask[]>([]);
  const [runs, setRuns] = useState<CompanyRunLink[]>([]);
  const [inboxItems, setInboxItems] = useState<CompanyInboxItem[]>([]);
  const [routes, setRoutes] = useState<CompanyInboundRoute[]>([]);
  const [p2pStatus, setP2PStatus] = useState<P2PStatusResponse | null>(null);
  const [p2pIdentity, setP2PIdentity] = useState<P2PIdentity | null>(null);
  const [peers, setPeers] = useState<P2PPeer[]>([]);
  const [activeChannelId, setActiveChannelId] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<CompanyTab>("tasks");
  const [isMoreMenuOpen, setIsMoreMenuOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hasActiveConversation = Boolean(activeConversationId);
  const isOverflowTabActive = OVERFLOW_TABS.some((tab) => tab.id === activeTab);
  const titleCompanyIdHint = companyIdFromConversationTitle(activeCompanyIdHint) ?? companyIdFromConversationTitle(activeConversationTitle);
  const normalizedActiveCompanyIdHint = (
    titleCompanyIdHint
    ?? companyIdFromHint(activeCompanyIdHint)
  );

  const effectiveCompanies = useMemo(() => resolveEffectiveCompanies({
    activeConversationId,
    activeCompanyIdHint: normalizedActiveCompanyIdHint,
    activeCompany: company,
    companies,
  }), [activeConversationId, companies, company, normalizedActiveCompanyIdHint]);

  const loadCompany = useCallback(async (requestedCompanyId?: string | null, requestedChannelId?: string | null) => {
    setBusy(true);
    setError(null);
    try {
      const requestedCompanyWasProvided = requestedCompanyId !== undefined;
      const hintedCompanyId = requestedCompanyWasProvided
        ? companyIdFromHint(requestedCompanyId)
        : normalizedActiveCompanyIdHint;
      const activeCompanyCandidate = requestedCompanyWasProvided ? null : activeCompanyId;

      const statusTarget = hintedCompanyId
        ? hintedCompanyId
        : activeConversationId
          ? { conversationId: activeConversationId, bootstrap: true }
          : activeCompanyCandidate ?? null;
      const statusRequest = statusTarget
        ? companyResources.getCompanyStatus(statusTarget)
        : Promise.resolve({ bootstrapped: false, company_id: "", company: null });
      const [companyListResult, statusResult, p2pStatusResult] = await Promise.allSettled([
        companyResources.listCompanies(),
        statusRequest,
        companyResources.getP2PStatus(),
      ]);

      const listedCompanies = companyListResult.status === "fulfilled" ? companyListResult.value.companies : [];
      const statusCompanyId = statusResult.status === "fulfilled" ? statusResult.value.company_id : null;
      let statusCompany = statusResult.status === "fulfilled" ? statusResult.value.company ?? null : null;
      const selectedId = resolveSelectedCompanyId({
        activeConversationId,
        activeCompanyId: activeCompanyCandidate,
        hintedCompanyId,
        statusCompany,
        statusCompanyId,
        companies: listedCompanies,
      });
      if (statusCompany?.id && statusCompany.id !== selectedId) {
        statusCompany = null;
      }
      let selectedCompanyDetails: CompanyRecord | null = null;
      let companyDetailsResult: PromiseSettledResult<CompanyRecord> | null = null;
      if (selectedId) {
        const [detailsResult] = await Promise.allSettled([
          companyResources.getCompany(selectedId),
        ]);
        companyDetailsResult = detailsResult;
        if (detailsResult.status === "fulfilled") {
          selectedCompanyDetails = detailsResult.value;
        }
      }
      if (!statusCompany && selectedId) {
        statusCompany = listedCompanies.find((item) => item.id === selectedId) ?? selectedCompanyDetails;
      }
      const listedSelectedCompany = selectedId
        ? listedCompanies.find((item) => item.id === selectedId) ?? null
        : null;
      let selectedCompany = resolveSelectedCompanyRecord({
        selectedId,
        selectedCompanyDetails,
        statusCompany,
        listedSelectedCompany,
      });
      const selectedCompanyId = selectedCompany?.id;
      const visibleCompanies = selectedCompany && selectedCompanyId
        ? [selectedCompany, ...listedCompanies.filter((item) => item.id !== selectedCompanyId)]
        : listedCompanies;
      setCompanies(visibleCompanies);
      setActiveCompanyId(selectedId);
      setCompany(selectedCompany);

      const nextP2PStatus = p2pStatusResult.status === "fulfilled" ? p2pStatusResult.value : null;
      setP2PStatus(nextP2PStatus);
      const p2pDetails = await loadEnabledP2PDetails(nextP2PStatus);
      setP2PIdentity(p2pDetails.identity);
      setPeers(p2pDetails.peers);

      const loadErrors = [
        settledErrorMessage("Company list", companyListResult),
        settledErrorMessage("Company status", statusResult),
      ].filter((message): message is string => Boolean(message));
      const companyDetailsError = companyDetailsResult ? settledErrorMessage("Company details", companyDetailsResult) : null;
      if (companyDetailsError && !selectedCompany) loadErrors.push(companyDetailsError);

      if (selectedId) {
        const [agentResult, channelResult, taskResult, routeResult, runResult] = await Promise.allSettled([
          companyResources.listCompanyAgents(selectedId),
          companyResources.listCompanyChannels(selectedId),
          companyResources.listCompanyTasks(selectedId),
          companyResources.listCompanyInboundRoutes(selectedId),
          companyResources.listCompanyRuns(selectedId, { limit: 80 }),
        ]);
        const fallbackAgents = arrayFromRecord(selectedCompany?.agents);
        const nextAgents = preferLoadedCompanyResource(
          agentResult.status === "fulfilled" ? agentResult.value.agents : null,
          fallbackAgents,
        );
        setAgents(nextAgents);
        const fallbackChannels = arrayFromRecord(selectedCompany?.channels);
        const nextChannels = preferLoadedCompanyResource(
          channelResult.status === "fulfilled" ? channelResult.value.channels : null,
          fallbackChannels,
        );
        setChannels(nextChannels);
        const resolvedChannelId = resolveActiveChannelId(requestedChannelId ?? activeChannelId, nextChannels);
        setActiveChannelId(resolvedChannelId);
        const fallbackTasks = arrayFromRecord(selectedCompany?.tasks);
        const nextTasks = preferLoadedCompanyResource(
          taskResult.status === "fulfilled" ? taskResult.value.tasks : null,
          fallbackTasks,
        );
        setTasks(nextTasks);
        const fallbackRoutes = arrayFromRecord(selectedCompany?.inbound_routes);
        const nextRoutes = preferLoadedCompanyResource(
          routeResult.status === "fulfilled" ? routeResult.value.routes : null,
          fallbackRoutes,
        );
        setRoutes(nextRoutes);
        if (selectedCompany) {
          selectedCompany = enrichCompanyRecordWithLoadedResources(selectedCompany, {
            agents: nextAgents,
            channels: nextChannels,
            tasks: nextTasks,
            routes: nextRoutes,
          });
          setCompany(selectedCompany);
          setCompanies([selectedCompany, ...listedCompanies.filter((item) => item.id !== selectedCompany?.id)]);
        }
        const [messageResult] = await Promise.allSettled([
          companyResources.listCompanyMessages(selectedId, resolveCompanyMessageListOptions(nextChannels, resolvedChannelId)),
        ]);
        let loadedMessages = messageResult.status === "fulfilled" ? messageResult.value.messages : null;
        let messageRetryResult: PromiseSettledResult<{ messages: CompanyMessage[]; total: number }> | null = null;
        const selectedChannel = nextChannels.find((channel) => channel.id === resolvedChannelId);
        const selectedChannelMessageCount = typeof selectedChannel?.message_count === "number" ? selectedChannel.message_count : 0;
        if ((loadedMessages?.length ?? 0) === 0 && selectedChannelMessageCount > 0) {
          [messageRetryResult] = await Promise.allSettled([
            companyResources.listCompanyMessages(selectedId, { limit: 80, order: "desc" }),
          ]);
          if (messageRetryResult.status === "fulfilled" && messageRetryResult.value.messages.length > 0) {
            loadedMessages = messageRetryResult.value.messages;
          }
        }
        const fallbackMessages = arrayFromRecord(selectedCompany?.messages);
        setMessages(preferLoadedCompanyResource(loadedMessages, fallbackMessages));
        setRuns(runResult.status === "fulfilled" ? runResult.value.runs : []);
        const channelError = settledErrorMessage("Channels", channelResult);
        const messageError = settledErrorMessage("Messages", messageResult);
        const messageRetryError = messageRetryResult ? settledErrorMessage("Messages retry", messageRetryResult) : null;
        if (channelError) loadErrors.push(channelError);
        if (messageError) loadErrors.push(messageError);
        if (messageRetryError) loadErrors.push(messageRetryError);
        const inboxResults = await Promise.allSettled(
          nextAgents.map((agent) => companyResources.listCompanyAgentInbox(selectedId, agent.agent_id, { limit: 20 })),
        );
        setInboxItems(
          inboxResults.flatMap((result) => result.status === "fulfilled" ? result.value.inbox : []),
        );
      } else {
        setAgents([]);
        setChannels([]);
        setTasks([]);
        setRuns([]);
        setInboxItems([]);
        setRoutes([]);
        setMessages([]);
      }

      if (loadErrors.length > 0) {
        setError(loadErrors.join(" "));
      }
    } finally {
      setBusy(false);
    }
  }, [activeChannelId, activeCompanyId, activeConversationId, normalizedActiveCompanyIdHint]);

  useEffect(() => {
    setActiveCompanyId(normalizedActiveCompanyIdHint);
    void loadCompany(normalizedActiveCompanyIdHint);
  }, [activeConversationId, normalizedActiveCompanyIdHint]);

  useEffect(() => {
    if (!titleCompanyIdHint || activeCompanyId === titleCompanyIdHint) return;
    setActiveCompanyId(titleCompanyIdHint);
    void loadCompany(titleCompanyIdHint);
  }, [activeCompanyId, loadCompany, titleCompanyIdHint]);

  useEffect(() => {
    if (!activeCompanyId) return undefined;
    const intervalId = window.setInterval(() => {
      void loadCompany(activeCompanyId);
    }, 30000);
    return () => window.clearInterval(intervalId);
  }, [activeCompanyId, loadCompany]);

  const activeCompany = company ?? effectiveCompanies.find((item) => item.id === activeCompanyId) ?? null;

  const run = async (work: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
      await loadCompany(activeCompanyId);
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Company action failed.");
    } finally {
      setBusy(false);
    }
  };

  const selectTab = (tabId: CompanyTab) => {
    setActiveTab(tabId);
    setIsMoreMenuOpen(false);
  };

  const renderTab = () => {
    if (!activeCompanyId && activeTab !== "p2p") {
      return <div className="p-3 text-[12px] text-zinc-500">Start or send a chat message to create its Subagent Team.</div>;
    }
    switch (activeTab) {
      case "channels":
        return (
          <CompanyChannelView
            channels={channels}
            messages={messages}
            activeChannelId={activeChannelId}
            busy={busy}
            onChannelChange={(channelId) => {
              if (!activeCompanyId) return;
              setActiveChannelId(channelId);
              void loadCompany(activeCompanyId, channelId);
            }}
            onSendMessage={(content, channelId) => activeCompanyId && void run(() => companyResources.sendCompanyMessage(activeCompanyId, { content, channel_id: channelId, sender_id: "user" }))}
          />
        );
      case "agents":
        return (
          <CompanyAgentList
            agents={agents}
            runs={runs}
            inboxItems={inboxItems}
            expectedAgentCount={activeCompany?.agent_count}
            busy={busy}
            onUpsertAgent={(agent) => activeCompanyId && void run(() => companyResources.upsertCompanyAgent(activeCompanyId, agent))}
          />
        );
      case "routes":
        return (
          <CompanyInboundRoutesPanel
            routes={routes}
            busy={busy}
            onUpsertRoute={(route) => activeCompanyId && void run(() => companyResources.upsertCompanyInboundRoute(activeCompanyId, route))}
            onDeleteRoute={(routeId) => activeCompanyId && void run(() => companyResources.deleteCompanyInboundRoute(activeCompanyId, routeId))}
          />
        );
      case "settings":
        return (
          <CompanySettingsPanel
            settings={activeCompany?.settings ?? {}}
            busy={busy}
            onSave={(settings) => activeCompanyId && void run(() => companyResources.updateCompanySettings(activeCompanyId, settings))}
          />
        );
      case "p2p":
        return (
          <CompanyP2PPanel
            status={p2pStatus}
            identity={p2pIdentity}
            peers={peers}
            busy={busy}
            onStartPairing={(peerLabel) => void run(() => companyResources.startP2PPairing({ peer_label: peerLabel, allowed_company_ids: activeCompanyId ? [activeCompanyId] : undefined }))}
            onSendMessage={(peerId, text) => void run(() => companyResources.sendP2PMessage(peerId, { text, body: { text, company_id: activeCompanyId ?? undefined } }))}
          />
        );
      case "tasks":
      default:
        return (
          <CompanyTaskBoard
            tasks={tasks}
            agents={agents}
            runs={runs}
            expectedTaskCount={activeCompany?.task_count}
            busy={busy}
            onCreateTask={(title, targetAgentIds) => activeCompanyId && void run(() => companyResources.createCompanyTask(activeCompanyId, {
              title,
              target_agent_ids: targetAgentIds,
              source: "president",
              metadata: {
                ...(activeConversationId ? { conversation_id: activeConversationId } : {}),
                ...(activeConversationTitle ? { source_chat_title: activeConversationTitle } : {}),
                source_message: title,
              },
            }))}
            onCreateResearchTask={(query, targetAgentIds) => activeCompanyId && void run(async () => {
              const searchResult = await companyResources.webSearch(query, true);
              const sources = researchSources(searchResult.sources);
              return companyResources.createCompanyTask(activeCompanyId, {
                title: `Deep research: ${query}`,
                description: researchTaskDescription(query, sources),
                target_agent_ids: targetAgentIds.length > 0 ? targetAgentIds : ["research_specialist"],
                source: "president_deep_research",
                metadata: {
                  ...(activeConversationId ? { conversation_id: activeConversationId } : {}),
                  ...(activeConversationTitle ? { source_chat_title: activeConversationTitle } : {}),
                  source_message: query,
                  research_query: query,
                  search_provider: textValue(searchResult.provider) || "external_web",
                  sources,
                },
              });
            })}
            onUpdateTask={(taskId, updates) => activeCompanyId && void run(() => companyResources.updateCompanyTask(activeCompanyId, taskId, updates))}
            onDeleteTask={(taskId) => activeCompanyId && void run(() => companyResources.deleteCompanyTask(activeCompanyId, taskId))}
            onDispatchTask={(taskId) => activeCompanyId && void run(() => companyResources.dispatchCompanyTask(activeCompanyId, taskId))}
          />
        );
    }
  };

  return (
    <div className="relative flex h-full min-h-0 flex-col bg-[#0a0a0c] text-zinc-300">
      <div className="border-b border-zinc-800/60 px-3 py-2">
        <p className="truncate text-[13px] font-medium text-zinc-100">Main Agent &amp; Subagents</p>
        <p className="truncate text-[10px] text-zinc-600">
          {activeConversationTitle || activeConversationId || activeCompany?.name || "start a chat to create a Subagent Team"}
        </p>
        {(activeCompany?.id || activeCompanyId) && (
          <p
            className="truncate font-mono text-[9px] text-zinc-600"
            title={activeCompany?.id || activeCompanyId || undefined}
          >
            Company ID: {activeCompany?.id || activeCompanyId}
          </p>
        )}
      </div>

      {error && (
        <div className="m-2 flex items-start gap-2 rounded-md border border-amber-500/20 bg-amber-500/10 px-2 py-1.5 text-[11px] text-amber-200">
          <AlertTriangle size={13} className="mt-0.5 flex-shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <CompanyTree
        companies={effectiveCompanies}
        activeCompanyId={activeCompanyId}
        activeTaskCount={Math.max(tasks.length, activeCompany?.task_count ?? 0)}
        busy={busy}
        emptyMessage={hasActiveConversation ? "No Subagent Team loaded." : "Start or send a chat message to create its Subagent Team."}
        onSelect={(companyId) => void loadCompany(companyId)}
        onBootstrap={hasActiveConversation ? () => void run(() => companyResources.bootstrapCompanyWorkspace(
          {
            source: "webapp",
            name: "Executive Team",
            ...(activeConversationId ? { conversation_id: activeConversationId, scope: "conversation" } : {}),
          },
          activeConversationId ? { conversationId: activeConversationId, scope: "conversation" } : undefined,
        )) : undefined}
        onRefresh={() => void loadCompany(activeCompanyId)}
      />

      <div className="grid grid-cols-3 gap-1 border-b border-zinc-800/60 p-2">
        {PRIMARY_TABS.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              type="button"
              onClick={() => selectTab(tab.id)}
              className={`flex h-7 min-w-0 items-center justify-center gap-1.5 rounded-md px-2 text-[11px] transition-colors ${
                activeTab === tab.id ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:bg-zinc-900 hover:text-zinc-300"
              }`}
            >
              <Icon size={12} className="shrink-0" />
              <span className="truncate">{tab.label}</span>
            </button>
          );
        })}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto pb-14">
        {renderTab()}
      </div>

      {isMoreMenuOpen && (
        <button
          type="button"
          aria-label="Close Subagent Team options"
          className="fixed inset-0 rumi-layer-panel cursor-default bg-transparent"
          onClick={() => setIsMoreMenuOpen(false)}
        />
      )}
      <div className="absolute bottom-3 right-3 rumi-layer-local-popover flex flex-col items-end gap-2">
        {isMoreMenuOpen && (
          <div role="menu" className="w-44 overflow-hidden rounded-xl border border-zinc-700/70 bg-zinc-950 py-1 shadow-2xl">
            {OVERFLOW_TABS.map((tab) => {
              const Icon = tab.icon;
              const selected = activeTab === tab.id;
              return (
                <button
                  key={tab.id}
                  type="button"
                  role="menuitem"
                  onClick={() => selectTab(tab.id)}
                  className={`flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] transition-colors ${
                    selected ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
                  }`}
                >
                  <Icon size={13} className="shrink-0" />
                  <span className="truncate">{tab.label}</span>
                </button>
              );
            })}
          </div>
        )}
        <button
          type="button"
          aria-label="Subagent Team options"
          aria-haspopup="menu"
          aria-expanded={isMoreMenuOpen}
          onClick={() => setIsMoreMenuOpen((open) => !open)}
          className={`flex h-9 w-9 items-center justify-center rounded-full border shadow-xl transition-colors ${
            isMoreMenuOpen || isOverflowTabActive
              ? "border-zinc-600 bg-zinc-100 text-zinc-950"
              : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100"
          }`}
          title="Subagent Team options"
        >
          <Settings size={16} />
        </button>
      </div>
    </div>
  );
}
