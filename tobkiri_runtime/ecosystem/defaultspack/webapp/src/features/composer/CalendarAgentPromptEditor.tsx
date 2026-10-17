import type { ComponentType, FormEvent } from "react";
import type { ModelProfile } from "../../lib/api";
import type { ActionApprovalMode } from "../tools/ActionApprovalControl";
import { composerMentionToolIdsFromWidgets } from "../../lib/composerWidgets";
import { resolveComposerToolMentions } from "../../lib/composerToolMentions";
import type { ComposerExtensionItem, ComposerRendererProps, DroppedWidget } from "../../renderers/types";
import type { ToolSelectionMode } from "../tools/types";
import { useComposerEntityCatalog } from "./useComposerEntityCatalog";

/** Reuse Composer confirmation, IME and history-drop handling inside a task form. */
export function CalendarAgentPromptEditor({
  Composer, input, widgets, profileId, modelId, models, tools, busy,
  mode, showApprovalControl, showToolControl, actionApprovalMode, actionApprovalModes, onActionApprovalModeChange,
  onInputChange, onWidgetsChange, onModelChange, onModeChange, onPendingChange, onSubmit,
}: {
  Composer: ComponentType<ComposerRendererProps>;
  input: string;
  widgets: DroppedWidget[];
  profileId: string;
  modelId: string;
  models: ModelProfile[];
  tools: ComposerExtensionItem[];
  busy: boolean;
  mode: ToolSelectionMode;
  showApprovalControl: boolean;
  showToolControl: boolean;
  actionApprovalMode?: ActionApprovalMode;
  actionApprovalModes?: readonly ActionApprovalMode[];
  onActionApprovalModeChange?: (mode: ActionApprovalMode) => void;
  onInputChange: (value: string) => void;
  onWidgetsChange: (widgets: DroppedWidget[]) => void;
  onModelChange: (id: string) => void;
  onModeChange: (mode: ToolSelectionMode) => void;
  onPendingChange: (pending: boolean) => void;
  onSubmit: (event: FormEvent) => void;
}) {
  const catalog = useComposerEntityCatalog({ profileId, tools, enabled: input.includes("@") });
  const selectedProfile = models.find((model) => model.profile_id === modelId
    || model.qualified_model_id === modelId) ?? null;
  const draft = resolveComposerToolMentions(input, widgets, tools);
  return <div className="mt-2" aria-label="Agentタスクの入力">
    <Composer
      surfaceMode="scheduled"
      input={input}
      placeholder="実行する内容。@で機能・MCP・会話を選択できます。"
      isGenerating={false}
      submissionDisabled={busy || !selectedProfile}
      selectedProfile={selectedProfile}
      favoriteProfiles={[]}
      modelProfiles={models}
      thinkingLevel={null}
      contextUsage={{ usedTokens: 0, maxContext: 0, ratio: 0, label: "0%" }}
      inlineExtensions={tools}
      belowExtensions={[]}
      droppedWidgets={widgets}
      selectedToolIds={composerMentionToolIdsFromWidgets(draft.widgets)}
      composerInput={{ id: "calendar-agent-prompt", feature_flags: { file_attachments: false, attachments: false, voice_input: false, slash_commands: false } }}
      toolSelectionMode={mode}
      showActionApprovalControl={showApprovalControl}
      actionApprovalMode={actionApprovalMode}
      actionApprovalModes={actionApprovalModes}
      onActionApprovalModeChange={onActionApprovalModeChange}
      showToolSelectionControl={showToolControl}
      onToolSelectionModeChange={onModeChange}
      onModelProfileSelect={onModelChange}
      onThinkingLevelChange={() => undefined}
      onInputChange={onInputChange}
      onDroppedWidgetsChange={onWidgetsChange}
      onDropWidget={(widget) => onWidgetsChange([...widgets.filter((existing) => existing.id !== widget.id), widget])}
      historyReferenceTargetProfileId={profileId}
      historyReferences={catalog.historyReferences}
      entityCandidates={catalog.candidates}
      entityCandidateStatus={catalog.status}
      entityCandidatesHaveMore={catalog.hasMore}
      entityCandidatesLoadMore={catalog.loadMore}
      onEntityCandidateConfirm={catalog.confirm}
      onHistoryReferenceDrop={catalog.confirmHistoryDrop}
      onReferenceConfirmationPendingChange={onPendingChange}
      onSubmit={onSubmit}
    />
  </div>;
}
