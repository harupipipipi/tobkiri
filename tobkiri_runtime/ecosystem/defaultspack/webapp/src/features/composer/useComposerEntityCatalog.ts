import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type McpServerRecord } from "../../lib/api";
import { confirmedChatReferenceCatalog } from "../../lib/chatReferenceCatalog";
import { useVerifiedFrontendHost } from "../../host/VerifiedFrontendHostContext";
import {
  buildComposerMcpCandidates,
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

// The formal frontend catalog currently has no server-connection-list binding.
// A tool-schema catalog is a different operation and cannot substitute for it.
const MCP_NOT_PROVIDED = "MCPの接続先は現在参照できません。";

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
    mcpStatus: !readMcp ? MCP_NOT_PROVIDED : validMcp ? undefined : MCP_UNAVAILABLE,
  };
}

/** Load authenticated entity suggestions and resolve every selection against fresh data. */
export function useComposerEntityCatalog({ profileId, tools, enabled }: {
  profileId: string; tools: ComposerExtensionItem[]; enabled: boolean;
}) {
  const verifiedHost = useVerifiedFrontendHost();
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
  const loadingMore = useRef(false);
  activeProfile.current = profileId;

  useEffect(() => {
    const current = ++generation.current;
    const capturedProfile = profileId;
    loadingMore.current = false;
    setHistoryCandidates([]); setHistoryReferences([]); setServers([]); setCursor(undefined);
    setStatus(undefined); setMcpStatus(undefined);
    if (!enabled || !capturedProfile) return;
    setStatus("読み込み中…");
    const currentRequest = () => generation.current === current && activeProfile.current === capturedProfile;
    void loadComposerEntityCatalog(capturedProfile, () => api.listChatReferences({ limit: 100 })).then((result) => {
      if (!currentRequest()) return;
      setHistoryCandidates(result.catalog.candidates); setHistoryReferences(result.catalog.references);
      setCursor(result.catalog.nextCursor); setServers(result.servers);
      setStatus(result.status); setMcpStatus(result.mcpStatus);
    });
    return () => { ++generation.current; };
  }, [profileId, enabled, activationId, planHash]);

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
      if (candidate.kind === "mcp") throw new Error(MCP_NOT_PROVIDED);
      if (candidate.profileId !== profileId) throw new Error(UNAVAILABLE);
      const payload = candidate.kind === "chat"
        ? buildHistoryChatReference({ id: candidate.id, title: candidate.label }, profileId)
        : buildHistoryGroupReference({ id: candidate.id, title: candidate.label, chats: [], subGroups: [] }, profileId);
      if (!payload) throw new Error(UNAVAILABLE);
      return await resolveHistory(payload);
    } catch { throw new Error(candidate.kind === "mcp" ? MCP_NOT_PROVIDED : UNAVAILABLE); }
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
