import type { ComposerExtensionItem, DroppedWidget } from "../renderers/types";
import type { ToolSelectionMode } from "../features/tools/types";
import { validSavedToolSelection, type McpServerRecord, type SavedToolSelection, type SavedTurnRequest } from "./api";
import { confirmedChatReferenceCatalog } from "./chatReferenceCatalog";
import { isSavedTurnHistoryReferenceWidget, prepareComposerReferenceSubmission, requiresComposerReferenceRefresh } from "./composerReferenceSubmission";
import { composerMentionSelectionRequest, isSavedTurnToolMentionWidget, resolveComposerToolMentions } from "./composerToolMentions";

/** Signal an edited draft without displaying stale failures in another view. */
export class ComposerFollowupDraftChangedError extends Error {
  constructor() {
    super("追加指示の下書きが変更されました。");
    this.name = "ComposerFollowupDraftChangedError";
  }
}

/** Prepare the same lean references and tool selection as a normal saved turn. */
export async function prepareComposerFollowupInput({
  source, submitted, widgets, profileId, tools, mode, listMcpServers,
  resolveChatReferences, isCurrent,
}: {
  source: string;
  submitted: string;
  widgets: DroppedWidget[];
  profileId: string;
  tools: ComposerExtensionItem[];
  mode: ToolSelectionMode;
  listMcpServers: () => Promise<{ servers: McpServerRecord[] }>;
  resolveChatReferences: (references: Array<{ kind: "chat" | "group"; id: string }>) => Promise<unknown>;
  isCurrent: () => boolean;
}): Promise<{
  chat_references?: SavedTurnRequest["chat_references"];
  tool_selection: SavedToolSelection;
}> {
  const guard = () => {
    if (!isCurrent()) throw new ComposerFollowupDraftChangedError();
  };
  guard();
  const trimmed = source.trim();
  if (submitted !== trimmed && !(trimmed.startsWith("//") && submitted === trimmed.slice(1))) {
    throw new Error("入力と送信内容が一致しません。下書きは保持されています。");
  }
  if (mode === "review") {
    throw new Error("機能の確認を終えてから送信してください。下書きは保持されています。");
  }
  const mcpServers = requiresComposerReferenceRefresh(widgets, source)
    ? (await listMcpServers()).servers : undefined;
  guard();
  const prepared = prepareComposerReferenceSubmission({
    source, submitted, widgets, profileId, tools, mcpServers,
  });
  if (prepared.widgets.some((widget) => !isSavedTurnToolMentionWidget(widget)
    && !isSavedTurnHistoryReferenceWidget(widget, profileId, submitted))) {
    throw new Error("この入力のスキルや添付コンテキストにはまだ対応していません。下書きは保持されています。");
  }
  if (prepared.chatReferences.length) {
    const snapshot = await resolveChatReferences(prepared.chatReferences.map(({ kind, id }) => ({ kind, id })));
    guard();
    const confirmed = confirmedChatReferenceCatalog(snapshot, profileId).references;
    if (confirmed.length !== prepared.chatReferences.length
      || prepared.chatReferences.some((reference) => !confirmed.some((entry) => (
        entry.kind === reference.kind && entry.id === reference.id && entry.profileId === profileId
      )))) {
      throw new Error("チャット参照を確認できませんでした。参照を選び直してください。");
    }
  }
  const draft = resolveComposerToolMentions(submitted, prepared.widgets, tools);
  const selection = composerMentionSelectionRequest(draft, mode);
  if (!validSavedToolSelection(selection)) {
    throw new Error("機能の選択を確認できませんでした。下書きは保持されています。");
  }
  guard();
  return {
    tool_selection: selection,
    ...(prepared.chatReferences.length ? { chat_references: prepared.chatReferences } : {}),
  };
}
