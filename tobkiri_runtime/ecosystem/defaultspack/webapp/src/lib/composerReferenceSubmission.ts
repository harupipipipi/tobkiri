import type { ComposerExtensionItem, DroppedWidget } from "../renderers/types";
import type { McpServerRecord } from "./api";
import { buildComposerMcpCandidates, composerMcpMentionWidget } from "./composerEntityCandidates";
import { confirmedComposerMentionRange, transformConfirmedComposerWidgetsForSubmit } from "./composerMentionAnchors";
import { composerMentionMetadataFromWidgets } from "./composerWidgets";

export type ComposerChatReference = { kind: "chat" | "group"; profile_id: string; id: string };
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const HISTORY_ERROR = "チャット参照を確認できませんでした。下書きは保持されています。参照を選び直してください。";
const MCP_ERROR = "MCP接続または利用可能なツールを確認できませんでした。下書きは保持されています。接続を確認して参照を選び直してください。";

function mentionRecord(widget: DroppedWidget): Record<string, unknown> | null {
  const value = widget.metadata?.mention;
  return widget.metadata?.source === "composer_at_mention" && value
    && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function hasCurrentConfirmation(widget: DroppedWidget, text: string): boolean {
  // History and MCP references are admitted only by explicit selection, never by
  // finding a matching raw token in a restored legacy widget.
  return widget.metadata?.composer_confirmation !== undefined
    && confirmedComposerMentionRange(widget, text) !== null;
}

function validHistoryMention(widget: DroppedWidget, profileId: string, text: string): ComposerChatReference | null {
  const raw = mentionRecord(widget);
  const mention = composerMentionMetadataFromWidgets([widget])[0];
  if (!raw || !mention || (mention.kind !== "chat" && mention.kind !== "group")
    || !profileId || mention.profileId !== profileId
    || typeof raw.id !== "string" || raw.id !== mention.id
    || !ID_PATTERN.test(mention.id)
    || mention.syntax !== `@${mention.kind}:${mention.id}`
    || !Array.isArray(mention.memberIds) || mention.memberIds.length > 256
    || mention.memberIds.some((id) => !ID_PATTERN.test(id))
    || (mention.kind === "chat" && (mention.memberIds.length !== 1 || mention.memberIds[0] !== mention.id))
    || !hasCurrentConfirmation(widget, text)) return null;
  return { kind: mention.kind, profile_id: profileId, id: mention.id };
}

/** Refresh only confirmed MCP references; plain text never requests a registry read. */
export function requiresComposerReferenceRefresh(widgets: DroppedWidget[], source: string): boolean {
  return widgets.some((widget) => mentionRecord(widget)?.kind === "mcp"
    && hasCurrentConfirmation(widget, source));
}

/** Narrow exception for owned, anchored history widgets in normal-chat saved turns. */
export function isSavedTurnHistoryReferenceWidget(widget: DroppedWidget, profileId: string, submitted: string): boolean {
  const reference = validHistoryMention(widget, profileId, submitted);
  const snapshot = widget.metadata?.history_reference as Record<string, unknown> | undefined;
  return reference !== null && widget.type === (reference.kind === "chat" ? "conversation" : "group")
    && widget.widgetKind === "history_context" && widget.action === undefined
    && widget.id === `${reference.kind}:${profileId}:${reference.id}`
    && widget.sourceItemId === reference.id && snapshot?.schema === "io.tobkiri.history-reference.v1"
    && snapshot.kind === reference.kind && snapshot.profile_id === profileId && snapshot.id === reference.id;
}

/** Prepare semantic references without granting tools or expanding history content. */
export function prepareComposerReferenceSubmission({ source, submitted, widgets, profileId, tools, mcpServers }: {
  source: string;
  submitted: string;
  widgets: DroppedWidget[];
  profileId: string;
  tools: ComposerExtensionItem[];
  mcpServers?: McpServerRecord[];
}): { widgets: DroppedWidget[]; chatReferences: ComposerChatReference[] } {
  const eligible = widgets.filter((widget) => {
    const kind = mentionRecord(widget)?.kind;
    return kind !== "chat" && kind !== "group" && kind !== "mcp"
      || hasCurrentConfirmation(widget, source);
  });
  const transformed = transformConfirmedComposerWidgetsForSubmit(source, submitted, eligible);
  const chatReferences: ComposerChatReference[] = [];
  const seen = new Set<string>();
  const candidates = mcpServers ? buildComposerMcpCandidates(mcpServers, tools) : [];
  const prepared = transformed.map((widget) => {
    const raw = mentionRecord(widget);
    if (raw?.kind === "chat" || raw?.kind === "group") {
      const reference = validHistoryMention(widget, profileId, submitted);
      if (!reference) throw new Error(HISTORY_ERROR);
      const key = JSON.stringify([reference.kind, reference.id]);
      if (!seen.has(key)) {
        seen.add(key);
        chatReferences.push(reference);
        if (chatReferences.length > 16) throw new Error(HISTORY_ERROR);
      }
    }
    if (raw?.kind === "mcp") {
      const mention = composerMentionMetadataFromWidgets([widget])[0];
      const candidate = candidates.find((entry) => entry.id === raw.id);
      if (!mention || typeof raw.id !== "string" || raw.id !== mention.id
        || mention.syntax !== `@mcp:${mention.id}` || !candidate?.available
        || !hasCurrentConfirmation(widget, submitted)) throw new Error(MCP_ERROR);
      const fresh = composerMcpMentionWidget(candidate);
      if (!fresh) throw new Error(MCP_ERROR);
      return { ...widget, ...fresh, metadata: { ...widget.metadata, ...fresh.metadata } };
    }
    return widget;
  });
  return { widgets: prepared, chatReferences };
}

/** Compare selectable tool content across harmless catalog rerenders. */
export function composerSelectableToolSnapshotKey(tools: readonly ComposerExtensionItem[]): string {
  const rows = tools.map((tool) => [tool.id, tool.disabled === true,
    tool.serviceId ?? (typeof tool.ui?.service_id === "string" ? tool.ui.service_id : null),
    tool.sourcePackId ?? null] as const);
  rows.sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right)));
  // Retain duplicate IDs rather than guessing which catalog entry owns a tool.
  return JSON.stringify(rows);
}

export type ComposerReferencePreflightDraftSnapshot = {
  input: string;
  widgets: readonly DroppedWidget[];
  attachedFiles: readonly unknown[];
  profileId: string;
  toolSnapshotKey: string;
};

/** Prevent an awaited reference response from submitting a subsequently edited draft. */
export function composerReferencePreflightDraftIsCurrent(
  captured: ComposerReferencePreflightDraftSnapshot,
  current: ComposerReferencePreflightDraftSnapshot,
): boolean {
  return captured.input === current.input && captured.widgets === current.widgets
    && captured.attachedFiles === current.attachedFiles
    && captured.profileId === current.profileId
    && captured.toolSnapshotKey === current.toolSnapshotKey;
}
