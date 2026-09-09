import { useEffect, useMemo, useState } from "react";

import { api, type Conversation, type ModelSearchItem } from "../lib/api";
import { createModelSearchResources } from "../features/models";
import { ambientTriggerClient, type AmbientRoutingConfig, type AmbientRoutingMode, type AmbientStatus } from "./ambientTriggerClient";
import { conversationsToChatItems, normalizeRouting, routingLabel } from "./ambientRouting";

const modelSearchResources = createModelSearchResources(api);

export function useAmbientRouting({
  status,
  conversationId,
  setStatus,
  setBusy,
  setMessage,
  refresh,
}: {
  status: AmbientStatus | null;
  conversationId: string | null | undefined;
  setStatus: (status: AmbientStatus) => void;
  setBusy: (busy: boolean) => void;
  setMessage: (message: string | null) => void;
  refresh: () => Promise<void>;
}) {
  const [chatPickerOpen, setChatPickerOpen] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState(false);
  const [routingMode, setRoutingMode] = useState<AmbientRoutingMode>("selected_chat");
  const [routingConversationId, setRoutingConversationId] = useState<string | null>(conversationId || null);
  const [routingGroupEnabled, setRoutingGroupEnabled] = useState(true);
  const [routingGroupId, setRoutingGroupId] = useState("gesture");
  const [routingGroupTitle, setRoutingGroupTitle] = useState("Gesture");
  const [routingModel, setRoutingModel] = useState("");
  const [destinationConversationModel, setDestinationConversationModel] = useState<{ id: string; model: string; revision?: number }>({ id: "", model: "" });
  const [aiSendApprovalRequired, setAiSendApprovalRequired] = useState(false);
  const [modelQuery, setModelQuery] = useState("");
  const [modelResults, setModelResults] = useState<ModelSearchItem[]>([]);
  const [modelLoading, setModelLoading] = useState(false);

  useEffect(() => {
    const routing = normalizeRouting(status?.routing, conversationId || null);
    setRoutingMode(routing.mode);
    setRoutingConversationId(routing.conversation_id ?? null);
    setRoutingGroupEnabled(routing.group_enabled);
    setRoutingGroupId(routing.group_id || "gesture");
    setRoutingGroupTitle(routing.group_title || "Gesture");
    setRoutingModel(routing.model || "");
    setAiSendApprovalRequired(routing.ai_send_approval_required);
  }, [
    conversationId,
    status?.routing?.ai_send_approval_required,
    status?.routing?.conversation_id,
    status?.routing?.group_enabled,
    status?.routing?.group_id,
    status?.routing?.group_title,
    status?.routing?.mode,
    status?.routing?.model,
  ]);

  const routingNeedsNewChatSettings = routingMode === "startup_new_chat" || routingMode === "always_new_chat";
  const routingSelectedConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === routingConversationId) ?? null,
    [conversations, routingConversationId],
  );
  const destinationConversationId = routingConversationId || conversationId || null;
  useEffect(() => {
    let cancelled = false;
    const activeId = String(destinationConversationId ?? "").trim();
    if (!activeId) {
      setDestinationConversationModel({ id: "", model: "" });
      return () => {
        cancelled = true;
      };
    }

    const listed = conversations.find((conversation) => conversation.id === activeId);
    if (listed) {
      setDestinationConversationModel({ id: activeId, model: listed.model || "", revision: listed.conversation_revision });
      return () => {
        cancelled = true;
      };
    }

    setDestinationConversationModel((current) => (current.id === activeId ? current : { id: activeId, model: "" }));
    api.getConversation(activeId)
      .then((conversation) => {
        if (!cancelled) setDestinationConversationModel({ id: activeId, model: conversation.model || "", revision: conversation.conversation_revision });
      })
      .catch(() => {
        if (!cancelled) setDestinationConversationModel({ id: activeId, model: "" });
      });
    return () => {
      cancelled = true;
    };
  }, [conversations, destinationConversationId]);
  const destinationModel = destinationConversationModel.id === String(destinationConversationId ?? "").trim()
    ? destinationConversationModel.model
    : "";
  const effectiveRoutingModel = routingModel || routingSelectedConversation?.model || destinationModel || "";
  const routingChatItems = useMemo(() => conversationsToChatItems(conversations), [conversations]);
  const routingSummary = useMemo(
    () => routingLabel(routingMode, routingSelectedConversation, routingConversationId, status?.routing?.session_conversation_id),
    [routingConversationId, routingMode, routingSelectedConversation, status?.routing?.session_conversation_id],
  );

  async function loadConversations() {
    setConversationsLoading(true);
    try {
      const result = await api.listConversations({ limit: 80 });
      setConversations(result.conversations);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "チャット一覧を読み込めませんでした。");
    } finally {
      setConversationsLoading(false);
    }
  }

  async function openChatPicker() {
    setChatPickerOpen(true);
    await loadConversations();
  }

  async function saveRouting(patch: Partial<AmbientRoutingConfig>, success?: string) {
    const next = normalizeRouting({
      mode: routingMode,
      conversation_id: routingConversationId,
      group_enabled: routingGroupEnabled,
      group_id: routingGroupId,
      group_title: routingGroupTitle,
      model: effectiveRoutingModel,
      ai_send_approval_required: aiSendApprovalRequired,
      ...patch,
    }, conversationId || null);
    setRoutingMode(next.mode);
    setRoutingConversationId(next.conversation_id ?? null);
    setRoutingGroupEnabled(next.group_enabled);
    setRoutingGroupId(next.group_id || "gesture");
    setRoutingGroupTitle(next.group_title || "Gesture");
    setRoutingModel(next.model || "");
    setAiSendApprovalRequired(next.ai_send_approval_required);
    setBusy(true);
    try {
      const configured = await ambientTriggerClient.configure(next);
      setStatus(configured);
      if (success) setMessage(success);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "送信先を保存できませんでした。");
      await refresh().catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }


  async function saveRoutingModel(model: string) {
    const normalizedModel = model.trim();
    const targetConversationId = routingMode === "selected_chat"
      ? String(routingConversationId || conversationId || "").trim()
      : "";
    setRoutingModel(normalizedModel);
    setBusy(true);
    try {
      if (targetConversationId && normalizedModel) {
        const targetRevision = conversations.find((item) => item.id === targetConversationId)?.conversation_revision
          ?? (destinationConversationModel.id === targetConversationId ? destinationConversationModel.revision : undefined);
        const updated = await api.updateConversation(targetConversationId, { model: normalizedModel }, targetRevision);
        setDestinationConversationModel({ id: targetConversationId, model: updated.model || normalizedModel, revision: updated.conversation_revision });
        setConversations((current) => current.map((item) => (
          item.id === targetConversationId ? { ...item, model: updated.model || normalizedModel, conversation_revision: updated.conversation_revision } : item
        )));
      }
      const next = normalizeRouting({
        mode: routingMode,
        conversation_id: routingConversationId,
        group_enabled: routingGroupEnabled,
        group_id: routingGroupId,
        group_title: routingGroupTitle,
        model: normalizedModel,
        ai_send_approval_required: aiSendApprovalRequired,
      }, conversationId || null);
      const configured = await ambientTriggerClient.configure(next);
      setStatus(configured);
      setMessage(normalizedModel ? "送信モデルを保存しました。" : "モデル指定を外しました。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "送信モデルを保存できませんでした。");
      await refresh().catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }

  async function selectConversationForRouting(chatId: string) {
    setChatPickerOpen(false);
    const selected = conversations.find((conversation) => conversation.id === chatId);
    await saveRouting(
      { mode: "selected_chat", conversation_id: chatId, model: selected?.model || "" },
      "このチャットに送ります。",
    );
  }

  async function searchRoutingModels(query = modelQuery) {
    const trimmed = query.trim();
    setModelLoading(true);
    try {
      const result = await modelSearchResources.searchModels({ query: trimmed, max_results: 12 });
      setModelResults(result.models ?? []);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "モデルを検索できませんでした。");
    } finally {
      setModelLoading(false);
    }
  }

  return {
    chatPickerOpen,
    setChatPickerOpen,
    conversationsLoading,
    routingMode,
    routingConversationId,
    routingGroupEnabled,
    routingGroupId,
    setRoutingGroupId,
    routingGroupTitle,
    setRoutingGroupTitle,
    routingModel: effectiveRoutingModel,
    setRoutingModel,
    aiSendApprovalRequired,
    modelQuery,
    setModelQuery,
    modelResults,
    modelLoading,
    routingNeedsNewChatSettings,
    routingChatItems,
    routingSummary,
    loadConversations,
    openChatPicker,
    saveRouting,
    saveRoutingModel,
    selectConversationForRouting,
    searchRoutingModels,
  };
}
