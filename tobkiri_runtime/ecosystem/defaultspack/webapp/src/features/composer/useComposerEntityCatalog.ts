import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type McpServerRecord } from "../../lib/api";
import { confirmedChatReferenceCatalog } from "../../lib/chatReferenceCatalog";
import frontendContractMap from "../../../../defaultspack/frontend_contract_map.v4.json";
import { useVerifiedFrontendHost, type VerifiedFrontendHost } from "../../host/VerifiedFrontendHostContext";
import {
  buildComposerMcpCandidates, composerMcpMentionWidget,
  type ComposerEntityCandidate, type ComposerHistoryCandidate,
} from "../../lib/composerEntityCandidates";
import type { ComposerEntityReference } from "../../lib/composerReferences";
import {
  buildHistoryChatReference, buildHistoryGroupReference, decodeHistoryReferenceDrop,
  confirmResolvedHistoryReference, historyReferenceMention,
  type HistoryReferenceDragPayload,
} from "../../lib/historyReferences";
import type { ComposerExtensionItem, DroppedWidget } from "../../renderers/types";

type ConfirmedSelection = { widget: DroppedWidget; reference: ComposerEntityReference; syntax: string };
const UNAVAILABLE = "参照を確認できませんでした。もう一度お試しください。";
const MCP_UNAVAILABLE = "MCPの接続を確認できませんでした。";

/** A shipped route declaration alone never proves current Profile admission. */
export function hasAdmittedComposerMcpRead(host: VerifiedFrontendHost | null, profileId: string): boolean {
  if (!host || host.catalog.profile_id !== profileId
    || host.catalog.plan_hash !== host.activePlanHash) return false;
  return frontendContractMap.routes.some((route) => route.method === "GET"
    && route.path === "/api/tools/mcp" && route.targets.some((target) => (
      host.catalog.contributions.some((item) => item.kind === "data_source"
        && item.read_only === true && item.contribution_id === target.contribution_id
        && item.data_source_contract === target.contract_id && item.operation_id === target.operation_id
        && item.resolved_profile_id === profileId
        && item.resolved_profile_revision === host.catalog.profile_revision
        && item.resolved_activation_id === host.catalog.activation_id
        && item.resolved_plan_hash === host.activePlanHash
        && !host.catalog.quarantined_pack_ids.includes(item.owner_pack_id))
    )));
}

/** Keep optional MCP availability separate from authenticated history reads. */
export async function loadComposerEntityCatalog(
  profileId: string,
  readHistory: () => Promise<unknown>,
  readMcp?: () => Promise<unknown>,
) {
  const [history, mcp] = await Promise.allSettled([
    readHistory(), readMcp ? readMcp() : Promise.resolve({ servers: [] }),
  ]);
  const catalog = confirmedChatReferenceCatalog(
    history.status === "fulfilled" ? history.value : null, profileId,
  );
  const mcpServers = mcp.status === "fulfilled" && typeof mcp.value === "object"
    && mcp.value !== null && "servers" in mcp.value ? mcp.value.servers : undefined;
  const validMcp = Array.isArray(mcpServers);
  return {
    catalog,
    servers: validMcp ? mcpServers as McpServerRecord[] : [],
    status: history.status === "rejected" ? UNAVAILABLE : undefined,
    mcpStatus: validMcp ? undefined : MCP_UNAVAILABLE,
  };
}

/** Load authenticated entity suggestions and resolve every selection against fresh data. */
export function useComposerEntityCatalog({ profileId, tools, enabled }: {
  profileId: string; tools: ComposerExtensionItem[]; enabled: boolean;
}) {
  const verifiedHost = useVerifiedFrontendHost();
  const mcpReadAvailable = hasAdmittedComposerMcpRead(verifiedHost, profileId);
  const activationId = verifiedHost?.catalog.activation_id;
  const planHash = verifiedHost?.activePlanHash;
  const [historyCandidates, setHistoryCandidates] = useState<ComposerHistoryCandidate[]>([]);
  const [historyReferences, setHistoryReferences] = useState<ComposerEntityReference[]>([]);
  const [servers, setServers] = useState<McpServerRecord[]>([]);
  const [cursor, setCursor] = useState<string>();
  const [status, setStatus] = useState<string>();
  const [mcpStatus, setMcpStatus] = useState<string>();
  const generation = useRef(0);
  const activeProfile = useRef(profileId);
  const activeTools = useRef(tools);
  const activeMcpRead = useRef(mcpReadAvailable);
  const loadingMore = useRef(false);
  activeProfile.current = profileId;
  activeTools.current = tools;
  activeMcpRead.current = mcpReadAvailable;

  useEffect(() => {
    const current = ++generation.current;
    const capturedProfile = profileId;
    loadingMore.current = false;
    setHistoryCandidates([]); setHistoryReferences([]); setServers([]); setCursor(undefined);
    setStatus(undefined); setMcpStatus(undefined);
    if (!enabled || !capturedProfile) return;
    setStatus("読み込み中…");
    const currentRequest = () => generation.current === current && activeProfile.current === capturedProfile;
    void loadComposerEntityCatalog(capturedProfile, () => api.listChatReferences({ limit: 100 }),
      mcpReadAvailable ? () => api.listMcpServers() : undefined).then((result) => {
      if (!currentRequest()) return;
      setHistoryCandidates(result.catalog.candidates); setHistoryReferences(result.catalog.references);
      setCursor(result.catalog.nextCursor); setServers(result.servers);
      setStatus(result.status); setMcpStatus(result.mcpStatus);
    });
    return () => { ++generation.current; };
  }, [profileId, enabled, mcpReadAvailable, activationId, planHash]);

  const loadMore = useCallback(() => {
    if (!cursor || loadingMore.current || !enabled) return;
    const current = generation.current;
    const capturedProfile = profileId;
    loadingMore.current = true;
    setStatus("読み込み中…");
    void api.listChatReferences({ cursor, limit: 100 }).then((raw) => {
      if (generation.current !== current || activeProfile.current !== capturedProfile) return;
      const page = confirmedChatReferenceCatalog(raw, capturedProfile);
      setHistoryCandidates((previous) => {
        const map = new Map(previous.map((item) => [`${item.kind}:${item.id}`, item]));
        page.candidates.forEach((item) => map.set(`${item.kind}:${item.id}`, item));
        return [...map.values()];
      });
      setHistoryReferences((previous) => {
        const map = new Map(previous.map((item) => [`${item.kind}:${item.id}`, item]));
        page.references.forEach((item) => map.set(`${item.kind}:${item.id}`, item));
        return [...map.values()];
      });
      setCursor(page.nextCursor === cursor ? undefined : page.nextCursor);
      setStatus(undefined);
    }).catch(() => {
      if (generation.current === current && activeProfile.current === capturedProfile) setStatus(UNAVAILABLE);
    }).finally(() => {
      if (generation.current === current) loadingMore.current = false;
    });
  }, [cursor, enabled, profileId]);

  const resolveHistory = useCallback(async (candidate: HistoryReferenceDragPayload): Promise<ConfirmedSelection> => {
    const capturedProfile = profileId;
    const current = generation.current;
    if (candidate.profile_id !== capturedProfile || activeProfile.current !== capturedProfile) throw new Error(UNAVAILABLE);
    const response = await api.resolveChatReferences([{ kind: candidate.kind, id: candidate.id }]);
    if (activeProfile.current !== capturedProfile || generation.current !== current) throw new Error(UNAVAILABLE);
    const confirmed = confirmResolvedHistoryReference(candidate, response);
    if (!confirmed) throw new Error(UNAVAILABLE);
    const selection = historyReferenceMention(confirmed);
    selection.widget.metadata = { ...selection.widget.metadata, source: "composer_at_mention" };
    return selection;
  }, [profileId]);

  const confirm = useCallback(async (candidate: ComposerEntityCandidate): Promise<ConfirmedSelection | null> => {
    try {
      if (!candidate.available || activeProfile.current !== profileId) throw new Error(UNAVAILABLE);
      if (candidate.kind === "mcp") {
        if (!activeMcpRead.current) throw new Error(MCP_UNAVAILABLE);
        const capturedProfile = profileId;
        const current = generation.current;
        const response = await api.listMcpServers();
        if (activeProfile.current !== capturedProfile || generation.current !== current) throw new Error(UNAVAILABLE);
        if (!Array.isArray(response.servers)) throw new Error(UNAVAILABLE);
        const fresh = buildComposerMcpCandidates(response.servers, activeTools.current).find((item) => item.id === candidate.id);
        const widget = fresh && composerMcpMentionWidget(fresh);
        if (!fresh || !widget) throw new Error(UNAVAILABLE);
        return { widget, reference: { kind: "mcp", id: fresh.id, syntax: fresh.syntax, label: fresh.label }, syntax: fresh.syntax };
      }
      if (candidate.profileId !== profileId) throw new Error(UNAVAILABLE);
      const payload = candidate.kind === "chat"
        ? buildHistoryChatReference({ id: candidate.id, title: candidate.label }, profileId)
        : buildHistoryGroupReference({ id: candidate.id, title: candidate.label, chats: [], subGroups: [] }, profileId);
      if (!payload) throw new Error(UNAVAILABLE);
      return await resolveHistory(payload);
    } catch { throw new Error(UNAVAILABLE); }
  }, [profileId, resolveHistory]);

  const confirmHistoryDrop = useCallback(async (rawPayload: string): Promise<ConfirmedSelection | null> => {
    try {
      const candidate = decodeHistoryReferenceDrop(rawPayload, profileId);
      if (!candidate) throw new Error(UNAVAILABLE);
      return await resolveHistory(candidate);
    } catch { throw new Error(UNAVAILABLE); }
  }, [profileId, resolveHistory]);

  const candidates = useMemo<ComposerEntityCandidate[]>(() => [
    ...historyCandidates, ...buildComposerMcpCandidates(servers, tools),
  ], [historyCandidates, servers, tools]);
  return { candidates, historyReferences, status, mcpStatus, hasMore: Boolean(cursor), loadMore, confirm, confirmHistoryDrop };
}
