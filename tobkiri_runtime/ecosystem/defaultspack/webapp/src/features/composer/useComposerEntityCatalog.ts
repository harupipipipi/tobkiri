import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type McpServerRecord } from "../../lib/api";
import { confirmedChatReferenceCatalog } from "../../lib/chatReferenceCatalog";
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

/** Load authenticated entity suggestions and resolve every selection against fresh data. */
export function useComposerEntityCatalog({ profileId, tools, enabled }: {
  profileId: string; tools: ComposerExtensionItem[]; enabled: boolean;
}) {
  const [historyCandidates, setHistoryCandidates] = useState<ComposerHistoryCandidate[]>([]);
  const [historyReferences, setHistoryReferences] = useState<ComposerEntityReference[]>([]);
  const [servers, setServers] = useState<McpServerRecord[]>([]);
  const [cursor, setCursor] = useState<string>();
  const [status, setStatus] = useState<string>();
  const generation = useRef(0);
  const activeProfile = useRef(profileId);
  const activeTools = useRef(tools);
  const loadingMore = useRef(false);
  activeProfile.current = profileId;
  activeTools.current = tools;

  useEffect(() => {
    const current = ++generation.current;
    const capturedProfile = profileId;
    loadingMore.current = false;
    setHistoryCandidates([]); setHistoryReferences([]); setServers([]); setCursor(undefined);
    setStatus(undefined);
    if (!enabled || !capturedProfile) return;
    setStatus("読み込み中…");
    const currentRequest = () => generation.current === current && activeProfile.current === capturedProfile;
    void Promise.allSettled([api.listChatReferences({ limit: 100 }), api.listMcpServers()]).then(([history, mcp]) => {
      if (!currentRequest()) return;
      if (history.status === "fulfilled") {
        const catalog = confirmedChatReferenceCatalog(history.value, capturedProfile);
        setHistoryCandidates(catalog.candidates); setHistoryReferences(catalog.references); setCursor(catalog.nextCursor);
      }
      if (mcp.status === "fulfilled" && Array.isArray(mcp.value.servers)) setServers(mcp.value.servers);
      setStatus(history.status === "rejected" || mcp.status === "rejected"
        || (mcp.status === "fulfilled" && !Array.isArray(mcp.value.servers)) ? UNAVAILABLE : undefined);
    });
    return () => { ++generation.current; };
  }, [profileId, enabled]);

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
  return { candidates, historyReferences, status, hasMore: Boolean(cursor), loadMore, confirm, confirmHistoryDrop };
}
