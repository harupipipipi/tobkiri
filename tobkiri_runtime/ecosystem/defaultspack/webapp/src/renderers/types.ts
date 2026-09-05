import type { FormEvent, MutableRefObject, ReactNode } from "react";

import type { ChatActivityEvent, ChatContentBlock, CodingContextEntry, CodingGitStatus, CodingWorkspaceRecord, ComposerWidgetAction, ConversationSteerItem, ModelCommandCandidate, ModelProfile, PromptUsageSummary, SettingsSection, SidebarAction, SidebarItem, TemplateComposerInput, ToolLogEntry, ToolTarget, UICatalog } from "../lib/api";
import type { DesktopSystemInfo } from "../lib/desktopSystemInfo";
import type { ComposerCommandItem } from "../lib/api";
import type { ChatGroup, ChatItem, HistoryBoardNewTaskOptions } from "../components/HistoryBoard";
import type { ToolPreviewItem, ToolPreviewMode } from "../components/ToolPreview";
import type { LocaleSetting } from "../lib/i18n";
import type { RuntimeCapabilitySnapshot, ToolFilterEntry } from "../lib/toolStatus";
import type { WorkspaceTab, WorkspaceTabKind } from "../components/WorkspaceTabs";
import type { ActionApprovalMode } from "../features/tools/ActionApprovalControl";
import type { PendingToolReview, ToolSelectionChip } from "../features/tools/types";
import type { ComposerMentionMetadata } from "../lib/composerWidgets";
import type { ComposerEntityReference } from "../lib/composerReferences";
import type { WidgetConversationContext } from "../lib/widgetContext";
import type { ModelSelectorSchema } from "../features/models";
import type { ProjectInfo } from "../features/projects/projectStorage";

export type { ComposerCommandItem } from "../lib/api";

export type ChatUiMessage = {
  id: string;
  conversationId?: string;
  createdAt?: number;
  role: "user" | "agent";
  content: ChatContentBlock[];
  rawText: string;
  widget?: Record<string, unknown> | null;
  metadata?: {
    executionTime?: string;
    toolUsed?: string;
    modelName?: string;
    thinkingLabel?: string;
    thinkingDuration?: string;
    thinkingTranscript?: string;
    interrupted?: boolean;
    interruptionReason?: string;
    attachedToolCount?: number;
    pendingApproval?: Record<string, unknown>;
    pendingAuthorityApproval?: Record<string, unknown>;
    authorityFollowup?: Record<string, unknown>;
    chatDisplay?: {
      hidden?: boolean;
      reason?: string;
      [key: string]: unknown;
    };
    mentions?: ComposerMentionMetadata[];
    promptUsage?: PromptUsageSummary;
  };
  events?: ChatActivityEvent[];
  toolLogs?: ToolLogEntry[];
};

export type ComposerExtensionItem = {
  id: string;
  label: string;
  category?: string;
  description?: string;
  tags?: string[];
  disabled?: boolean;
  ui?: SidebarItem["ui"];
};

export type ComposerSkillItem = {
  id: string;
  label: string;
  description?: string;
  triggers?: string[];
  appliesToTools?: string[];
  aliases?: string[];
  metadata?: Record<string, unknown>;
};

export type ContextUsageInfo = {
  usedTokens: number;
  maxContext: number;
  ratio: number;
  label: string;
};

export type ComposerModelStatusIndicatorAction = {
  label: string;
  onSelect: () => void;
  tone?: "neutral" | "info" | "warning" | "danger";
};

export type ComposerModelStatusIndicator = {
  id: string;
  name: string;
  description: string;
  svgMarkup: string;
  tone?: "neutral" | "info" | "warning" | "danger";
  action?: ComposerModelStatusIndicatorAction | null;
};

export type ComposerSteerStatus = {
  kind: "success";
  message: string;
} | {
  kind: "error";
  message: string;
};

export type SettingChangeHandler = (sectionId: string, fieldId: string, value: unknown) => void;

export type SettingsLoadState = {
  status: "idle" | "loading" | "ready" | "error";
  message?: string | null;
};

export type SettingsSaveState = {
  status: "idle" | "saving" | "saved" | "error";
  dirtyKeys?: string[];
  lastSavedAt?: number | null;
  message?: string | null;
};

export type TitleBarRendererProps = {
  appName?: string;
  appIcon?: string;
};

export type HistoryBoardRendererProps = {
  activeChatId: string | null;
  chatItems: ChatItem[];
  account?: NonNullable<UICatalog["app"]>["account"];
  onChatSelect: (conversationId: string) => void;
  onNewTask: (options?: HistoryBoardNewTaskOptions) => void;
  onCalendarOpen?: () => void;
  isCalendarActive?: boolean;
  onKanbanOpen?: () => void;
  onGroupKanbanOpen?: (group: ChatGroup) => void;
  onGroupSelect?: (group: ChatGroup) => void;
  isKanbanActive?: boolean;
  onDesktopsOpen?: () => void;
  isDesktopsActive?: boolean;
  onSettingsClick: () => void;
  onChatMetadataChange?: (chatId: string, updates: { is_pinned?: boolean; is_starred?: boolean; tags?: string[] }) => void;
  onMinimize?: () => void;
  onRestore?: () => void;
  isCompact?: boolean;
  codingWorkspaces?: CodingWorkspaceRecord[];
  selectedCodingWorkspaceId?: string | null;
  onCodingWorkspaceCreate?: (rootPath: string) => Promise<CodingWorkspaceRecord | null | undefined>;
  onDirectorySelect?: () => Promise<string | null | undefined>;
  onGroupDataPathPrepare?: (rootPath: string) => Promise<{ rootPath: string; rumiDataPath: string } | null | undefined>;
  onCodingWorkspacesRefresh?: () => void | Promise<void>;
};

export type ChatHeaderRendererProps = {
  title: string;
  showPreview: boolean;
  canShowPreview: boolean;
  canOpenSettings: boolean;
  onTogglePreview: () => void;
  onOpenSettings: () => void;
};

export type ChatMessagesRendererProps = {
  error: string | null;
  isMessagesRegionVisible: boolean;
  isLoading: boolean;
  isNewConversation: boolean;
  isGenerating: boolean;
  pendingStatus?: string | null;
  pendingToolNames?: string[];
  pendingStartedAt?: number | null;
  pendingToolStartedAt?: Record<string, number>;
  messages: ChatUiMessage[];
  messagesEndRef: MutableRefObject<HTMLDivElement | null>;
  messagesScrollRef?: MutableRefObject<HTMLDivElement | null>;
  onMessagesScroll?: () => void;
  unknownBlockStrategy: string;
  showActivityInMessages: boolean;
  showWidgets: boolean;
  showPromptUsageInMessages?: boolean;
  onSuggestionClick: (text: string) => void;
  onOpenToolPreview?: (previewId: string) => void;
  onLoadPromptTrace?: (traceId: string, profileId?: string) => Promise<PromptUsageSummary>;
  onRetry?: () => void;
  onDismissError?: () => void;
};

export type ComposerRendererProps = {
  widgetContext?: WidgetConversationContext;
  input: string;
  placeholder: string;
  isNewConversation?: boolean;
  isGenerating: boolean;
  selectedProfile: ModelProfile | null;
  favoriteProfiles: ModelProfile[];
  modelProfiles?: ModelProfile[];
  modelSelectorSchema?: ModelSelectorSchema;
  thinkingLevel: string | null;
  contextUsage: ContextUsageInfo;
  inlineExtensions: ComposerExtensionItem[];
  belowExtensions: ComposerExtensionItem[];
  skillExtensions?: ComposerSkillItem[];
  commands?: ComposerCommandItem[];
  composerInput?: TemplateComposerInput | null;
  structuredInputValues?: Record<string, string>;
  modelCommandCandidates?: ModelCommandCandidate[];
  modelPickerRequestId?: number;
  modelStatusIndicators?: ComposerModelStatusIndicator[];
  voiceInputEnabled?: boolean;
  voiceInputUseAi?: boolean;
  manualRuntimeModeSelectionEnabled?: boolean;
  mode?: AppMode;
  codingContext?: CodingContext | null;
  codingWorkspaces?: CodingWorkspaceRecord[];
  selectedCodingWorkspaceId?: string | null;
  projects?: ProjectInfo[];
  selectedProjectId?: string | null;
  attachedFiles?: AttachedFile[];
  pendingMentionAttachmentPaths?: string[];
  droppedWidgets?: DroppedWidget[];
  entityReferences?: ComposerEntityReference[];
  selectedToolIds?: string[];
  actionApprovalMode?: ActionApprovalMode;
  toolSelectionTargets?: ToolSelectionChip[];
  toolSelectionReview?: PendingToolReview | null;
  keyboardButtonNavigation?: boolean;
  steerStatus?: ComposerSteerStatus | null;
  steerBusy?: boolean;
  steerQueuedCount?: number;
  steerPreviewItems?: ConversationSteerItem[];
  suppressPopovers?: boolean;
  onOpenModelManager?: () => void;
  onOpenToolSettings?: () => void;
  onActionApprovalModeChange?: (mode: ActionApprovalMode) => void;
  onToolSelectionTargetRemove?: (target: ToolTarget) => void;
  onToolSelectionReviewApprove?: () => void;
  onToolSelectionReviewEdit?: () => void;
  onToolSelectionReviewNoTools?: () => void;
  onToolSelectionReviewCancel?: () => void;
  onSwitchToVisionModel?: () => void;
  onExtensionSelect?: (item: ComposerExtensionItem) => void;
  onCommandSelect?: (commandId: string, rawInput?: string) => void;
  onModelCommandCandidateSelect?: (candidate: ModelCommandCandidate) => void;
  onModelCommandCandidatesClose?: () => void;
  onModelProfileSelect: (profileId: string) => void;
  onProviderApiKeySave?: (providerId: string, value: string) => Promise<void> | void;
  onThinkingLevelChange: (level: string | null) => void;
  onInputChange: (value: string) => void;
  onStructuredInputChange?: (values: Record<string, string>) => void;
  onSubmit: (event: FormEvent) => void;
  onStopGenerating?: () => void;
  onSteerSubmit?: (prompt: string) => void;
  onModeChange?: (mode: AppMode) => void;
  onFileAttach?: (files: AttachedFile[]) => void;
  onAtFileAttach?: (path: string) => void;
  onPendingMentionAttachmentRemove?: (path: string) => void;
  onFileRemove?: (fileId: string) => void;
  onDropWidget?: (widget: DroppedWidget) => void;
  onEntityReferencesChange?: (references: ComposerEntityReference[]) => void;
  onWidgetAction?: (widget: DroppedWidget) => void;
  onWidgetToggle?: (widgetId: string) => void;
  onCodingBranchSwitch?: (branch: string, create?: boolean) => void;
  onCodingDirectoryChange?: (directory: string) => void;
  onCodingWorkspaceSelect?: (workspaceId: string) => void;
  onCodingWorkspaceTrust?: (workspaceId: string) => void;
  onCodingWorkspaceCreate?: (rootPath?: string) => Promise<CodingWorkspaceRecord | null | undefined> | void;
  onCodingWorkspacesRefresh?: () => void;
  onCodingContextRefresh?: () => void;
  onProjectSelect?: (project: ProjectInfo | null) => void;
  onProjectDirectorySelect?: () => Promise<string | null | undefined>;
  onProjectStoragePrepare?: (rootPath: string) => Promise<{ rootPath: string; rumiDataPath: string } | null | undefined>;
};

export type ToolPreviewPanelRendererProps = {
  widgetContext?: WidgetConversationContext;
  previews: ToolPreviewItem[];
  showPreview: boolean;
  previewMode: ToolPreviewMode;
  activePreviewId: string | null;
  memo?: string;
  onClose: () => void;
  onModeChange: (mode: ToolPreviewMode) => void;
  onMemoChange?: (value: string) => void;
};

export type RightSidebarRendererProps = {
  widgetContext?: WidgetConversationContext;
  items: SidebarItem[];
  activeItemId?: string | null;
  settingsValues: Record<string, Record<string, unknown>>;
  settingsSections: SettingsSection[];
  selectedToolIds?: string[];
  companyPanel?: ReactNode;
  codingPanel?: ReactNode;
  keyboardButtonNavigation?: boolean;
  attachedFiles?: AttachedFile[];
  selectedProfile?: ModelProfile | null;
  toolFilterEntries?: ToolFilterEntry[];
  runtimeCapabilitySnapshot?: RuntimeCapabilitySnapshot | null;
  contextUsage?: ContextUsageInfo | null;
  promptUsage?: PromptUsageSummary | null;
  promptProfileId?: string;
  conversationId?: string | null;
  showChatPromptUsage?: boolean;
  yoloMode?: boolean;
  workspaceTabs?: WorkspaceTab[];
  activeWorkspaceTabId?: string | null;
  activeConversationId?: string | null;
  onSettingChange: SettingChangeHandler;
  onOpenSettings: () => void;
  onOpenSettingsSection?: (sectionId: string) => void;
  onToggleYolo?: () => void;
  onWorkspaceTabSelect?: (tabId: string) => void;
  onWorkspaceTabClose?: (tabId: string) => void;
  onWorkspaceTabCreate?: (kind: WorkspaceTabKind) => void;
  onLoadPromptActive?: (params: { profile_id?: string; conversation_id?: string; include_text?: boolean }) => Promise<PromptUsageSummary>;
  onTogglePromptEdge?: (payload: { profile_id?: string; conversation_id?: string; edge_id: string; enabled: boolean }) => Promise<PromptUsageSummary>;
  onToggleChatPromptUsage?: (visible: boolean) => void;
  onToolToggle?: (item: SidebarItem) => void;
  onToolBatchSet?: (toolIds: string[], enabled: boolean) => void;
  onPanelAction?: (item: SidebarItem, action: SidebarAction) => void;
};

export type SettingsModalRendererProps = {
  isOpen: boolean;
  activeSectionId?: string | null;
  catalog: UICatalog | null;
  health: { status: string; pack: string; ts: string } | null;
  previewsCount: number;
  settingsSections: SettingsSection[];
  settingsValues: Record<string, Record<string, unknown>>;
  desktopSystemInfo?: DesktopSystemInfo | null;
  modelProfiles?: ModelProfile[];
  activeModelProfileId?: string | null;
  backendConnectionState?: "online" | "degraded" | "offline";
  backendConnectionNote?: string | null;
  saveState?: SettingsSaveState;
  loadState?: SettingsLoadState;
  modelProfilesLoadState?: SettingsLoadState;
  locale?: LocaleSetting;
  onClose: () => void;
  onStartSettingsChat?: () => void;
  onOpenSection?: (sectionId: string) => void;
  onRetryLoad?: () => void;
  onRetrySave?: () => void;
  onSettingChange: SettingChangeHandler;
};

export type ToolGroup = {
  id: string;
  label: string;
  description: string;
  icon?: string;
  path?: string[];
  items: ComposerExtensionItem[];
};

export type AppMode = "chat" | "coding" | "agent";

export type CodingContext = {
  branch: string | null;
  rootFolder: string | null;
  workspaceId?: string | null;
  directory?: string;
  branches?: string[];
  files: string[];
  entries?: CodingContextEntry[];
  git?: CodingGitStatus | null;
};

export type AttachedFile = {
  id: string;
  name: string;
  size: number;
  content?: string;
  dataUrl?: string;
  type?: string;
  truncated?: boolean;
  source?: "local_file" | "workspace";
  sourcePath?: string;
};

export type DroppedWidget = {
  id: string;
  type: "tool" | "model" | "setting" | "button" | "panel" | "selector" | "model_card" | string;
  label: string;
  enabled?: boolean;
  widgetKind?: string;
  action?: ComposerWidgetAction;
  sourceItemId?: string;
  description?: string;
  icon?: string;
  metadata?: Record<string, unknown>;
};
