import { useMemo, useState } from "react";

import { api } from "../../lib/api";
import type { ConversationToolPreferences, PendingToolReview, ToolReviewDraft, ToolSelectionChip, ToolSelectionMode, ToolSelectionRequest, ToolTarget } from "./types";

type ControllerInput = {
  settingsValues: Record<string, Record<string, unknown>>;
  selectedToolIds: string[];
  setSelectedToolIds: (toolIds: string[]) => void;
  conversationPreferences?: ConversationToolPreferences;
};

type BuildRequestInput = {
  toolIds: string[];
  mentionedToolIds?: string[];
};

type PreviewReviewInput = {
  conversationId?: string | null;
  userText: string;
  attachmentMetadata?: unknown[];
  toolSelection: ToolSelectionRequest;
  draft: ToolReviewDraft;
  model?: string | null;
};

const MODES = new Set<ToolSelectionMode>(["auto", "review", "manual", "none"]);

export function useToolSelectionController({
  settingsValues,
  selectedToolIds,
  setSelectedToolIds,
  conversationPreferences = {},
}: ControllerInput) {
  const [turnModeOverride, setTurnModeOverride] = useState<ToolSelectionMode | null>(null);
  const [turnExclude, setTurnExclude] = useState<ToolTarget[]>([]);
  const [pendingReview, setPendingReview] = useState<PendingToolReview | null>(null);
  const [latestDecision, setLatestDecision] = useState<PendingToolReview["decision"] | null>(null);

  const defaultMode = useMemo<ToolSelectionMode>(() => {
    const raw = String(settingsValues.tools?.default_mode ?? "auto").trim().toLowerCase() as ToolSelectionMode;
    return MODES.has(raw) ? raw : "auto";
  }, [settingsValues.tools?.default_mode]);

  const conversationMode = conversationPreferences.mode && MODES.has(conversationPreferences.mode)
    ? conversationPreferences.mode
    : undefined;
  const effectiveMode = turnModeOverride ?? conversationMode ?? defaultMode;
  const turnInclude = useMemo<ToolTarget[]>(
    () => selectedToolIds.map((id) => ({ kind: "tool", id })),
    [selectedToolIds],
  );
  const conversationInclude = useMemo(
    () => normalizeTargets(conversationPreferences.include),
    [conversationPreferences.include],
  );
  const conversationExclude = useMemo(
    () => normalizeTargets(conversationPreferences.exclude),
    [conversationPreferences.exclude],
  );
  const overrideChips = useMemo<ToolSelectionChip[]>(() => {
    const turnExcludeKeys = new Set(turnExclude.map(targetKey));
    const turnIncludeKeys = new Set(turnInclude.map(targetKey));
    return [
      ...conversationInclude
        .filter((target) => !turnExcludeKeys.has(targetKey(target)))
        .map((target) => ({ ...target, scope: "conversation" as const, intent: "include" as const, removable: true })),
      ...conversationExclude
        .filter((target) => !turnIncludeKeys.has(targetKey(target)))
        .map((target) => ({ ...target, scope: "conversation" as const, intent: "exclude" as const, removable: false })),
      ...turnInclude.map((target) => ({ ...target, scope: "turn" as const, intent: "include" as const, removable: true })),
      ...turnExclude.map((target) => ({ ...target, scope: "turn" as const, intent: "exclude" as const, removable: true })),
    ];
  }, [conversationExclude, conversationInclude, turnExclude, turnInclude]);

  const setTurnMode = (mode: ToolSelectionMode | null) => {
    setTurnModeOverride(mode);
  };

  const toggleTurnTarget = (target: ToolTarget) => {
    if (target.kind !== "tool") return;
    setSelectedToolIds(
      selectedToolIds.includes(target.id)
        ? selectedToolIds.filter((id) => id !== target.id)
        : [...selectedToolIds, target.id],
    );
    if (effectiveMode === "auto") setTurnModeOverride("manual");
  };

  const removeTarget = (target: ToolTarget) => {
    const scoped = target as ToolSelectionChip;
    if (scoped.scope === "conversation" && scoped.intent === "include") {
      setTurnExclude((current) => mergeTargets(current, [target]));
      return;
    }
    if (scoped.scope === "conversation") return;
    if (target.kind === "tool" && scoped.intent !== "exclude") {
      setSelectedToolIds(selectedToolIds.filter((id) => id !== target.id));
      return;
    }
    setTurnExclude((current) => current.filter((item) => !(item.kind === target.kind && item.id === target.id)));
  };

  const buildRequest = ({ toolIds, mentionedToolIds = [] }: BuildRequestInput): ToolSelectionRequest => {
    const uniqueToolIds = [...new Set([...toolIds, ...mentionedToolIds].filter(Boolean))];
    const turnTargets = uniqueToolIds.map((id) => ({ kind: "tool" as const, id }));
    const include = mergeTargets(conversationInclude, turnTargets);
    const exclude = mergeTargets(conversationExclude, turnExclude);
    const hasTurnOverrides = turnTargets.length > 0 || turnExclude.length > 0 || Boolean(turnModeOverride);
    const scope = hasTurnOverrides ? "turn" : (conversationInclude.length || conversationExclude.length || conversationMode ? "conversation" : "turn");
    if (effectiveMode === "none") {
      return {
        mode: "none",
        include: [],
        exclude,
        scope,
        must_use: false,
      };
    }
    if (effectiveMode === "manual" || (effectiveMode === "auto" && uniqueToolIds.length > 0)) {
      return {
        mode: "manual",
        include,
        exclude,
        scope,
        // A turn-level Tool chip is an explicit execution request.  Mark it
        // required so the backend cannot silently downgrade the user's
        // "今回使う" selection to provider tool_choice=auto.
        must_use: turnTargets.length > 0,
      };
    }
    return {
      mode: effectiveMode,
      include,
      exclude,
      scope,
      must_use: false,
    };
  };

  const previewReview = async ({
    conversationId,
    userText,
    attachmentMetadata = [],
    toolSelection,
    draft,
    model,
  }: PreviewReviewInput): Promise<PendingToolReview> => {
    const response = await api.previewToolSelection({
      conversation_id: conversationId ?? null,
      user_text: userText,
      attachment_metadata: attachmentMetadata,
      tool_selection: toolSelection,
      model: model ?? null,
    });
    const pending: PendingToolReview = {
      previewId: response.preview_id,
      expiresAt: response.expires_at,
      userText,
      request: toolSelection,
      decision: response.decision,
      draft,
      createdAt: Date.now(),
    };
    setPendingReview(pending);
    setLatestDecision(response.decision);
    return pending;
  };

  const approveReview = (): ToolSelectionRequest | null => {
    if (!pendingReview) return null;
    const reviewedToolIds = selectedToolIds.length ? selectedToolIds : pendingReview.decision.selected_tools;
    const include = reviewedToolIds.map((id) => ({ kind: "tool" as const, id }));
    const request: ToolSelectionRequest = {
      mode: include.length ? "manual" : "none",
      include,
      exclude: pendingReview.request.exclude ?? [],
      scope: pendingReview.request.scope ?? "turn",
      must_use: false,
      preview_id: pendingReview.previewId,
    };
    setPendingReview(null);
    return request;
  };

  const continueWithoutTools = (): ToolSelectionRequest | null => {
    if (!pendingReview) return null;
    const request: ToolSelectionRequest = {
      mode: "none",
      include: [],
      exclude: [],
      scope: pendingReview.request.scope ?? "turn",
      must_use: false,
      preview_id: pendingReview.previewId,
    };
    setPendingReview(null);
    return request;
  };

  const cancelReview = () => {
    setPendingReview(null);
  };

  const clearTurnStateAfterSend = ({ keepSelectedTools }: { keepSelectedTools: boolean }) => {
    setTurnModeOverride(null);
    setTurnExclude([]);
    if (!keepSelectedTools) setSelectedToolIds([]);
  };

  return {
    state: {
      effectiveMode,
      turnModeOverride,
      turnInclude,
      turnExclude,
      conversationPreferences,
      overrideChips,
      pendingReview,
      latestDecision,
    },
    setTurnMode,
    toggleTurnTarget,
    removeTarget,
    buildRequest,
    previewReview,
    approveReview,
    continueWithoutTools,
    cancelReview,
    clearTurnStateAfterSend,
  };
}

function normalizeTargets(value: ToolTarget[] | undefined): ToolTarget[] {
  if (!Array.isArray(value)) return [];
  const result: ToolTarget[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    if (!item || (item.kind !== "tool" && item.kind !== "service") || !item.id?.trim()) continue;
    const target = { kind: item.kind, id: item.id.trim() };
    const key = targetKey(target);
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(target);
  }
  return result;
}

function mergeTargets(...groups: ToolTarget[][]): ToolTarget[] {
  const result: ToolTarget[] = [];
  const seen = new Set<string>();
  for (const group of groups) {
    for (const target of group) {
      const key = targetKey(target);
      if (seen.has(key)) continue;
      seen.add(key);
      result.push(target);
    }
  }
  return result;
}

function targetKey(target: ToolTarget): string {
  return `${target.kind}:${target.id}`;
}
