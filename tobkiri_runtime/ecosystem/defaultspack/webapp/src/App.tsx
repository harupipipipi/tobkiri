import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type DragEvent as ReactDragEvent, type FormEvent, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from "react";
import { Cloud, Copy, Download, Hand, Link, Loader2, X } from "lucide-react";

import {
  CompanyWorkspacePanel,
  resolveCompanyWorkspaceHint,
  resolveCompanyWorkspaceHintFromGroup,
} from "./components/company/CompanyWorkspacePanel";
import { AmbientTriggerPanel } from "./ambient/AmbientTriggerPanel";
import { DefaultsConsoleWindow } from "./ambient/DefaultsConsoleWindow";
import { AdaptiveRuntimePage } from "./adaptive";
import { ambientTriggerClient, type AmbientRoutingConfig } from "./ambient/ambientTriggerClient";
import { publishAmbientFinalAnswer } from "./ambient/finalAnswerBridge";
import { AuthorityApprovalNotice } from "./components/AuthorityApprovalNotice";
import { AuthorityApprovalWindow } from "./components/AuthorityApprovalWindow";
import { ApprovalDecisionSurface } from "./components/ApprovalDecisionSurface";
import { ErrorNotice } from "./components/ErrorNotice";
import { CodingCockpit } from "./components/coding/CodingCockpit";
import { HostPermissionsPage } from "./hostPermissions/HostPermissionsPage";
import { ConversationSpotlight } from "./components/ConversationSpotlight";
import { DesktopMonitorWorkspace } from "./components/desktops/DesktopMonitorWorkspace";
import { KanbanWorkspacePanel } from "./components/kanban/KanbanWorkspacePanel";
import {
  alertPlacementForComposerPosition,
  TransientAlert,
  type TransientAlertItem,
  type TransientAlertTone,
} from "./components/TransientAlert";
import { WarmActionIcon } from "./components/WarmActionIcon";
import {
  TobkiriLoadingScreen,
  type TobkiriLoadingStep,
} from "./components/TobkiriLoadingScreen";
import { SubagentTeamWorkspace } from "./subagentTeam";
import {
  DEFAULT_WORKSPACE_TAB_ID,
  WORKSPACE_TAB_CREATE_OPTIONS,
  WorkspaceLaunchpad,
  WorkspaceTabBar,
  createWorkspaceTab,
  workspaceTabDisplayTitle,
  type WorkspaceTab,
  type WorkspaceTabKind,
} from "./components/WorkspaceTabs";
import {
  initialActiveWorkspaceTabIdForPathname,
  initialWorkspaceTabsForPathname,
  workspaceKindForPathname,
  workspaceUrlForKind,
} from "./lib/workspaceRouting";
import { UiPrecisionComparator } from "./pages/UiPrecisionComparator";
import { ConversationShareLanding, ImportedConversationNotice } from "./pages/ConversationShareLanding";
import type { ChatGroup, ChatItem, HistoryBoardNewTaskOptions } from "./components/HistoryBoard";
import type { ToolPreviewItem, ToolPreviewMode } from "./components/ToolPreview";
import { buildToolPreviewDisplayItems, hasCanvasItems } from "./components/ToolPreview";
import { ChatStreamInterruptedError, api, composerCommandFeedbackTone, composerCommandResultMessage, defaultspackApiFetch, defaultspackCanonicalRouteKey, defaultspackContractRoute, defaultspackUrlWithLocalAuth, mergeComposerCommands, type ChatActivityEvent, type ChatContentBlock, type ChatMessage, type ChatStreamEvent, type ChatToolStreamEvent, type CodingWorkspaceRecord, type ComposerCommandExecuteResult, type ComposerCommandItem, type ComposerCommandMode, type ComposerWidgetAction, type Conversation, type ConversationSearchResult, type ConversationSteerItem, type KanbanBoardScope, type MimoCodingCompanyStatus, type ModelCommandCandidate, type ModelProfile, type OperationsCompanyStatus, type PromptUsageSummary, type ResolvedCommandCatalog, type SettingsSection, type SidebarAction, type SidebarItem, type ToolSelectionRequest, type ToolTarget, type UICatalog } from "./lib/api";
import { applyCommandStateSnapshots, createCommandInvocationId } from "./lib/commandState";
import type { ActionApprovalMode } from "./features/tools/ActionApprovalControl";
import {
  PROJECTS_CHANGED_EVENT,
  loadProjects,
  projectTaskContext,
  type ProjectInfo,
} from "./features/projects/projectStorage";
import {
  filterModelProfilesBySelector,
  modelSelectorSchemaFromCatalog,
} from "./features/models";
import type { ConversationToolPreferences } from "./features/tools/types";
import { useToolSelectionController } from "./features/tools/useToolSelectionController";
import {
  AUTHORITY_WAITING_TEXT,
  authorityApprovalTitle,
  pendingAuthorityApproval,
  sanitizeAssistantAuthorityBoilerplate,
} from "./lib/authorityApproval";
import { subscribeAuthorityApprovalSettlements } from "./lib/authorityApprovalEvents";
import { browserApprovalRuntimeContent, pendingBrowserApproval, pendingRuntimeApproval, staleRuntimeApproval, type BrowserApproval, type RuntimeApproval, type StaleRuntimeApproval } from "./lib/browserApproval";
import { browserApprovalViewModel, runtimeApprovalViewModel, type ApprovalViewModel } from "./lib/approvalPresentation";
import { reduceBrowserStateFromEvents } from "./lib/browserState";
import { deriveConversationTitle, formatRelativeTime, inspectConversationIntegrity, messageToText, orderConversationMessages } from "./lib/chat";
import { isMessageScrollerNearBottom } from "./lib/chatScroll";
import { loadConversationForRefresh, resolveSupersededConversationRedirect } from "./lib/chatRouteLoading";
import { cn } from "./lib/cn";
import { deleteCalendarScheduleBeforeLocalChange } from "./lib/calendarScheduleDeletion";
import {
  canExecuteComposerEndpointAction,
  composerMentionMetadataFromWidgets,
  composerMentionSyntaxesForToolId,
  composerMentionToolIdsFromWidgets,
  composerSkillMentionWidget,
  composerToolMentionWidget,
  isSafeLocalEndpoint,
  normalizeComposerMentionMetadata,
  publicComposerWidgetMetadata,
  reconcileComposerSemanticDraft,
  skillMentionIdsFromText,
  toolMentionIdsFromText,
  trustedComposerActionForWidget,
  withComposerMentionSelectionOwnership,
} from "./lib/composerWidgets";
import { hasUnescapedMentionSyntax } from "./lib/mentionContract";
import {
  beginHighRiskAttempt,
  highRiskCommandRef,
  highRiskPrepareArguments,
  releaseHighRiskAttempt,
} from "./lib/highRiskCommand";
import { fileToAttachment } from "./lib/attachments";
import { toolGroupFor } from "./lib/toolUi";
import type { ComposerEntityReference } from "./lib/composerReferences";
import { conversationMatchesSpotlightFilter, conversationToSearchResult, type SpotlightFilter } from "./lib/conversationSpotlight";
import { boundedDurationLabel } from "./lib/duration";
import { openAuthorityApprovalWindow, openFingerRecordingWindow } from "./lib/desktopApproval";
import { fetchDesktopSystemInfo, type DesktopSystemInfo } from "./lib/desktopSystemInfo";
import { normalizeLocale } from "./lib/i18n";
import { shortcutLabel, shortcutSpecMatchesEvent } from "./lib/keyboardShortcuts";
import { PENDING_CHAT_REQUEST_TTL_MS, shouldClearPendingAfterConversationRefresh, shouldForgetPendingAfterPollError, type PendingChatRequest } from "./lib/pendingChat";
import { normalizePinnedPlacements, withPinnedPlacements } from "./lib/placement";
import { reportClientDiagnostic } from "./lib/clientDiagnostics";
import {
  DEFAULT_COMPOSER_HOME_TITLE,
  createSettingsModeDraft,
  normalizeComposerHomeTitle,
  resolveComposerHomeTitle,
  resolveSettingsAssistantSkill,
} from "./lib/settingsMode";
import { isRegisteredSlashCommand, mergeRegisteredSlashCommands, registeredSlashCommandsFromSettings } from "./lib/registeredSlashCommands";
import { selectTemplateAiInput, selectTemplateComposerInput, selectTemplateToolPolicy, templateAiInputParamsPayload, templateComposerWidgetsForInput, templateFeatureFlagEnabled, templateToolPolicyReferencePayload, templateToolPolicySettings } from "./lib/templateAiInput";
import { initialComposerFieldValues, normalizeComposerFields, structuredComposerPayload } from "./lib/structuredComposer";
import { isHumanOperatorCanvasPreview, isRecord, toolPreviewsFromMessages, upsertStreamActivityEvent } from "./lib/toolPreviews";
import { extractLatestToolFilterContext } from "./lib/toolStatus";
import { hasShellRegion } from "./lib/uiShell";
import { hasWorkspaceAttachment, workspaceFileToAttachment } from "./lib/workspaceAttachments";
import { createWidgetConversationContext } from "./lib/widgetContext";
import { promptResources } from "./features/prompts/resources/promptResources";
import { manualRuntimeModeSelectionEnabled } from "./features/runtimeMode/runtimeMode";
import { resolveDefaultspackRenderers } from "./renderers/defaultspackRenderers";
import { RendererBoundary } from "./renderers/trustedRendererLoader";
import type { AppMode, AttachedFile, ChatUiMessage, CodingContext, ComposerExtensionItem, ComposerModelStatusIndicator, ComposerSkillItem, ComposerSteerStatus, ContextUsageInfo, DroppedWidget, SettingsLoadState, SettingsSaveState } from "./renderers/types";
import { LayerPortal } from "./ui/layers/LayerPortal";

type ComposerCandidateMenuState = {
  mode: "model";
  query: string;
  candidates: ModelCommandCandidate[];
} | null;

type BackendConnectionState = "online" | "degraded" | "offline";

type CatalogRefreshResult = {
  catalog: UICatalog | null;
  ready: boolean;
  degraded: boolean;
  errorMessage: string | null;
};

type PendingMentionAttachmentRequest = {
  generation: number;
  token: number;
};

const AMBIENT_ROUTING_SETTING_KEYS: Record<string, keyof AmbientRoutingConfig> = {
  "ambient.routing.mode": "mode",
  "ambient.routing.model": "model",
  "ambient.routing.group_enabled": "group_enabled",
  "ambient.routing.group_id": "group_id",
  "ambient.routing.group_title": "group_title",
  "ambient.routing.ai_send_approval_required": "ai_send_approval_required",
};

type PendingNewTaskContext = {
  groupId?: string;
  workspaceId?: string | null;
  workspaceLabel?: string | null;
  workspaceRoot?: string | null;
  rumiDataPath?: string | null;
};

type CalendarItemKind = "task" | "event" | "reminder";

type CalendarItem = {
  id: string;
  date: string;
  endDate?: string;
  agentPrompt?: string;
  kind: CalendarItemKind;
  lastRunStatus?: string;
  scheduleId?: string;
  scheduleStatus?: string;
  title: string;
  time?: string;
};

type CalendarSettings = {
  agentCurrentChat: boolean;
  agentModel: string;
  agentTaskDefault: boolean;
  defaultTime: string;
  defaultItemType: CalendarItemKind;
  dimWeekends: boolean;
  eventColor: "green" | "blue" | "slate";
  maxItemsPerDay: number;
  quickAddEnabled: boolean;
  showOutsideDays: boolean;
  showTimePicker: boolean;
  taskColor: "blue" | "cyan" | "slate";
  timeSlotMinutes: 15 | 30 | 60;
  weekStart: "sunday" | "monday";
};

type SubmitOverride = {
  input: string;
  attachments: AttachedFile[];
  droppedWidgets: DroppedWidget[];
  toolSelectionRequest?: ToolSelectionRequest;
  skipReview?: boolean;
};

type RetryableSubmission = SubmitOverride & {
  errorMessage: string;
};

type ConversationScrollState = {
  follow: boolean;
  scrollTop: number;
};

// The shell may remount ChatApp while refreshing its resolved UI catalog.
// Keep the user's reading position outside the component so a remount cannot
// silently restore the default "follow bottom" behavior.
const conversationScrollState = new Map<string, ConversationScrollState>();

function toolIdsFromSelectionRequest(request: ToolSelectionRequest): string[] {
  const ids: string[] = [];
  for (const target of request.include ?? []) {
    if (typeof target === "string") {
      if (target.trim()) ids.push(target.trim());
      continue;
    }
    const structured = target as ToolTarget;
    if (structured.kind === "tool" && structured.id.trim()) ids.push(structured.id.trim());
  }
  return [...new Set(ids)];
}

function semanticAttachmentPaths(files: AttachedFile[]): string[] {
  return files.flatMap((file) => {
    if (file.sourcePath) return [file.sourcePath];
    return file.source === "workspace" && file.name ? [file.name] : [];
  });
}

function parseConversationToolPreferences(metadata: unknown): ConversationToolPreferences {
  const source = metadata && typeof metadata === "object" && !Array.isArray(metadata)
    ? (metadata as Record<string, unknown>).tool_preferences
    : null;
  const raw = source && typeof source === "object" && !Array.isArray(source)
    ? source as Record<string, unknown>
    : {};
  const mode = typeof raw.mode === "string" && ["auto", "review", "manual", "none"].includes(raw.mode)
    ? raw.mode as ConversationToolPreferences["mode"]
    : undefined;
  return {
    mode,
    include: normalizeConversationToolTargets(raw.include),
    exclude: normalizeConversationToolTargets(raw.exclude),
  };
}

function normalizeConversationToolTargets(value: unknown): ToolTarget[] {
  if (!Array.isArray(value)) return [];
  const targets: ToolTarget[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    let target: ToolTarget | null = null;
    if (typeof item === "string" && item.trim()) {
      target = { kind: "tool", id: item.trim() };
    } else if (item && typeof item === "object") {
      const raw = item as Record<string, unknown>;
      const kind = raw.kind === "service" ? "service" : raw.kind === "tool" ? "tool" : null;
      const id = typeof raw.id === "string" ? raw.id.trim() : "";
      if (kind && id) target = { kind, id };
    }
    if (!target) continue;
    const key = `${target.kind}:${target.id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    targets.push(target);
  }
  return targets;
}

type CalendarCell = {
  col: number;
  date: Date;
  isCurrentMonth: boolean;
  isToday: boolean;
  key: string;
  label: string;
  row: number;
};

type CalendarEditorState = {
  cell: CalendarCell;
  endKey: string;
  itemId?: string;
  mode: "create" | "edit";
  startKey: string;
};

type CalendarDragState = {
  currentKey: string;
  startKey: string;
  startedAt: number;
};

function formatLastHealthyLabel(timestamp: number | null): string | null {
  if (!timestamp) return null;
  return new Intl.DateTimeFormat("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(timestamp);
}

function backendConnectionCopy(
  state: BackendConnectionState,
  lastHealthyAt: number | null,
  note: string | null,
): { title: string; detail: string } {
  if (state === "offline") {
    return {
      title: "backend との接続が切れても、ここまでの表示は守ります。",
      detail: note || "再接続を試しながら、いま見えている会話と操作面を保持しています。",
    };
  }
  if (state === "degraded") {
    const lastHealthy = formatLastHealthyLabel(lastHealthyAt);
    return {
      title: "接続は揺れていますが、画面は崩さず受け止めます。",
      detail: lastHealthy
        ? `最後に backend を確認できたのは ${lastHealthy} です。いまは再接続を試しながら静かに保護運転へ切り替えています。`
        : "いまは再接続を試しながら静かに保護運転へ切り替えています。",
    };
  }
  return {
    title: "",
    detail: "",
  };
}

const calendarSettingsDefaults: CalendarSettings = {
  agentCurrentChat: false,
  agentModel: "",
  agentTaskDefault: false,
  defaultTime: "09:00",
  defaultItemType: "task",
  dimWeekends: true,
  eventColor: "green",
  maxItemsPerDay: 3,
  quickAddEnabled: true,
  showOutsideDays: true,
  showTimePicker: true,
  taskColor: "blue",
  timeSlotMinutes: 15,
  weekStart: "sunday",
};

function withCalendarSettingsValues(values: Record<string, Record<string, unknown>>): Record<string, Record<string, unknown>> {
  return {
    ...values,
    calendar: {
      agent_current_chat: calendarSettingsDefaults.agentCurrentChat,
      agent_model: calendarSettingsDefaults.agentModel,
      agent_task_default: calendarSettingsDefaults.agentTaskDefault,
      default_time: calendarSettingsDefaults.defaultTime,
      quick_add_enabled: calendarSettingsDefaults.quickAddEnabled,
      default_item_type: calendarSettingsDefaults.defaultItemType,
      week_start: calendarSettingsDefaults.weekStart,
      show_outside_days: calendarSettingsDefaults.showOutsideDays,
      show_time_picker: calendarSettingsDefaults.showTimePicker,
      dim_weekends: calendarSettingsDefaults.dimWeekends,
      task_color: calendarSettingsDefaults.taskColor,
      time_slot_minutes: calendarSettingsDefaults.timeSlotMinutes,
      event_color: calendarSettingsDefaults.eventColor,
      max_items_per_day: calendarSettingsDefaults.maxItemsPerDay,
      ...(values.calendar ?? {}),
    },
  };
}

type ExternalIoTemplateRecord = Record<string, unknown>;

const fallbackExternalIoTemplates: ExternalIoTemplateRecord[] = [
  {
    id: "line.input.default",
    direction: "input",
    provider: "line",
    input_profile_id: "line.default",
    endpoint: { id: "line-main", route: defaultspackCanonicalRouteKey("api/integrations/line/webhook") },
  },
  {
    id: "line.input.computer_use",
    direction: "input",
    provider: "line",
    input_profile_id: "line.computer_use",
    endpoint: { id: "line-main", route: defaultspackCanonicalRouteKey("api/integrations/line/webhook") },
    response: { mode: "computer_use_line_biz" },
    response_prompt: { preset: "computer_use_line_biz" },
  },
  {
    id: "discord.input.default",
    direction: "input",
    provider: "discord",
    input_profile_id: "discord.default",
    endpoint: { id: "discord-main", route: defaultspackCanonicalRouteKey("api/integrations/discord/interactions") },
  },
  {
    id: "slack.input.default",
    direction: "input",
    provider: "slack",
    input_profile_id: "slack.default",
    endpoint: { id: "slack-main", route: defaultspackCanonicalRouteKey("api/integrations/slack/events") },
  },
  {
    id: "generic.input.default",
    direction: "input",
    provider: "generic",
    input_profile_id: "generic.webhook.default",
    endpoint: { id: "generic-main", route: defaultspackCanonicalRouteKey("api/webhooks/inbound/{webhook_id}") },
  },
  {
    id: "line.output.default",
    direction: "output",
    provider: "line",
    output_profile_id: "line.default",
    response: { mode: "reply_to_origin" },
  },
  {
    id: "discord.output.bot_channel",
    direction: "output",
    provider: "discord",
    output_profile_id: "discord.bot_channel",
    response: { mode: "discord_bot_channel" },
  },
  {
    id: "discord.output.webhook",
    direction: "output",
    provider: "discord",
    output_profile_id: "discord.webhook",
    response: { mode: "discord_webhook_url" },
  },
  {
    id: "slack.output.default",
    direction: "output",
    provider: "slack",
    output_profile_id: "slack.default",
    response: { mode: "slack_channel" },
  },
  {
    id: "generic.output.webhook",
    direction: "output",
    provider: "generic",
    output_profile_id: "generic.webhook",
    response: { mode: "generic_webhook" },
  },
];

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function externalIoTemplateItems(catalog: UICatalog | null, direction: "input" | "output"): ExternalIoTemplateRecord[] {
  const catalogItems = Array.isArray(catalog?.external_io_templates) ? catalog.external_io_templates : [];
  const items = catalogItems.length ? catalogItems : fallbackExternalIoTemplates;
  return items.filter((item) => String(item.direction ?? "") === direction);
}

function externalIoTemplateById(catalog: UICatalog | null, direction: "input" | "output", templateId: string): ExternalIoTemplateRecord | null {
  return externalIoTemplateItems(catalog, direction).find((item) => String(item.id ?? "") === templateId) ?? null;
}

function firstExternalIoTemplateForProvider(catalog: UICatalog | null, direction: "input" | "output", provider: string): ExternalIoTemplateRecord | null {
  return externalIoTemplateItems(catalog, direction).find((item) => (
    String(item.provider ?? "") === provider && String(item.origin ?? "") !== "custom"
  )) ?? null;
}

function externalIoTemplateRoute(template: ExternalIoTemplateRecord | null): string {
  const endpoint = recordValue(template?.endpoint);
  const route = String(endpoint.route ?? "").trim();
  if (route) return route;
  const routes = Array.isArray(endpoint.routes) ? endpoint.routes : [];
  return String(routes[0] ?? "").trim();
}

function externalIoInputEndpointId(template: ExternalIoTemplateRecord | null, provider: string): string {
  const endpoint = recordValue(template?.endpoint);
  return String(endpoint.id ?? "").trim() || `${provider}-main`;
}

function externalIoOutputMode(template: ExternalIoTemplateRecord | null): string {
  const response = recordValue(template?.response);
  const defaultResponse = recordValue(template?.default_response);
  return String(
    template?.output_send_mode
      ?? template?.send_mode
      ?? response.mode
      ?? defaultResponse.mode
      ?? "",
  ).trim();
}

function externalIoTemplateForResponsePreset(catalog: UICatalog | null, preset: string): ExternalIoTemplateRecord | null {
  return externalIoTemplateItems(catalog, "input").find((item) => {
    const response = recordValue(item.response);
    const responsePrompt = recordValue(item.response_prompt);
    return String(response.mode ?? "") === preset || String(responsePrompt.preset ?? "") === preset;
  }) ?? null;
}

function calendarDateKey(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function calendarDateLabel(date: Date): string {
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`;
}

function calendarDateFromKey(key: string): Date {
  const [year, month, day] = key.split("-").map((part) => Number(part));
  return new Date(year, month - 1, day);
}

function compareCalendarKeys(a: string, b: string): number {
  return calendarDateFromKey(a).getTime() - calendarDateFromKey(b).getTime();
}

function orderedCalendarRange(startKey: string, endKey: string): [string, string] {
  return compareCalendarKeys(startKey, endKey) <= 0 ? [startKey, endKey] : [endKey, startKey];
}

function calendarKeysBetween(startKey: string, endKey: string): string[] {
  const [start, end] = orderedCalendarRange(startKey, endKey);
  const current = calendarDateFromKey(start);
  const endTime = calendarDateFromKey(end).getTime();
  const keys: string[] = [];
  while (current.getTime() <= endTime) {
    keys.push(calendarDateKey(current));
    current.setDate(current.getDate() + 1);
  }
  return keys;
}

function calendarRangeLabel(startKey: string, endKey: string): string {
  const [start, end] = orderedCalendarRange(startKey, endKey);
  const startLabel = calendarDateLabel(calendarDateFromKey(start));
  const endLabel = calendarDateLabel(calendarDateFromKey(end));
  return start === end ? startLabel : `${startLabel} - ${endLabel}`;
}

function calendarItemCoversDate(item: CalendarItem, key: string): boolean {
  const [start, end] = orderedCalendarRange(item.date, item.endDate ?? item.date);
  return compareCalendarKeys(key, start) >= 0 && compareCalendarKeys(key, end) <= 0;
}

function normalizeCalendarTimeInput(value: string | undefined, fallback = calendarSettingsDefaults.defaultTime): string {
  const source = String(value || "").trim();
  const fallbackMatch = /^(\d{1,2}):(\d{2})/.exec(fallback);
  const fallbackValue = fallbackMatch ? `${fallbackMatch[1].padStart(2, "0")}:${fallbackMatch[2]}` : "09:00";
  if (!source) return fallbackValue;

  const normalized = source
    .replace(/\s+/g, "")
    .replace(/[０-９]/g, (char) => String.fromCharCode(char.charCodeAt(0) - 0xfee0));
  const japaneseMatch = /^(午前|午後)(\d{1,2})(?::(\d{1,2}))?/.exec(normalized);
  if (japaneseMatch) {
    let hour = Number(japaneseMatch[2]);
    const minute = Number(japaneseMatch[3] ?? 0);
    if (!Number.isFinite(hour) || !Number.isFinite(minute)) return fallbackValue;
    if (japaneseMatch[1] === "午後" && hour < 12) hour += 12;
    if (japaneseMatch[1] === "午前" && hour === 12) hour = 0;
    return `${String(Math.max(0, Math.min(23, hour))).padStart(2, "0")}:${String(Math.max(0, Math.min(59, minute))).padStart(2, "0")}`;
  }
  const plainMatch = /^(\d{1,2})(?::(\d{1,2}))?/.exec(normalized);
  if (!plainMatch) return fallbackValue;
  const hour = Number(plainMatch[1]);
  const minute = Number(plainMatch[2] ?? 0);
  if (!Number.isFinite(hour) || !Number.isFinite(minute)) return fallbackValue;
  return `${String(Math.max(0, Math.min(23, hour))).padStart(2, "0")}:${String(Math.max(0, Math.min(59, minute))).padStart(2, "0")}`;
}

function formatCalendarTime(time: string | undefined): string {
  const normalized = normalizeCalendarTimeInput(time);
  const [hourText, minute] = normalized.split(":");
  const hour = Number(hourText);
  const period = hour < 12 ? "午前" : "午後";
  const hour12 = hour % 12 || 12;
  return `${period}${hour12}:${minute}`;
}

function buildCalendarTimeOptions(stepMinutes: CalendarSettings["timeSlotMinutes"]): string[] {
  const step = stepMinutes === 30 || stepMinutes === 60 ? stepMinutes : 15;
  const options: string[] = [];
  for (let minutes = 0; minutes < 24 * 60; minutes += step) {
    options.push(`${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`);
  }
  return options;
}

function calendarRunAtIso(dateKey: string, time: string): string {
  const normalized = normalizeCalendarTimeInput(time);
  return new Date(`${dateKey}T${normalized}:00`).toISOString();
}

function createCalendarItemId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `calendar-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function parseCalendarSettings(raw: Record<string, unknown> | undefined): CalendarSettings {
  const value = raw ?? {};
  const defaultItemType = String(value.default_item_type ?? calendarSettingsDefaults.defaultItemType);
  const eventColor = String(value.event_color ?? calendarSettingsDefaults.eventColor);
  const taskColor = String(value.task_color ?? calendarSettingsDefaults.taskColor);
  const weekStart = String(value.week_start ?? calendarSettingsDefaults.weekStart);
  const maxItems = Number(value.max_items_per_day ?? calendarSettingsDefaults.maxItemsPerDay);
  const slotMinutes = Number(value.time_slot_minutes ?? calendarSettingsDefaults.timeSlotMinutes);
  return {
    agentCurrentChat: value.agent_current_chat === true,
    agentModel: String(value.agent_model ?? "").trim(),
    agentTaskDefault: value.agent_task_default === true,
    defaultTime: normalizeCalendarTimeInput(String(value.default_time ?? calendarSettingsDefaults.defaultTime)),
    defaultItemType: defaultItemType === "event" || defaultItemType === "reminder" ? defaultItemType : "task",
    dimWeekends: value.dim_weekends !== false,
    eventColor: eventColor === "blue" || eventColor === "slate" ? eventColor : "green",
    maxItemsPerDay: Number.isFinite(maxItems) ? Math.max(1, Math.min(6, Math.round(maxItems))) : calendarSettingsDefaults.maxItemsPerDay,
    quickAddEnabled: value.quick_add_enabled !== false,
    showOutsideDays: value.show_outside_days !== false,
    showTimePicker: value.show_time_picker !== false,
    taskColor: taskColor === "cyan" || taskColor === "slate" ? taskColor : "blue",
    timeSlotMinutes: slotMinutes === 30 || slotMinutes === 60 ? slotMinutes : 15,
    weekStart: weekStart === "monday" ? "monday" : "sunday",
  };
}

function calendarItemClassName(item: CalendarItem, settings: CalendarSettings): string {
  if (item.kind === "task") {
    if (settings.taskColor === "cyan") return "bg-cyan-500/85 text-cyan-950";
    if (settings.taskColor === "slate") return "bg-zinc-300/85 text-zinc-950";
    return "bg-blue-500/90 text-white";
  }
  if (item.kind === "event") {
    if (settings.eventColor === "blue") return "bg-blue-500/85 text-white";
    if (settings.eventColor === "slate") return "bg-zinc-300/85 text-zinc-950";
    return "bg-emerald-500/85 text-emerald-950";
  }
  return "bg-zinc-500/80 text-zinc-50";
}

function resolveCalendarAgentModel(settings: CalendarSettings, activeModelId: string, profiles: ModelProfile[]): string {
  if (settings.agentModel) return settings.agentModel;
  const isUsableProfile = (profile: ModelProfile): boolean => {
    const availability = profile.availability ?? {};
    const metadata = profile.metadata ?? {};
    const configurationSource = String(availability.configuration_source ?? metadata.configuration_source ?? "").toLowerCase();
    if (configurationSource === "no_key_gateway") return false;
    return Boolean(profile.local || availability.local === true || availability.configured === true || availability.status === "configured");
  };
  const activeProfile = profiles.find((profile) => profile.profile_id === activeModelId || profile.qualified_model_id === activeModelId);
  if (activeProfile && isUsableProfile(activeProfile)) return activeModelId;
  const configuredProfiles = profiles.filter((profile) => {
    const id = `${profile.profile_id} ${profile.qualified_model_id} ${profile.model_id}`.toLowerCase();
    return isUsableProfile(profile) && !id.includes("embedding");
  });
  const configuredProfile = configuredProfiles.find((profile) => {
    const id = `${profile.profile_id} ${profile.qualified_model_id} ${profile.model_id}`.toLowerCase();
    return id.includes("gemini") && id.includes("flash");
  }) ?? configuredProfiles[0];
  return configuredProfile?.profile_id || configuredProfile?.qualified_model_id || activeModelId || "default";
}

function CalendarComposerPanel({
  conversationId,
  modelId,
  modelProfiles,
  settings,
}: {
  conversationId: string | null;
  modelId: string;
  modelProfiles: ModelProfile[];
  settings: CalendarSettings;
}) {
  const today = new Date();
  const [visibleMonth, setVisibleMonth] = useState(() => new Date(today.getFullYear(), today.getMonth(), 1));
  const year = visibleMonth.getFullYear();
  const month = visibleMonth.getMonth();
  const monthStart = new Date(year, month, 1);
  const weekStartIndex = settings.weekStart === "monday" ? 1 : 0;
  const weekLabels = ["日", "月", "火", "水", "木", "金", "土"];
  const visibleWeekLabels = weekLabels.map((_, index) => weekLabels[(index + weekStartIndex) % 7]);
  const monthStartOffset = (monthStart.getDay() - weekStartIndex + 7) % 7;
  const [items, setItems] = useLocalStorage<CalendarItem[]>("defaultspack.calendar.items.v1", []);
  const [activeEditor, setActiveEditor] = useState<CalendarEditorState | null>(null);
  const [dragState, setDragState] = useState<CalendarDragState | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftKind, setDraftKind] = useState<CalendarItemKind>(settings.defaultItemType);
  const [draftTime, setDraftTime] = useState(formatCalendarTime(settings.defaultTime));
  const [draftAgentEnabled, setDraftAgentEnabled] = useState(settings.agentTaskDefault);
  const [draftAgentPrompt, setDraftAgentPrompt] = useState("");
  const [draftError, setDraftError] = useState<string | null>(null);
  const [isSavingDraft, setIsSavingDraft] = useState(false);
  const [isTimeMenuOpen, setIsTimeMenuOpen] = useState(false);
  const [lastAgentResult, setLastAgentResult] = useState<string | null>(null);
  const calendarRef = useRef<HTMLElement | null>(null);
  const suppressNextCellOpenRef = useRef(false);

  useEffect(() => {
    setDraftKind(settings.defaultItemType);
  }, [settings.defaultItemType]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setActiveEditor(null);
        setDragState(null);
        setIsTimeMenuOpen(false);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (!calendarRef.current?.contains(target)) {
        setActiveEditor(null);
        setDragState(null);
        setIsTimeMenuOpen(false);
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, []);

  const calendarCells = Array.from({ length: 42 }, (_, index): CalendarCell => {
    const date = new Date(year, month, 1 + index - monthStartOffset);
    const isCurrentMonth = date.getMonth() === month;
    const isToday = (
      date.getFullYear() === today.getFullYear()
      && date.getMonth() === today.getMonth()
      && date.getDate() === today.getDate()
    );
    const row = Math.floor(index / 7);
    const col = index % 7;
    return {
      col,
      date,
      isCurrentMonth,
      isToday,
      key: calendarDateKey(date),
      label: date.getDate() === 1 ? `${date.getMonth() + 1}月 1日` : String(date.getDate()),
      row,
    };
  });
  const itemsByDate = items.reduce<Record<string, CalendarItem[]>>((acc, item) => {
    if (!item.date || !item.title) return acc;
    for (const key of calendarKeysBetween(item.date, item.endDate ?? item.date)) {
      acc[key] = [...(acc[key] ?? []), item].sort((left, right) => {
        const timeOrder = String(left.time ?? "").localeCompare(String(right.time ?? ""));
        return timeOrder || left.title.localeCompare(right.title);
      });
    }
    return acc;
  }, {});
  const activeRangeKeys = activeEditor ? new Set(calendarKeysBetween(activeEditor.startKey, activeEditor.endKey)) : new Set<string>();
  const dragRangeKeys = dragState ? new Set(calendarKeysBetween(dragState.startKey, dragState.currentKey)) : new Set<string>();
  const activeItem = activeEditor?.itemId ? items.find((item) => item.id === activeEditor.itemId) ?? null : null;
  const timeOptions = buildCalendarTimeOptions(settings.timeSlotMinutes);
  const popoverStyle = activeEditor ? {
    left: `${(activeEditor.cell.col / 7) * 100}%`,
    top: `${(activeEditor.cell.row / 6) * 100}%`,
    transform: `${activeEditor.cell.col >= 5 ? "translateX(calc(-100% - 10px))" : "translateX(10px)"} ${activeEditor.cell.row >= 4 ? "translateY(calc(-100% - 10px))" : "translateY(36px)"}`,
  } : undefined;

  const dismissActiveEditorForSelection = (suppressCellMouseUp = false) => {
    if (!activeEditor) return false;
    suppressNextCellOpenRef.current = suppressCellMouseUp;
    setActiveEditor(null);
    setDragState(null);
    setIsTimeMenuOpen(false);
    return true;
  };

  const moveVisibleMonth = (offset: number) => {
    suppressNextCellOpenRef.current = false;
    setActiveEditor(null);
    setDragState(null);
    setIsTimeMenuOpen(false);
    setVisibleMonth((current) => new Date(current.getFullYear(), current.getMonth() + offset, 1));
  };

  const returnToToday = () => {
    suppressNextCellOpenRef.current = false;
    setActiveEditor(null);
    setDragState(null);
    setIsTimeMenuOpen(false);
    setVisibleMonth(new Date(today.getFullYear(), today.getMonth(), 1));
  };

  const resetDraftForCreate = (kind = settings.defaultItemType) => {
    setDraftTitle("");
    setDraftKind(kind);
    setDraftTime(formatCalendarTime(settings.defaultTime));
    setDraftAgentEnabled(settings.agentTaskDefault && kind === "task");
    setDraftAgentPrompt("");
    setDraftError(null);
    setLastAgentResult(null);
    setIsTimeMenuOpen(false);
  };

  const openCreateEditor = (cell: CalendarCell, startKey = cell.key, endKey = cell.key) => {
    if (!settings.quickAddEnabled) return;
    resetDraftForCreate(settings.defaultItemType);
    setActiveEditor({ mode: "create", cell, startKey, endKey });
  };

  const openEditEditor = (item: CalendarItem, cell: CalendarCell) => {
    setActiveEditor({
      mode: "edit",
      itemId: item.id,
      cell,
      startKey: item.date,
      endKey: item.endDate ?? item.date,
    });
    setDraftTitle(item.title);
    setDraftKind(item.kind);
    setDraftTime(formatCalendarTime(item.time ?? settings.defaultTime));
    setDraftAgentEnabled(Boolean(item.scheduleId));
    setDraftAgentPrompt(item.agentPrompt ?? item.title);
    setDraftError(null);
    setLastAgentResult(item.lastRunStatus ? `Agent last run: ${item.lastRunStatus}` : null);
    setIsTimeMenuOpen(false);
  };

  const schedulePayloadForItem = (itemId: string, title: string, startKey: string, endKey: string, time: string, agentPrompt: string) => ({
    name: `Calendar: ${title}`,
    description: `Created from Rumi calendar for ${calendarRangeLabel(startKey, endKey)}.`,
    schedule_type: "once",
    schedule_config: { run_at: calendarRunAtIso(startKey, time) },
    task: {
      message: agentPrompt || title,
      model: resolveCalendarAgentModel(settings, modelId, modelProfiles),
      conversation_id: settings.agentCurrentChat ? conversationId || null : null,
      metadata: {
        source: "calendar",
        calendar_item_id: itemId,
        calendar_start_date: startKey,
        calendar_end_date: endKey,
        calendar_time: normalizeCalendarTimeInput(time),
      },
    },
  });

  const extractScheduleRecord = (response: Record<string, unknown>): Record<string, unknown> => {
    const data = response.data;
    return isRecord(data) ? data : response;
  };

  const persistAgentSchedule = async (
    existing: CalendarItem | null,
    itemId: string,
    title: string,
    startKey: string,
    endKey: string,
    time: string,
    agentPrompt: string,
  ): Promise<{ scheduleId?: string; scheduleStatus?: string }> => {
    const payload = schedulePayloadForItem(itemId, title, startKey, endKey, time, agentPrompt);
    if (existing?.scheduleId) {
      const updated = extractScheduleRecord(await api.updateSchedule(existing.scheduleId, payload));
      return {
        scheduleId: String(updated.id ?? existing.scheduleId),
        scheduleStatus: String(updated.status ?? existing.scheduleStatus ?? "active"),
      };
    }
    const created = extractScheduleRecord(await api.createSchedule(payload));
    const scheduleId = created.id ? String(created.id) : undefined;
    return {
      scheduleId,
      scheduleStatus: String(created.status ?? "active"),
    };
  };

  const submitDraft = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!activeEditor) return;
    setIsSavingDraft(true);
    setDraftError(null);
    setLastAgentResult(null);
    const [startKey, endKey] = orderedCalendarRange(activeEditor.startKey, activeEditor.endKey);
    const title = draftTitle.trim() || (draftKind === "task" ? "New task" : draftKind === "event" ? "New event" : "Reminder");
    const normalizedTime = normalizeCalendarTimeInput(draftTime, settings.defaultTime);
    const existing = activeEditor.itemId ? items.find((item) => item.id === activeEditor.itemId) ?? null : null;
    const itemId = existing?.id ?? createCalendarItemId();
    const agentPrompt = draftAgentPrompt.trim() || title;
    try {
      let scheduleId = existing?.scheduleId;
      let scheduleStatus = existing?.scheduleStatus;
      if (draftKind === "task" && draftAgentEnabled) {
        const schedule = await persistAgentSchedule(existing, itemId, title, startKey, endKey, normalizedTime, agentPrompt);
        scheduleId = schedule.scheduleId;
        scheduleStatus = schedule.scheduleStatus;
      } else if (existing?.scheduleId) {
        await deleteCalendarScheduleBeforeLocalChange(existing.scheduleId, api.deleteSchedule);
        scheduleId = undefined;
        scheduleStatus = undefined;
      }
      const nextItem: CalendarItem = {
        id: itemId,
        date: startKey,
        endDate: endKey === startKey ? undefined : endKey,
        kind: draftKind,
        title,
        time: normalizedTime,
        agentPrompt: draftKind === "task" && draftAgentEnabled ? agentPrompt : undefined,
        scheduleId,
        scheduleStatus,
        lastRunStatus: existing?.lastRunStatus,
      };
      setItems((current) => activeEditor.mode === "edit"
        ? current.map((item) => item.id === itemId ? nextItem : item)
        : [...current, nextItem]);
      setActiveEditor(null);
      setDraftTitle("");
      setIsTimeMenuOpen(false);
    } catch (error) {
      setDraftError(error instanceof Error ? error.message : "Agent task schedule failed.");
    } finally {
      setIsSavingDraft(false);
    }
  };

  const deleteActiveItem = async () => {
    if (!activeItem) return;
    setIsSavingDraft(true);
    setDraftError(null);
    try {
      await deleteCalendarScheduleBeforeLocalChange(activeItem.scheduleId, api.deleteSchedule);
      setItems((current) => current.filter((item) => item.id !== activeItem.id));
      setActiveEditor(null);
    } catch (error) {
      setDraftError(error instanceof Error ? error.message : "Delete failed.");
    } finally {
      setIsSavingDraft(false);
    }
  };

  const runActiveAgentNow = async () => {
    if (!activeItem?.scheduleId) return;
    setIsSavingDraft(true);
    setDraftError(null);
    try {
      const response = extractScheduleRecord(await api.triggerSchedule(activeItem.scheduleId));
      const status = String(response.status ?? "triggered");
      setItems((current) => current.map((item) => item.id === activeItem.id ? { ...item, lastRunStatus: status } : item));
      setLastAgentResult(`Agent run: ${status}`);
    } catch (error) {
      setDraftError(error instanceof Error ? error.message : "Agent trigger failed.");
    } finally {
      setIsSavingDraft(false);
    }
  };

  const handleCellMouseDown = (event: ReactMouseEvent<HTMLDivElement>, cell: CalendarCell) => {
    if (event.button !== 0 || cell.isCurrentMonth === false && !settings.showOutsideDays) return;
    event.preventDefault();
    if (dismissActiveEditorForSelection(true)) return;
    setDragState({ startKey: cell.key, currentKey: cell.key, startedAt: Date.now() });
  };

  const handleCellMouseEnter = (cell: CalendarCell) => {
    setDragState((current) => current ? { ...current, currentKey: cell.key } : current);
  };

  const handleCellMouseUp = (event: ReactMouseEvent<HTMLDivElement>, cell: CalendarCell) => {
    if (event.button !== 0) return;
    event.preventDefault();
    if (suppressNextCellOpenRef.current) {
      suppressNextCellOpenRef.current = false;
      return;
    }
    const currentDrag = dragState;
    setDragState(null);
    if (!currentDrag) {
      openCreateEditor(cell);
      return;
    }
    const holdMs = Date.now() - currentDrag.startedAt;
    const endKey = currentDrag.currentKey || cell.key;
    openCreateEditor(cell, currentDrag.startKey, endKey);
    if (holdMs > 360 || currentDrag.startKey !== endKey) {
      setDraftTitle("");
    }
  };

  return (
    <section
      ref={calendarRef}
      aria-label="Calendar month"
      className="relative flex h-full min-h-0 w-full flex-col overflow-hidden rounded-lg border border-zinc-800 bg-[#101112] shadow-[0_20px_60px_rgba(0,0,0,0.32)]"
    >
      <div className="flex h-12 flex-shrink-0 items-center justify-between border-b border-zinc-800/80 bg-[#121314] px-4">
        <div className="flex items-center gap-2">
        <button
          type="button"
          aria-label="前の月"
          title="前の月"
          className="flex h-8 w-8 items-center justify-center rounded-md border border-zinc-700/80 bg-zinc-950/70 text-lg leading-none text-zinc-300 transition-colors hover:border-zinc-500 hover:bg-zinc-900 hover:text-zinc-50"
          onClick={() => moveVisibleMonth(-1)}
        >
          ‹
        </button>
        <button
          type="button"
          aria-label="今日"
          title="今日"
          className="rounded-md border border-zinc-700/80 bg-zinc-950/70 px-3 py-1.5 text-[12px] font-semibold text-zinc-200 transition-colors hover:border-zinc-500 hover:bg-zinc-900 hover:text-zinc-50"
          onClick={returnToToday}
        >
          {year}年{month + 1}月
        </button>
        <button
          type="button"
          aria-label="次の月"
          title="次の月"
          className="flex h-8 w-8 items-center justify-center rounded-md border border-zinc-700/80 bg-zinc-950/70 text-lg leading-none text-zinc-300 transition-colors hover:border-zinc-500 hover:bg-zinc-900 hover:text-zinc-50"
          onClick={() => moveVisibleMonth(1)}
        >
          ›
        </button>
        </div>
        <div className="h-8 w-[112px]" aria-hidden="true" />
      </div>
      <div className="grid min-h-0 flex-1 grid-cols-7 grid-rows-6 overflow-hidden">
        {calendarCells.map((cell, index) => {
          const visibleItems = (itemsByDate[cell.key] ?? []).slice(0, settings.maxItemsPerDay);
          const hiddenCount = Math.max(0, (itemsByDate[cell.key] ?? []).length - visibleItems.length);
          const isOutsideHidden = !cell.isCurrentMonth && !settings.showOutsideDays;
          const isWeekend = (cell.date.getDay() === 0 || cell.date.getDay() === 6) && settings.dimWeekends;
          const isSelected = activeRangeKeys.has(cell.key);
          const isDragSelected = dragRangeKeys.has(cell.key);
          return (
            <div
              key={`${cell.date.toISOString()}-${index}`}
              role="button"
              tabIndex={isOutsideHidden ? -1 : 0}
              data-testid={`calendar-day-${cell.key}`}
              aria-label={`${calendarDateLabel(cell.date)} の予定を追加`}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  if (dismissActiveEditorForSelection()) return;
                  openCreateEditor(cell);
                }
              }}
              onMouseDown={(event) => handleCellMouseDown(event, cell)}
              onMouseEnter={() => handleCellMouseEnter(cell)}
              onMouseUp={(event) => handleCellMouseUp(event, cell)}
              className={cn(
                "relative flex min-h-0 flex-col items-stretch border-b border-r border-zinc-800/90 px-2 py-2 text-left transition-colors hover:bg-zinc-900/70 focus:outline-none focus-visible:bg-zinc-900 focus-visible:ring-2 focus-visible:ring-blue-400/70",
                !cell.isCurrentMonth && "text-zinc-600",
                isOutsideHidden && "cursor-default text-transparent hover:bg-transparent",
                isWeekend && cell.isCurrentMonth && "bg-black/10",
                isSelected && "bg-blue-950/20 ring-2 ring-inset ring-blue-400/70",
                isDragSelected && "bg-blue-950/35",
              )}
            >
              {cell.row === 0 && (
                <div className="mb-1.5 text-center text-[12px] font-semibold text-zinc-500">
                  {visibleWeekLabels[cell.col]}
                </div>
              )}
              <div className="flex justify-center">
                <span
                  className={cn(
                    "inline-flex min-h-6 min-w-6 items-center justify-center rounded-full px-1.5 text-[13px] font-semibold leading-none text-zinc-300",
                    !cell.isCurrentMonth && "text-zinc-500",
                    cell.isToday && "bg-zinc-100 text-zinc-950 shadow-[0_0_0_1px_rgba(255,255,255,0.18)]",
                  )}
                >
                  {isOutsideHidden ? "" : cell.label}
                </span>
              </div>
              <div className="mt-2 flex min-h-0 flex-1 flex-col gap-1 overflow-hidden">
                {visibleItems.map((item) => (
                  <button
                    type="button"
                    key={item.id}
                    data-testid={`calendar-item-${item.id}`}
                    className={cn("truncate rounded-[7px] px-2 py-0.5 text-left text-[10.5px] font-medium leading-5 shadow-sm transition-opacity hover:opacity-90", calendarItemClassName(item, settings))}
                    title={item.title}
                    onPointerDown={(event) => event.stopPropagation()}
                    onPointerUp={(event) => event.stopPropagation()}
                    onMouseDown={(event) => event.stopPropagation()}
                    onMouseUp={(event) => event.stopPropagation()}
                    onClick={(event) => {
                      event.stopPropagation();
                      if (dismissActiveEditorForSelection()) return;
                      openEditEditor(item, cell);
                    }}
                  >
                    {item.time && <span className="mr-1 opacity-75">{formatCalendarTime(item.time)}</span>}
                    {item.title}
                  </button>
                ))}
                {hiddenCount > 0 && (
                  <div className="text-[10px] font-medium text-zinc-500">ほか{hiddenCount}件</div>
                )}
              </div>
            </div>
          );
        })}
      </div>
      {activeEditor && settings.quickAddEnabled && (
        <form
          key={`${activeEditor.mode}-${activeEditor.itemId ?? "new"}-${activeEditor.startKey}-${activeEditor.endKey}`}
          role="dialog"
          aria-label={`${calendarRangeLabel(activeEditor.startKey, activeEditor.endKey)}に追加`}
          className="rumi-calendar-popover absolute rumi-layer-global-overlay w-[min(320px,calc(100%-24px))] rounded-2xl border border-zinc-700 bg-zinc-950/95 p-3 text-left shadow-[0_24px_70px_rgba(0,0,0,0.65)] backdrop-blur"
          style={popoverStyle}
          onPointerDown={(event) => {
            const target = event.target as HTMLElement | null;
            if (target?.closest("button, input, textarea, label, [role='listbox'], [role='option']")) {
              event.stopPropagation();
              return;
            }
            dismissActiveEditorForSelection(true);
          }}
          onClick={(event) => event.stopPropagation()}
          onSubmit={submitDraft}
        >
          <div className="mb-3 flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-[11px] uppercase tracking-[0.2em] text-zinc-600">{activeEditor.mode === "edit" ? "項目を編集" : "新規項目"}</p>
              <p className="truncate text-sm font-semibold text-zinc-100">{calendarRangeLabel(activeEditor.startKey, activeEditor.endKey)}</p>
            </div>
            <button
              type="button"
              onClick={() => setActiveEditor(null)}
              className="flex h-7 w-7 items-center justify-center rounded-lg text-zinc-500 hover:bg-zinc-900 hover:text-zinc-200"
              aria-label="カレンダーのクイック追加を閉じる"
            >
              ×
            </button>
          </div>
          <input
            autoFocus
            value={draftTitle}
            onChange={(event) => setDraftTitle(event.target.value)}
            placeholder="何を追加しますか？"
            className="h-10 w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 text-sm text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-blue-400/70"
          />
          <div className="mt-3 grid grid-cols-3 gap-1.5">
            {(["task", "event", "reminder"] as CalendarItemKind[]).map((kind) => (
              <button
                key={kind}
                type="button"
                onClick={() => {
                  setDraftKind(kind);
                  setDraftAgentEnabled((current) => kind === "task" ? current || settings.agentTaskDefault : false);
                }}
                className={cn(
                  "rounded-lg border px-2 py-1.5 text-xs font-medium transition-colors",
                  draftKind === kind
                    ? "border-zinc-200 bg-zinc-100 text-zinc-950"
                    : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100",
                )}
              >
                {kind === "task" ? "タスク" : kind === "event" ? "予定" : "リマインダー"}
              </button>
            ))}
          </div>
          <div className="mt-3 flex items-end gap-2">
            <label className="relative flex-1">
              <span className="mb-1 block text-[10px] uppercase tracking-[0.18em] text-zinc-600">時刻</span>
              <input
                type="text"
                value={draftTime}
                aria-label="カレンダー項目の時刻"
                onClick={() => setIsTimeMenuOpen(settings.showTimePicker)}
                onFocus={() => setIsTimeMenuOpen(settings.showTimePicker)}
                onKeyDown={(event) => {
                  if (event.key === "Escape") {
                    event.stopPropagation();
                    setIsTimeMenuOpen(false);
                  }
                  if (event.key === "Enter") {
                    setIsTimeMenuOpen(false);
                  }
                }}
                onBlur={() => window.setTimeout(() => setIsTimeMenuOpen(false), 120)}
                onChange={(event) => {
                  setDraftTime(event.target.value);
                  setIsTimeMenuOpen(settings.showTimePicker);
                }}
                className="h-9 w-full rounded-lg border border-zinc-800 bg-zinc-900 px-2 text-xs text-zinc-200 outline-none focus:border-zinc-600"
              />
              {isTimeMenuOpen && settings.showTimePicker && (
                <div
                  role="listbox"
                  aria-label="カレンダー時刻候補"
                  className="absolute bottom-11 left-0 rumi-layer-global-overlay max-h-[300px] w-[210px] overflow-y-auto rounded-[22px] border border-zinc-700 bg-zinc-800 p-1.5 shadow-[0_18px_60px_rgba(0,0,0,0.55)]"
                >
                  {timeOptions.map((option) => (
                    <button
                      key={option}
                      type="button"
                      role="option"
                      aria-selected={normalizeCalendarTimeInput(draftTime, settings.defaultTime) === option}
                      className={cn(
                        "block w-full rounded-xl px-3 py-2 text-left text-[15px] leading-6 text-zinc-100 hover:bg-zinc-700",
                        normalizeCalendarTimeInput(draftTime, settings.defaultTime) === option && "bg-zinc-700",
                      )}
                      onClick={() => {
                        setDraftTime(formatCalendarTime(option));
                        setIsTimeMenuOpen(false);
                      }}
                    >
                      {formatCalendarTime(option)}
                    </button>
                  ))}
                </div>
              )}
            </label>
            <button
              type="submit"
              disabled={isSavingDraft}
              className="h-9 rounded-lg bg-zinc-100 px-4 text-xs font-semibold text-zinc-950 transition-colors hover:bg-white disabled:cursor-not-allowed disabled:opacity-50"
            >
              {activeEditor.mode === "edit" ? "保存" : "追加"}
            </button>
          </div>
          {draftKind === "task" && (
            <div className="mt-3 rounded-xl border border-zinc-800 bg-zinc-900/50 p-2.5">
              <label className="flex items-center justify-between gap-3 text-xs font-medium text-zinc-200">
                <span>Agentタスク</span>
                <input
                  type="checkbox"
                  checked={draftAgentEnabled}
                  onChange={(event) => setDraftAgentEnabled(event.target.checked)}
                  className="h-4 w-4 accent-blue-500"
                />
              </label>
              {draftAgentEnabled && (
                <textarea
                  value={draftAgentPrompt}
                  onChange={(event) => setDraftAgentPrompt(event.target.value)}
                  placeholder="エージェントに実行させる内容。空ならタイトルを使います。"
                  className="mt-2 h-16 w-full resize-none rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-xs text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-blue-400/70"
                />
              )}
            </div>
          )}
          {draftError ? (
            <ErrorNotice
              className="mt-3 rounded-lg px-2.5 py-2 text-xs"
              copyLabel="Copy calendar action error"
              copyText={draftError}
              errorIcon="calendar-action"
              message={draftError}
            />
          ) : lastAgentResult || activeItem?.scheduleId ? (
            <div className="mt-3 rounded-lg border border-blue-500/30 bg-blue-500/10 px-2.5 py-2 text-xs text-blue-100">
              {lastAgentResult ?? `Agentスケジュール: ${activeItem?.scheduleStatus ?? "有効"}`}
            </div>
          ) : null}
          {activeEditor.mode === "edit" && (
            <div className="mt-3 flex items-center justify-between gap-2">
              <button
                type="button"
                onClick={() => void deleteActiveItem()}
                disabled={isSavingDraft}
                className="rounded-lg border border-red-500/30 px-3 py-1.5 text-xs font-medium text-red-200 hover:bg-red-500/10 disabled:opacity-50"
              >
                削除
              </button>
              {activeItem?.scheduleId && (
                <button
                  type="button"
                  onClick={() => void runActiveAgentNow()}
                  disabled={isSavingDraft}
                  className="rounded-lg border border-blue-500/30 px-3 py-1.5 text-xs font-medium text-blue-100 hover:bg-blue-500/10 disabled:opacity-50"
                >
                  今すぐ実行
                </button>
              )}
            </div>
          )}
          <div className="sr-only">
            <input
              type="time"
              value={normalizeCalendarTimeInput(draftTime, settings.defaultTime)}
              readOnly
            />
          </div>
        </form>
      )}
    </section>
  );
}

function useLocalStorage<T>(key: string, defaultValue: T): [T, (v: T | ((prev: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const saved = localStorage.getItem(key);
      return saved ? JSON.parse(saved) : defaultValue;
    } catch {
      return defaultValue;
    }
  });

  useEffect(() => {
    localStorage.setItem(key, JSON.stringify(value));
  }, [key, value]);

  return [value, setValue];
}

function clampNumber(value: unknown, min: number, max: number, fallback: number): number {
  const numeric = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(numeric)) return fallback;
  return Math.max(min, Math.min(max, numeric));
}

export function shouldAutoCompactHistory(width: number): boolean {
  return width < 760;
}

function writeJsonLocalStorage<T>(key: string, value: T) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage may be unavailable in restricted browser contexts.
  }
}

function cleanOptionalString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function workspaceContextFromMetadata(metadata: Record<string, unknown> | null | undefined): PendingNewTaskContext {
  return {
    groupId: cleanOptionalString(metadata?.group_id ?? metadata?.groupId) ?? undefined,
    workspaceId: cleanOptionalString(metadata?.workspace_id ?? metadata?.workspaceId),
    workspaceLabel: cleanOptionalString(metadata?.workspace_label ?? metadata?.workspaceLabel),
    workspaceRoot: cleanOptionalString(metadata?.workspace_root ?? metadata?.workspaceRoot ?? metadata?.rootPath),
    rumiDataPath: cleanOptionalString(metadata?.rumi_data_path ?? metadata?.rumiDataPath ?? metadata?.rumi_dp_path),
  };
}

function workspaceContextFromConversation(conversation: Conversation | null | undefined): PendingNewTaskContext {
  const metadataContext = workspaceContextFromMetadata(conversation?.metadata);
  return {
    ...metadataContext,
    groupId: cleanOptionalString(conversation?.group_id) ?? metadataContext.groupId,
  };
}

function workspaceContextFromHistoryOptions(options?: HistoryBoardNewTaskOptions): PendingNewTaskContext | null {
  if (!options) return null;
  const context: PendingNewTaskContext = {
    groupId: cleanOptionalString(options.groupId) ?? undefined,
    workspaceId: cleanOptionalString(options.workspaceId),
    workspaceLabel: cleanOptionalString(options.workspaceLabel),
    workspaceRoot: cleanOptionalString(options.workspaceRoot),
    rumiDataPath: cleanOptionalString(options.rumiDataPath),
  };
  return context.groupId || context.workspaceId || context.workspaceRoot || context.rumiDataPath ? context : null;
}

function formatBoardDate(updatedAt: number): string {
  const diffHours = (Date.now() - updatedAt) / 3_600_000;
  if (diffHours < 24) return "今日";
  if (diffHours < 48) return "昨日";
  if (diffHours < 24 * 7) return "過去7日";
  return formatRelativeTime(updatedAt);
}

function externalConversationSection(conversation: Conversation): { id: string; title: string } | null {
  const metadata = conversation.metadata ?? {};
  const provider = typeof metadata.external_provider === "string" ? metadata.external_provider.trim().toLowerCase() : "";
  if (!provider) return null;
  if (provider === "line") {
    return { id: "integration-line", title: "LINE" };
  }
  return {
    id: `integration-${provider}`,
    title: provider.slice(0, 1).toUpperCase() + provider.slice(1),
  };
}

function toChatItem(conversation: Conversation): ChatItem {
  const section = externalConversationSection(conversation);
  const metadata = conversation.metadata ?? {};
  const groupId = cleanOptionalString(conversation.group_id) ?? cleanOptionalString(metadata.group_id ?? metadata.groupId);
  const normalizedMetadata: Record<string, unknown> = {
    ...metadata,
    ...(groupId ? { group_id: groupId } : {}),
  };
  return {
    id: conversation.id,
    title: conversation.title,
    date: formatBoardDate(conversation.updated_at),
    type: "chat",
    parentId: conversation.parent_conversation_id ?? null,
    conversationKind: conversation.conversation_kind ?? "chat",
    sectionId: section?.id ?? null,
    sectionTitle: section?.title ?? null,
    tags: conversation.tags ?? [],
    isStarred: conversation.is_starred,
    isPinned: Boolean(conversation.is_pinned),
    companyId: typeof normalizedMetadata.company_id === "string" ? normalizedMetadata.company_id : null,
    workspaceId: typeof normalizedMetadata.workspace_id === "string" ? normalizedMetadata.workspace_id : null,
    metadata: normalizedMetadata,
  };
}

function buildChatItems(conversations: Conversation[]): ChatItem[] {
  const byId = new Map(conversations.map((conversation) => [conversation.id, conversation]));
  const childIds = new Set<string>();

  for (const conversation of conversations) {
    if (conversation.parent_conversation_id) {
      childIds.add(conversation.id);
    }
    for (const childId of conversation.child_conversation_ids ?? []) {
      if (byId.has(childId)) childIds.add(childId);
    }
  }

  const build = (conversation: Conversation): ChatItem => {
    const linkedChildren = [
      ...new Set([
        ...(conversation.child_conversation_ids ?? []),
        ...conversations
          .filter((candidate) => candidate.parent_conversation_id === conversation.id)
          .map((candidate) => candidate.id),
      ]),
    ]
      .map((childId) => byId.get(childId))
      .filter((child): child is Conversation => Boolean(child))
      .sort((a, b) => b.updated_at - a.updated_at)
      .map(build);
    return { ...toChatItem(conversation), children: linkedChildren };
  };

  return conversations
    .filter((conversation) => !childIds.has(conversation.id))
    .map(build);
}

function normalizeBlocks(message: ChatMessage): ChatContentBlock[] {
  if (typeof message.content === "string") {
    return [{ type: "text", text: message.content }];
  }
  return message.content;
}

function chatMessageMetadataRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function toUiMessage(message: ChatMessage, profile?: ModelProfile | null): ChatUiMessage {
  const isUser = message.role === "user";
  const metadata = message.metadata ?? {};
  const thinking = metadata.thinking as Record<string, unknown> | undefined;
  const timing = metadata.timing as Record<string, unknown> | undefined;
  const pendingApproval = metadata.pending_approval;
  const pendingAuthorityApproval = chatMessageMetadataRecord(metadata.pendingAuthorityApproval ?? metadata.pending_authority_approval);
  const authorityFollowup = chatMessageMetadataRecord(metadata.authority_followup ?? metadata.authorityFollowup);
  const chatDisplay = chatMessageMetadataRecord(metadata.chat_display ?? metadata.chatDisplay);
  const promptUsage = metadata.prompt_usage && typeof metadata.prompt_usage === "object" && !Array.isArray(metadata.prompt_usage)
    ? metadata.prompt_usage as NonNullable<ChatUiMessage["metadata"]>["promptUsage"]
    : undefined;
  const attachedToolCount = Number(metadata.attached_tool_count ?? 0);
  const thinkingDuration = String(timing?.thinking_duration_label ?? "")
    || boundedDurationLabel(timing?.thinking_started_at, timing?.completed_at);
  const displayMetadata = {
    ...(authorityFollowup ? { authorityFollowup } : {}),
    ...(chatDisplay ? { chatDisplay } : {}),
  };
  const explicitMentions = normalizeComposerMentionMetadata(metadata.mentions);
  const fallbackMentions = explicitMentions.length === 0 && Array.isArray(metadata.dropped_widgets)
    ? composerMentionMetadataFromWidgets(metadata.dropped_widgets as DroppedWidget[])
    : [];
  const mentions = explicitMentions.length > 0 ? explicitMentions : fallbackMentions;
  const userMetadata = Object.keys(displayMetadata).length > 0 || mentions.length > 0
    ? { ...displayMetadata, ...(mentions.length > 0 ? { mentions } : {}) }
    : undefined;
  return {
    id: message.id,
    conversationId: message.conversation_id,
    createdAt: message.created_at,
    role: isUser ? "user" : "agent",
    content: normalizeBlocks(message),
    rawText: messageToText(message),
    widget: message.widget,
    events: message.events ?? [],
    toolLogs: message.tool_logs ?? [],
    metadata: isUser
      ? userMetadata
      : {
          executionTime: formatRelativeTime(message.created_at),
          modelName: profile?.display_name ?? String(message.model ?? ""),
          thinkingLabel: String(thinking?.state ?? ""),
          thinkingDuration,
          thinkingTranscript: String(thinking?.transcript ?? ""),
          interrupted: metadata.interrupted === true || message.finish_reason === "interrupted",
          interruptionReason: String(metadata.interruption_reason ?? ""),
          attachedToolCount,
          pendingApproval: pendingApproval && typeof pendingApproval === "object" && !Array.isArray(pendingApproval)
            ? pendingApproval as Record<string, unknown>
            : undefined,
          pendingAuthorityApproval,
          ...displayMetadata,
          promptUsage,
        },
  };
}

function optimisticUserMessage(
  conversationId: string,
  text: string,
  metadata?: Record<string, unknown>,
): ChatMessage {
  return {
    id: `optimistic-${Date.now()}`,
    role: "user",
    content: [{ type: "text", text }],
    raw_text: text,
    created_at: Date.now(),
    conversation_id: conversationId,
    parent_id: null,
    children_ids: [],
    sequence_number: 0,
    finish_reason: null,
    usage: null,
    widget: null,
    metadata,
  };
}

function optimisticAssistantMessage(conversationId: string, model: string): ChatMessage {
  return {
    id: `optimistic-assistant-${Date.now()}`,
    role: "assistant",
    content: [{ type: "text", text: "" }],
    raw_text: "",
    created_at: Date.now(),
    conversation_id: conversationId,
    parent_id: null,
    children_ids: [],
    sequence_number: 0,
    finish_reason: null,
    usage: null,
    widget: null,
    metadata: { model, thinking: { state: "streaming" }, attached_tool_count: 0 },
    events: [],
    tool_logs: [],
    model,
  };
}

function mergeChatActivityEvents(base: ChatActivityEvent[] | null | undefined, extra: ChatActivityEvent[] | null | undefined): ChatActivityEvent[] {
  let merged = [...(base ?? [])];
  for (const event of extra ?? []) {
    merged = upsertStreamActivityEvent(merged, event);
  }
  return merged;
}

function mergeStreamingFinalMessage(existing: ChatMessage | undefined, incoming: ChatMessage): ChatMessage {
  return {
    ...incoming,
    events: mergeChatActivityEvents(incoming.events, existing?.events),
    tool_logs: incoming.tool_logs ?? existing?.tool_logs ?? null,
  };
}

function previewFromAction(action: SidebarAction, title: string, data: unknown): ToolPreviewItem {
  const content = typeof data === "string" ? data : JSON.stringify(data, null, 2);
  return {
    id: `sidebar-${action.id}-${Date.now()}`,
    toolStepId: action.id,
    timestamp: Date.now(),
    data: {
      type: "file",
      filename: `${title}.json`,
      size: "sidebar action",
      content,
    },
  };
}

function previewLabel(preview: ToolPreviewItem | undefined): string {
  if (!preview) return "memo.md";
  const data = preview.data;
  if (data.type === "web") return data.title || data.url || "Web preview";
  if (data.type === "code") return data.filename || "Code preview";
  if (data.type === "file") return data.filename || "File preview";
  return data.alt || "Image preview";
}

function CanvasPeek({
  previews,
  memo,
  activePreviewId,
  onOpen,
}: {
  previews: ToolPreviewItem[];
  memo: string;
  activePreviewId: string | null;
  onOpen: () => void;
}) {
  const items = buildToolPreviewDisplayItems(previews, memo, activePreviewId);
  if (items.length === 0) return null;

  const latest = items[0];
  const count = items.length;
  const isMemo = latest.id === "__memo__";
  const subLabel = isMemo ? "Canvas · memo" : "Canvas · tool activity";
  return (
    <button
      type="button"
      onClick={onOpen}
      className="mx-auto mb-2 flex w-[min(620px,calc(100%_-_40px))] items-center justify-between gap-3 rounded-xl border border-zinc-800/90 bg-zinc-950/85 px-3 py-2 text-left shadow-[0_14px_38px_rgba(0,0,0,0.24)] transition-colors hover:border-zinc-700 hover:bg-zinc-900/90"
      title="Canvas を開く"
    >
      <span className="flex min-w-0 items-center gap-3">
        <span className="h-8 w-8 flex-shrink-0 rounded-lg border border-zinc-800 bg-zinc-900/80" />
        <span className="min-w-0">
          <span className="block truncate text-[12px] font-medium text-zinc-300">
            {previewLabel(latest)}
          </span>
          <span className="block truncate text-[10px] text-zinc-600">{subLabel}</span>
        </span>
      </span>
      <span className="flex-shrink-0 rounded-full border border-zinc-800 bg-zinc-900 px-2 py-0.5 text-[10px] text-zinc-500">
        {count}
      </span>
    </button>
  );
}

function approvalPayloadPreview(payload: Record<string, unknown>): string {
  try {
    return JSON.stringify(payload, null, 2);
  } catch {
    return String(payload);
  }
}

function normalizedPreviewUrl(value: string): string {
  try {
    const url = new URL(value);
    url.hash = "";
    return url.href;
  } catch {
    return value.trim();
  }
}

function canvasPreviewIdentity(preview: ToolPreviewItem): string {
  const data = preview.data;
  if (data.type === "web") return `web:${normalizedPreviewUrl(data.url)}`;
  if (data.type === "image") return `image:${data.path || data.url || data.alt}`;
  if (data.type === "file") return `file:${data.path || data.url || `${data.filename}:${data.content ?? ""}`}`;
  return `code:${data.filename}:${data.diff ?? data.content ?? ""}`;
}

function runtimeApprovalRuntimeContent(approval: RuntimeApproval, token?: string): string {
  const payload = approvalPayloadPreview({
    ...approval.payload,
    ...(token ? { approval_token: token } : {}),
  });
  return [
    "The user approved the pending server-side tool operation.",
    "Continue by calling the exact pending tool once with the approved arguments below.",
    "Do not ask the user for the same approval again unless the tool returns a new approval_request_id.",
    `Tool: ${approval.toolName}`,
    `Operation: ${approval.operation}`,
    `Approval request id: ${approval.requestId}`,
    "Approved arguments JSON:",
    payload,
  ].join("\n");
}

type PendingCommandApproval = {
  requestId: string;
  invocationId: string;
  commandRef: string;
  command: ComposerCommandItem;
  args: Record<string, unknown>;
  conversationId: string | null;
  mode: ComposerCommandMode;
  approvalKind: "authority" | "coding";
  authorityRequestId?: string;
  authorityToken?: string;
  codingToken?: string;
};

/**
 * Client-only correlation for one Host-owned interactive effect.
 *
 * No effect id, prepared plan, scope, grant, token, or command arguments are
 * retained here. The Host adapter owns those values and only accepts this
 * invocation id for status, cancel, and the single resume call.
 */
type PendingHighRiskCommand = {
  requestId: string;
  invocationId: string;
  commandLabel: string;
};

const HIGH_RISK_TERMINAL_STATES = new Set([
  "succeeded",
  "failed",
  "stale",
  "ambiguous",
  "cancelled",
]);

function commandApprovalViewModel(
  pending: PendingCommandApproval,
): ApprovalViewModel {
  return {
    id: pending.requestId,
    source: pending.approvalKind === "authority" ? "authority" : "coding",
    title: `「${pending.command.label}」を実行`,
    consequence: `${pending.command.label} はローカル環境を変更する可能性があります。`,
    reason: "選択したコマンドを続行するために明示的な許可が必要です。",
    target: pending.commandRef,
    riskExplanation: "データの変更・送信を伴う可能性があります。対象と影響を確認してください。",
    scope: "この1回のコマンド実行のみ",
    persistence: "承認トークンは再利用できません。",
    auditText: "判断、コマンド、引数ハッシュはローカルの監査記録に残ります。",
    technicalDetails: {
      request_id: pending.requestId,
      invocation_id: pending.invocationId,
      command_ref: pending.commandRef,
      approved_arguments: pending.args,
      cwd: "current workspace",
      impact: pending.command.description || pending.command.label,
    },
    status: "pending",
  };
}

function staleRuntimeApprovalTitle(approval: StaleRuntimeApproval): string {
  const label = approval.operation || approval.toolName || "tool";
  return `${label} は再実行が必要です`;
}

function browserApprovalSettlementKey(approval: BrowserApproval): string {
  const requestId = approval.requestId?.trim();
  if (requestId) return `request:${requestId}`;
  return [
    "local",
    approval.toolCallId ?? "",
    approval.toolName,
    approval.action,
    JSON.stringify(approval.payload),
  ].join(":");
}

function approvalStaleUiMessage(error: unknown): string | null {
  const message = error instanceof Error ? error.message : String(error ?? "");
  const looksStale = /\bHTTP\s+(403|404|409)\b/i.test(message)
    || /approval.*(expired|not found|not pending|already|denied)/i.test(message)
    || /(expired|stale).*approval/i.test(message)
    || /承認.*(期限|期限切れ|拒否|処理済み|見つかりません)/.test(message);
  return looksStale
    ? "この承認リクエストは期限切れか、すでに処理済みです。新しい承認カードが届くまで操作できません。"
    : null;
}

function hasAgentServiceProfile(catalog: UICatalog | null, profileId: string): boolean {
  const profiles = catalog?.agent_service?.profiles ?? [];
  return profiles.some((profile) => String(profile.profile_id ?? profile.id ?? "") === profileId);
}

function hasOperationsProfile(catalog: UICatalog | null): boolean {
  return hasAgentServiceProfile(catalog, "defaultspack.operations_company");
}

function hasMimoCodingProfile(catalog: UICatalog | null): boolean {
  return hasAgentServiceProfile(catalog, "defaultspack.mimo_coding_company");
}

function isOperationsConversation(conversation: Conversation | null): boolean {
  if (!conversation) return false;
  return (
    conversation.conversation_kind === "operations_company"
    || conversation.metadata?.profile_id === "defaultspack.operations_company"
    || conversation.tags?.includes("operations-company")
  );
}

function isMimoCodingConversation(conversation: Conversation | null): boolean {
  if (!conversation) return false;
  return (
    conversation.conversation_kind === "mimo_coding_company"
    || conversation.metadata?.profile_id === "defaultspack.mimo_coding_company"
    || conversation.tags?.includes("mimo-coding-company")
  );
}

function settingList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => String(item).trim()).filter(Boolean);
  }
  if (typeof value === "string") {
    return value.split(/\r?\n|,/).map((item) => item.trim()).filter(Boolean);
  }
  return [];
}

export const MIMO_CODING_DEFAULT_MODEL = "xiaomi-token-plan-sgp/mimo-v2.5-pro";
export const MIMO_CODING_DEFAULT_VISION_MODEL = "xiaomi-token-plan-sgp/mimo-v2-omni";
export const MIMO_CODING_DEFAULT_FAST_MODEL = "xiaomi-token-plan-sgp/mimo-v2-flash";

const MIMO_CODING_EXPIRED_MODELS = new Set([
  "opencode-go/mimo-v2.5",
]);

const MIMO_CODING_BACKEND_COMPATIBLE_MODELS = new Set([
  MIMO_CODING_DEFAULT_MODEL,
  "xiaomi-token-plan-sgp/mimo-v2.5",
  "xiaomi-token-plan-sgp/mimo-v2-pro",
  MIMO_CODING_DEFAULT_VISION_MODEL,
  MIMO_CODING_DEFAULT_FAST_MODEL,
  "gitlawb-opengateway/mimo-v2.5-pro",
  "gitlawb-opengateway/mimo-v2.5",
  "gitlawb-opengateway/mimo-v2-pro",
  "gitlawb-opengateway/mimo-v2-omni",
  "gitlawb-opengateway/mimo-v2-flash",
  "groq/openai/gpt-oss-120b",
  "cerebras/gpt-oss-120b",
  "stub/default",
]);

function mimoCodingCandidateModels(settingsAllowlist: string[], manifestAllowlist: string[]): string[] {
  const sourceAllowlist = settingsAllowlist.length ? settingsAllowlist : manifestAllowlist;
  return sourceAllowlist.filter((item) => (
    MIMO_CODING_BACKEND_COMPATIBLE_MODELS.has(item)
    && !MIMO_CODING_EXPIRED_MODELS.has(item)
  ));
}

export function resolveMimoCodingModel(
  preferredModel: string,
  settingsAllowlist: string[],
  manifestAllowlist: string[],
): string {
  const candidates = mimoCodingCandidateModels(settingsAllowlist, manifestAllowlist);
  if (candidates.includes(preferredModel)) return preferredModel;
  if (candidates.includes(MIMO_CODING_DEFAULT_MODEL)) return MIMO_CODING_DEFAULT_MODEL;
  if (candidates.includes("stub/default")) return "stub/default";
  return candidates[0] ?? MIMO_CODING_DEFAULT_MODEL;
}

export function resolveMimoVisionModel(settingsAllowlist: string[], manifestAllowlist: string[]): string {
  const candidates = mimoCodingCandidateModels(settingsAllowlist, manifestAllowlist);
  const visionPreferred = candidates.find((item) => /omni|vision|vl/i.test(item));
  if (visionPreferred) return visionPreferred;
  if (candidates.includes(MIMO_CODING_DEFAULT_VISION_MODEL)) return MIMO_CODING_DEFAULT_VISION_MODEL;
  return MIMO_CODING_DEFAULT_VISION_MODEL;
}

export function resolveMimoFastModel(settingsAllowlist: string[], manifestAllowlist: string[]): string {
  const candidates = mimoCodingCandidateModels(settingsAllowlist, manifestAllowlist);
  const fastPreferred = candidates.find((item) => /flash|mini/i.test(item));
  if (fastPreferred) return fastPreferred;
  if (candidates.includes(MIMO_CODING_DEFAULT_FAST_MODEL)) return MIMO_CODING_DEFAULT_FAST_MODEL;
  return MIMO_CODING_DEFAULT_FAST_MODEL;
}

function settingNumber(value: unknown, fallback: number): number {
  const numeric = Number(value ?? fallback);
  return Number.isFinite(numeric) ? numeric : fallback;
}

function isAbortError(errorValue: unknown): boolean {
  return Boolean(
    errorValue
    && typeof errorValue === "object"
    && "name" in errorValue
    && String((errorValue as { name?: unknown }).name) === "AbortError",
  );
}

function isCancelledStreamError(errorValue: unknown): boolean {
  if (isAbortError(errorValue)) return true;
  const message = errorValue instanceof Error ? errorValue.message : String(errorValue ?? "");
  return message.trim().toLowerCase() === "cancelled";
}

function isLikelyTransportFailure(errorValue: unknown): boolean {
  if (errorValue instanceof ChatStreamInterruptedError) return true;
  const name = errorValue && typeof errorValue === "object" && "name" in errorValue
    ? String((errorValue as { name?: unknown }).name ?? "")
    : "";
  const message = errorValue instanceof Error ? errorValue.message : String(errorValue ?? "");
  return name === "TypeError" || /network|fetch|connection|timeout|timed out|load failed/i.test(message);
}

function isActivityStreamEvent(event: ChatStreamEvent): event is ChatToolStreamEvent {
  return (
    event.type === "status"
    || event.type === "tool_call"
    || event.type === "tool_call_started"
    || event.type === "tool_call_delta"
    || event.type === "tool_call_completed"
    || event.type === "tool_result"
    || event.type === "browser_state_invalidated"
    || event.type === "browser_state_snapshot"
    || event.type === "browser_dom_snapshot"
    || event.type === "browser_screenshot"
    || event.type === "approval_requested"
    || event.type === "ai_retry_scheduled"
    || event.type === "task_failed"
  );
}

function isConversationSteerItem(value: unknown): value is ConversationSteerItem {
  return Boolean(
    value
    && typeof value === "object"
    && "id" in value
    && "prompt" in value
  );
}

function activeComposerSteerItems(items: ConversationSteerItem[], isRunning: boolean): ConversationSteerItem[] {
  return items
    .filter((item) => item.visible !== false && String(item.prompt ?? "").trim())
    .filter((item) => {
      const status = String(item.status || "").toLowerCase();
      return status === "queued" || status === "sending" || (isRunning && status === "injected");
    })
    .slice(-3)
    .reverse();
}

function profileKey(profile: ModelProfile | null | undefined, fallback: string): string {
  return profile?.profile_id || profile?.qualified_model_id || fallback;
}

function getNewConversationPlaceholder(): string {
  return "指示を入力するか、/ でツール・コマンドを選択します...";
}

function findProfile(profiles: ModelProfile[], modelId: string): ModelProfile | null {
  return profiles.find((profile) => (
    profile.profile_id === modelId
    || profile.qualified_model_id === modelId
    || `${profile.provider_id}/${profile.model_id}` === modelId
  )) ?? null;
}

const LOCAL_MODEL_PROVIDER_IDS = new Set(["stub", "ollama", "lmstudio", "vllm", "llamacpp", "llama_cpp"]);
const API_KEY_PROVIDER_IDS = new Set([
  "anthropic",
  "deepseek",
  "glm",
  "google",
  "groq",
  "longcat",
  "mistral",
  "opencode-go",
  "opencode-zen",
  "openai",
  "openai_compatible",
  "openrouter",
  "perplexity",
  "together",
  "xai",
]);

function isConfiguredProfile(profile: ModelProfile): boolean {
  const availability = profile.availability ?? {};
  return Boolean(
    availability.configured
    || availability.active
    || availability.status === "configured"
    || availability.status === "active",
  );
}

export function profileNeedsApiKey(profile: ModelProfile | null | undefined): boolean {
  if (!profile) return false;
  const providerId = String(profile.provider_id ?? "").trim();
  if (!providerId || providerId === "rumi" || LOCAL_MODEL_PROVIDER_IDS.has(providerId)) return false;
  const availability = profile.availability ?? {};
  if (profile.local || availability.local || availability.offline || isConfiguredProfile(profile)) return false;
  return API_KEY_PROVIDER_IDS.has(providerId);
}

function isUserFacingModelProfile(profile: ModelProfile, preferredModel: string): boolean {
  const providerId = String(profile.provider_id ?? "").trim();
  const modelId = String(profile.model_id ?? "").trim();
  const profileId = profile.profile_id || profile.qualified_model_id || `${providerId}/${modelId}`;
  const availabilityStatus = String(profile.availability?.status ?? "").trim().toLowerCase();

  if (profileId === preferredModel) return true;
  if (!profileIsChatSelectable(profile)) return false;
  if (providerId === "rumi") return false;
  if (providerId === "stub") return modelId === "default";
  if (profile.local || profile.availability?.local || profile.availability?.offline || LOCAL_MODEL_PROVIDER_IDS.has(providerId)) return true;
  if (isConfiguredProfile(profile)) return true;
  if (availabilityStatus === "route_required") return true;
  return profileNeedsApiKey(profile);
}

function modelProfileSortKey(profile: ModelProfile): [number, number, string] {
  const modelId = String(profile.model_id ?? "").trim();
  const providerId = String(profile.provider_id ?? "").trim();
  const isDefault = profile.profile_id === "stub/default";
  const isLocal = Boolean(
    profile.local
    || profile.availability?.local
    || profile.availability?.offline
    || LOCAL_MODEL_PROVIDER_IDS.has(providerId),
  );
  const isConfigured = isConfiguredProfile(profile);
  const providerOrder = isDefault ? 0 : isLocal ? 1 : isConfigured ? 2 : 9;
  const modelOrder = modelId === "default" ? 0 : 20;
  return [
    providerOrder,
    modelOrder,
    profile.display_name || profile.profile_id,
  ];
}

export function userFacingModelProfiles(profiles: ModelProfile[], preferredModel: string): ModelProfile[] {
  const deduped = new Map<string, ModelProfile>();
  for (const profile of profiles) {
    if (!isUserFacingModelProfile(profile, preferredModel)) continue;
    const key = profile.profile_id || profile.qualified_model_id || `${profile.provider_id}/${profile.model_id}`;
    if (key) deduped.set(key, profile);
  }
  return [...deduped.values()].sort((a, b) => {
    const aKey = modelProfileSortKey(a);
    const bKey = modelProfileSortKey(b);
    return aKey[0] - bKey[0] || aKey[1] - bKey[1] || aKey[2].localeCompare(bKey[2]);
  });
}

function favoriteModelProfiles(rawFavorites: unknown, profiles: ModelProfile[], preferredModel: string): ModelProfile[] {
  const favoriteIds = Array.isArray(rawFavorites)
    ? rawFavorites.map((item) => String(item))
    : typeof rawFavorites === "string"
      ? rawFavorites.split(/\r?\n|,/).map((item) => item.trim())
      : [preferredModel];
  const uniqueIds = favoriteIds.filter(Boolean).filter((item, index, all) => all.indexOf(item) === index);
  const selected = uniqueIds
    .map((profileId) => findProfile(profiles, profileId) ?? {
      profile_id: profileId,
      qualified_model_id: profileId,
      display_name: profileId,
      max_context: -1,
      supports_thinking: false,
      thinking_levels: [],
    })
    .filter(Boolean);
  if (selected.length > 0) return selected;
  const fallback = findProfile(profiles, preferredModel);
  return fallback ? [fallback] : [];
}

function profileIdentity(profile: ModelProfile | null | undefined): string {
  if (!profile) return "";
  return profile.profile_id || profile.qualified_model_id || `${profile.provider_id ?? ""}/${profile.model_id ?? ""}`;
}

function profileDefaults(profile: ModelProfile | null | undefined): Record<string, unknown> {
  if (!profile) return {};
  const metadataDefaults = profile.metadata?.defaults;
  if (metadataDefaults && typeof metadataDefaults === "object" && !Array.isArray(metadataDefaults)) {
    return { ...(metadataDefaults as Record<string, unknown>), ...(profile.defaults ?? {}) };
  }
  return profile.defaults ?? {};
}

function profileIsChatSelectable(profile: ModelProfile | null | undefined): boolean {
  if (!profile) return false;
  const type = String(profile.type ?? "chat").toLowerCase();
  if (!type || type === "chat") return true;
  if (type !== "reasoning") return false;
  const defaults = profileDefaults(profile);
  const metadataCapabilities = profile.metadata?.capabilities;
  const capabilities = metadataCapabilities && typeof metadataCapabilities === "object" && !Array.isArray(metadataCapabilities)
    ? metadataCapabilities as Record<string, unknown>
    : {};
  return Boolean(
    defaults.chat
    || capabilities.chat
    || capabilities.text
    || profile.capability_tags?.includes("chat"),
  );
}

function profilePriceTier(profile: ModelProfile | null | undefined): string {
  if (!profile) return "";
  if (profile.cost_tier) return String(profile.cost_tier);
  const defaults = profileDefaults(profile);
  const pricing = profile.pricing ?? (profile.metadata?.pricing as Record<string, unknown> | undefined) ?? {};
  const explicit = String(
    pricing.tier
    ?? pricing.price_tier
    ?? defaults.price
    ?? defaults.price_tier
    ?? "",
  ).toLowerCase();
  if (explicit) return explicit;
  const modelId = String(profile.model_id ?? profile.profile_id ?? "").toLowerCase();
  if (defaults.large || defaults.heavy) return "high";
  if (defaults.fast || /(?:mini|nano|lite|flash|free|small|cheap)/.test(modelId)) return "low";
  return "";
}

function profileSupportsFast(profile: ModelProfile | null | undefined): boolean {
  if (!profile) return false;
  if (profile.supports_fast || profile.speed_tier === "fast") return true;
  const defaults = profileDefaults(profile);
  const tags = Array.isArray(profile.metadata?.tags) ? profile.metadata?.tags : [];
  const traits = Array.isArray(profile.metadata?.traits) ? profile.metadata?.traits : [];
  return Boolean(defaults.fast || tags.includes("fast") || traits.includes("fast_response"));
}

function profileSupportsThinking(profile: ModelProfile | null | undefined): boolean {
  return Boolean(profile?.supports_thinking && profile.thinking_levels?.length);
}

function bestConfiguredCandidate(candidates: ModelProfile[]): ModelProfile | null {
  if (candidates.length === 0) return null;
  return [...candidates].sort((a, b) => {
    const configured = Number(isConfiguredProfile(b)) - Number(isConfiguredProfile(a));
    if (configured) return configured;
    const local = Number(Boolean(b.local)) - Number(Boolean(a.local));
    if (local) return local;
    return (a.display_name || a.profile_id).localeCompare(b.display_name || b.profile_id);
  })[0] ?? null;
}

function fastCandidateForProfile(activeProfile: ModelProfile | null, profiles: ModelProfile[]): ModelProfile | null {
  if (!activeProfile) return null;
  if (profileSupportsFast(activeProfile)) return activeProfile;
  const providerId = String(activeProfile.provider_id ?? "");
  const providerDefaults = activeProfile.metadata?.default_model_for;
  const fastModel = providerDefaults && typeof providerDefaults === "object"
    ? String((providerDefaults as Record<string, unknown>).fast ?? "")
    : "";
  if (providerId && fastModel) {
    const providerFast = profiles.find((profile) => (
      profile.provider_id === providerId
      && (profile.model_id === fastModel || profile.qualified_model_id === `${providerId}/${fastModel}`)
      && profileSupportsFast(profile)
    ));
    if (providerFast) return providerFast;
  }
  const sameModelKey = String(activeProfile.same_model_across_providers_key ?? activeProfile.model_id ?? "").toLowerCase();
  const sameModelFast = profiles.filter((profile) => (
    profileIdentity(profile) !== profileIdentity(activeProfile)
    && profileSupportsFast(profile)
    && String(profile.same_model_across_providers_key ?? profile.model_id ?? "").toLowerCase() === sameModelKey
  ));
  if (sameModelFast.length) return bestConfiguredCandidate(sameModelFast);
  const providerFast = profiles.filter((profile) => (
    profile.provider_id === providerId
    && profileSupportsFast(profile)
    && profileIsChatSelectable(profile)
  ));
  return bestConfiguredCandidate(providerFast);
}

function priceCandidateForProfile(activeProfile: ModelProfile | null, profiles: ModelProfile[], tier: string): ModelProfile | null {
  const normalizedTier = tier === "high" ? "high" : "low";
  if (!activeProfile) return null;
  if (profilePriceTier(activeProfile) === normalizedTier || profileDefaults(activeProfile)[`price_${normalizedTier}`]) {
    return activeProfile;
  }
  const sameModelKey = String(activeProfile.same_model_across_providers_key ?? activeProfile.model_id ?? "").toLowerCase();
  if (!sameModelKey) return null;
  const sameModelCandidates = profiles.filter((profile) => (
    profileIdentity(profile) !== profileIdentity(activeProfile)
    && String(profile.same_model_across_providers_key ?? profile.model_id ?? "").toLowerCase() === sameModelKey
    && (profilePriceTier(profile) === normalizedTier || Boolean(profileDefaults(profile)[`price_${normalizedTier}`]))
  ));
  if (sameModelCandidates.length) return bestConfiguredCandidate(sameModelCandidates);
  return null;
}

function visionCandidateForProfile(activeProfile: ModelProfile | null, profiles: ModelProfile[]): ModelProfile | null {
  if (activeProfile?.supports_vision || activeProfile?.supports_image_input) return activeProfile;
  const providerId = String(activeProfile?.provider_id ?? "");
  const sameProvider = profiles.filter((profile) => (
    profile.provider_id === providerId
    && (profile.supports_vision || profile.supports_image_input)
    && profileIsChatSelectable(profile)
  ));
  if (sameProvider.length > 0) return bestConfiguredCandidate(sameProvider);
  const anyVision = profiles.filter((profile) => (
    (profile.supports_vision || profile.supports_image_input)
    && profileIsChatSelectable(profile)
  ));
  return bestConfiguredCandidate(anyVision);
}

function contextUsageFor(conversation: Conversation | null, profile: ModelProfile | null): ContextUsageInfo {
  const usedTokens = (conversation?.messages ?? []).reduce((total, message) => {
    const usage = message.usage ?? {};
    return total + Number(usage.total_tokens ?? usage.input_tokens ?? usage.prompt_tokens ?? 0);
  }, 0);
  const maxContext = Number(profile?.max_context_tokens ?? profile?.max_context ?? 0);
  if (maxContext < 0) {
    return { usedTokens, maxContext, ratio: 0, label: "∞" };
  }
  if (!maxContext) {
    return { usedTokens, maxContext: 0, ratio: 0, label: "?" };
  }
  const ratio = Math.min(1, Math.max(0, usedTokens / maxContext));
  return { usedTokens, maxContext, ratio, label: `${Math.round(ratio * 100)}%` };
}

function composerExtensionItems(items: SidebarItem[]): ComposerExtensionItem[] {
  return items
    .filter((item) => item.category === "tool" || item.category === "capability")
    .map((item) => ({
      id: item.id,
      label: item.label,
      category: item.category,
      description: item.description,
      tags: item.tags ?? [],
      ui: item.ui,
    }));
}

function chatIdFromLocation(): string | null {
  const params = new URLSearchParams(window.location.search);
  return params.get("chat") || null;
}

function isPendingInLocation(): boolean {
  const params = new URLSearchParams(window.location.search);
  return params.get("pending") === "1";
}

function replaceChatIdInUrl(conversationId: string | null, pending?: boolean) {
  const routeKind = workspaceKindForPathname(window.location.pathname);
  if (routeKind && routeKind !== "chat" && routeKind !== "coding") return;
  const url = new URL(window.location.href);
  url.pathname = window.location.pathname === "/coding" ? "/coding" : "/chat";
  if (conversationId) {
    url.searchParams.set("chat", conversationId);
  } else {
    url.searchParams.delete("chat");
  }
  if (pending === true) {
    url.searchParams.set("pending", "1");
  } else if (pending === false || !conversationId) {
    url.searchParams.delete("pending");
  }
  const next = `${url.pathname}${url.search}${url.hash}`;
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (next !== current) {
    window.history.pushState({ conversationId }, "", next);
  }
}

function pushWorkspaceRoute(kind: WorkspaceTabKind, conversationId: string | null = null) {
  const next = workspaceUrlForKind(kind, window.location.href, conversationId);
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (next !== current) {
    window.history.pushState({ workspaceKind: kind, conversationId }, "", next);
  }
}

function commandNames(command: ComposerCommandItem): string[] {
  return [command.id, command.name, ...(command.aliases ?? [])]
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean)
    .sort((left, right) => right.length - left.length);
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function matchCommandName(body: string, candidate: string): string | null {
  const directPattern = new RegExp(`^${escapeRegExp(candidate)}(?:\\s+|$)`, "i");
  const directMatch = body.match(directPattern);
  if (directMatch) return directMatch[0].trimEnd();

  const candidateParts = candidate.split(/[\s_-]+/).filter(Boolean);
  if (candidateParts.length < 2) return null;
  const flexiblePattern = new RegExp(`^${candidateParts.map(escapeRegExp).join("[\\s_-]+")}(?:\\s+|$)`, "i");
  const flexibleMatch = body.match(flexiblePattern);
  return flexibleMatch ? flexibleMatch[0].trimEnd() : null;
}

function normalizeCommandText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function parseCommandRest(rest: string, specs: ComposerCommandItem["args"] = []): Record<string, unknown> {
  if (!rest) return {};
  const args: Record<string, unknown> = {};
  let remaining = rest;

  for (const spec of specs) {
    const name = spec.name.trim();
    if (!name) continue;
    const optionPattern = new RegExp(`(^|\\s)${escapeRegExp(name)}=([^\\s]+)`, "gi");
    remaining = remaining.replace(optionPattern, (_match, prefix: string, value: string) => {
      args[name] = value;
      return prefix ? " " : "";
    });
    const slashFlagPattern = new RegExp(`(^|\\s)/${escapeRegExp(name)}(?=\\s|$)`, "gi");
    remaining = remaining.replace(slashFlagPattern, (_match, prefix: string) => {
      args[name] = true;
      return prefix ? " " : "";
    });
  }

  const greedySpec = specs.find((spec) => spec.greedy === true);
  const remainder = normalizeCommandText(remaining);
  if (greedySpec) {
    if (remainder) args[greedySpec.name] = remainder;
    return args;
  }

  const positionalSpecs = specs.filter((spec) => args[spec.name] === undefined);
  if (positionalSpecs.length === 1 && remainder) {
    args[positionalSpecs[0].name] = remainder;
  } else if (positionalSpecs.length > 1 && remainder) {
    const tokens = remainder.split(/\s+/);
    positionalSpecs.forEach((spec, index) => {
      if (index === positionalSpecs.length - 1) {
        const trailing = tokens.slice(index).join(" ");
        if (trailing) args[spec.name] = trailing;
      } else if (tokens[index]) {
        args[spec.name] = tokens[index];
      }
    });
  }

  return args;
}

type ParsedSlashCommandInput = {
  command: ComposerCommandItem;
  args: Record<string, unknown>;
  raw: string;
};

export function parseSlashCommandInput(
  input: string,
  commands: ComposerCommandItem[],
  options: { enabled?: boolean } = {},
): ParsedSlashCommandInput | null {
  if (options.enabled === false) return null;
  const trimmed = input.trim();
  if (!trimmed.startsWith("/") || trimmed.startsWith("//")) return null;
  const body = trimmed.slice(1).trim();
  if (!body) return null;
  const normalizedBody = body.toLowerCase();

  let matchedCommand: ComposerCommandItem | null = null;
  let matchedName = "";
  for (const item of commands) {
    const candidate = commandNames(item)
      .map((name) => matchCommandName(normalizedBody, name))
      .find((name): name is string => Boolean(name));
    if (!candidate || candidate.length <= matchedName.length) continue;
    matchedCommand = item;
    matchedName = candidate;
  }
  if (!matchedCommand) return null;

  const rest = body.slice(matchedName.length).trim();
  const args = parseCommandRest(rest, matchedCommand.args ?? []);
  return { command: matchedCommand, args, raw: trimmed };
}

export function parseCommandBoolean(value: unknown, fallback: boolean): boolean {
  if (value === undefined || value === null || value === "") return fallback;
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (!normalized) return fallback;
    if (["false", "0", "off", "no", "n", "disable", "disabled"].includes(normalized)) return false;
    if (["true", "1", "on", "yes", "y", "enable", "enabled"].includes(normalized)) return true;
  }
  return Boolean(value);
}

export function frontendCommandArgs(
  parsedArgs: Record<string, unknown>,
  backendArgs: unknown,
): Record<string, unknown> {
  return isRecord(backendArgs) ? { ...backendArgs } : parsedArgs;
}

export function resolvedFrontendCommandArgs(
  command: ComposerCommandItem,
  parsedArgs: Record<string, unknown>,
  backendArgs: unknown,
): Record<string, unknown> {
  return command.execution.type === "frontend"
    ? parsedArgs
    : frontendCommandArgs(parsedArgs, backendArgs);
}

type UltraYoloModeState = {
  yoloMode: boolean;
  ultraYoloMode: boolean;
  restoreYoloMode: boolean;
};

export function resolveUltraYoloModeState(
  state: UltraYoloModeState,
  enabled: boolean,
): UltraYoloModeState {
  void state;
  return {
    // `/yolo` is the Full Access switch.  Keep the older agent-approval bit
    // separate so toggling Full Access off always returns to Ask.
    yoloMode: false,
    ultraYoloMode: enabled,
    restoreYoloMode: false,
  };
}

export function keepSelectedToolsAfterSend(settingsValues: Record<string, Record<string, unknown>>): boolean {
  return parseCommandBoolean(settingsValues.tools?.keep_selected_tools_after_send, false);
}

function commandSearchText(command: ComposerCommandItem): string {
  return [
    command.id,
    command.name,
    ...(command.aliases ?? []),
    command.label,
    command.description ?? "",
  ].join(" ").toLowerCase();
}

function isModelCommand(command: ComposerCommandItem | undefined): boolean {
  if (!command) return false;
  if (command.protocol_presentation) {
    return command.protocol_presentation.input.kind === "search_select"
      && command.protocol_presentation.input.datasource_ref === "tobkiri:model_catalog";
  }
  return [command.id, command.name, ...(command.aliases ?? [])]
    .map((value) => String(value ?? "").toLowerCase())
    .includes("model");
}

function protocolCommandStateRef(command: ComposerCommandItem): string {
  if (command.protocol_presentation?.input.kind !== "toggle") return "";
  return String(command.protocol_presentation.input.state_ref ?? "").trim();
}

function settingsStateRefValue(
  stateRef: string,
  settingsValues: Record<string, Record<string, unknown>>,
): boolean | undefined {
  if (!stateRef.startsWith("defaultspack:")) return undefined;
  const path = stateRef.slice("defaultspack:".length);
  const separator = path.indexOf(".");
  if (separator <= 0) return undefined;
  const section = path.slice(0, separator);
  const field = path.slice(separator + 1);
  const value = settingsValues[section]?.[field];
  return typeof value === "boolean" ? value : undefined;
}

function modelCandidateProfileId(candidate: ModelCommandCandidate): string {
  return String(candidate.profile_id ?? candidate.qualified_model_id ?? "").trim();
}

function selectedModelProfileId(value: ComposerCommandExecuteResult["selected_model"]): string {
  if (typeof value === "string") return value.trim();
  if (value && typeof value === "object") return modelCandidateProfileId(value);
  return "";
}

function modelCommandInputQuery(value: string): string | null {
  const match = value.trim().match(/^\/models?(?:\s+([\s\S]*))?$/i);
  if (!match) return null;
  return String(match[1] ?? "").trim();
}

function ChatApp() {
  const [catalog, setCatalog] = useState<UICatalog | null>(null);
  const [modelProfiles, setModelProfiles] = useState<ModelProfile[]>([]);
  const [settingsSections, setSettingsSections] = useState<SettingsSection[]>([]);
  const [settingsValues, setSettingsValues] = useState<Record<string, Record<string, unknown>>>({});
  const settingsValuesRef = useRef(settingsValues);
  const pinnedPlacementSaveRevisionRef = useRef(0);
  const settingsSaveRevisionRef = useRef(0);
  const settingsSaveQueueRef = useRef<Promise<unknown>>(Promise.resolve());
  const settingsDirtyKeysRef = useRef<string[]>([]);
  const refreshCatalogSequenceRef = useRef(0);
  const [settingsSaveState, setSettingsSaveState] = useState<SettingsSaveState>({ status: "idle", dirtyKeys: [] });
  const [settingsLoadState, setSettingsLoadState] = useState<SettingsLoadState>({ status: "loading" });
  const [modelProfilesLoadState, setModelProfilesLoadState] = useState<SettingsLoadState>({ status: "loading" });
  useEffect(() => {
    settingsValuesRef.current = settingsValues;
  }, [settingsValues]);
  const [desktopSystemInfo, setDesktopSystemInfo] = useState<DesktopSystemInfo | null>(null);
  const [commandCatalog, setCommandCatalog] = useState<ComposerCommandItem[]>([]);
  const [usesResolvedCommandProtocol, setUsesResolvedCommandProtocol] = useState(false);
  const [commandProtocolInfo, setCommandProtocolInfo] = useState<ResolvedCommandCatalog | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const widgetContext = useMemo(
    () => createWidgetConversationContext(activeConversationId),
    [activeConversationId],
  );
  const [activeConversation, setActiveConversation] = useState<Conversation | null>(null);
  const [activeHistoryCompanyId, setActiveHistoryCompanyId] = useState<string | null>(null);
  const [input, setInput] = useLocalStorage("rumi-input", "");
  const [customHomeTitle, setCustomHomeTitle] = useLocalStorage(
    "rumi-home-title",
    DEFAULT_COMPOSER_HOME_TITLE,
  );
  const [structuredComposerValues, setStructuredComposerValues] = useState<Record<string, string>>({});
  const [composerCandidateMenu, setComposerCandidateMenu] = useState<ComposerCandidateMenuState>(null);
  const [isSpotlightOpen, setIsSpotlightOpen] = useState(false);
  const [spotlightQuery, setSpotlightQuery] = useState("");
  const [spotlightFilter, setSpotlightFilter] = useState<SpotlightFilter>("all");
  const [spotlightResults, setSpotlightResults] = useState<ConversationSearchResult[]>([]);
  const [spotlightSelectedIndex, setSpotlightSelectedIndex] = useState(0);
  const [spotlightLoading, setSpotlightLoading] = useState(false);
  const [modelPickerRequestId, setModelPickerRequestId] = useState(0);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [requestedSettingsSectionId, setRequestedSettingsSectionId] = useState<string | null>(null);
  const [isGenerating, setIsGenerating] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [startupError, setStartupError] = useState<string | null>(null);
  const [startupSteps, setStartupSteps] = useState<TobkiriLoadingStep[]>([
    { id: "backend", label: "バックエンドとの接続を確認しています…", status: "loading" },
    { id: "capabilities", label: "ツール・スキル・@候補を読み込みます", status: "pending" },
    { id: "commands", label: "/コマンド・モデル・設定を準備します", status: "pending" },
    { id: "conversations", label: "会話とワークスペースを復元します", status: "pending" },
  ]);
  const [error, setError] = useState<string | null>(null);
  const [transientAlert, setTransientAlert] = useState<TransientAlertItem | null>(null);
  const transientAlertSequenceRef = useRef(0);
  const composerAlertAnchorRef = useRef<HTMLDivElement>(null);
  const [retryableSubmission, setRetryableSubmission] = useState<RetryableSubmission | null>(null);
  const [shareDialogOpen, setShareDialogOpen] = useState(false);
  const [shareBusy, setShareBusy] = useState(false);
  const [shareCreatedUrl, setShareCreatedUrl] = useState<string | null>(null);
  const [shareCreatedToken, setShareCreatedToken] = useState<string | null>(null);
  const [shareExpiryHours, setShareExpiryHours] = useState("24");
  const [shareRevoked, setShareRevoked] = useState(false);
  const [shareDialogError, setShareDialogError] = useState<string | null>(null);
  const [provenanceDismissedFor, setProvenanceDismissedFor] = useState<string | null>(null);
  const [showPreview, setShowPreview] = useLocalStorage("rumi-show-preview", false);
  const [showPromptUsageInMessages, setShowPromptUsageInMessages] = useLocalStorage("rumi-show-prompt-usage-in-messages", true);
  const [workspaceTabs, setWorkspaceTabs] = useState<WorkspaceTab[]>(() => initialWorkspaceTabsForPathname(window.location.pathname));
  const [activeWorkspaceTabId, setActiveWorkspaceTabId] = useState(() => initialActiveWorkspaceTabIdForPathname(window.location.pathname));
  const [isHistoryMinimized, setIsHistoryMinimized] = useLocalStorage("rumi-history-minimized", false);
  const [isNewChatLaunching, setIsNewChatLaunching] = useState(false);
  const [modelSteerStatus, setModelSteerStatus] = useState<ComposerSteerStatus | null>(null);
  const [modelSteerBusy, setModelSteerBusy] = useState(false);
  const [steerItems, setSteerItems] = useState<ConversationSteerItem[]>([]);
  const [previewMode, setPreviewMode] = useLocalStorage<ToolPreviewMode>("rumi-preview-mode", "auto");
  const [activityPreviewWidth, setActivityPreviewWidth] = useLocalStorage("rumi-activity-preview-width", 340);
  const [canvasMemo, setCanvasMemo] = useLocalStorage("rumi-canvas-memo", "");
  const [activePreviewId, setActivePreviewId] = useState<string | null>(null);
  const [previews, setPreviews] = useState<ToolPreviewItem[]>([]);
  const [settledRuntimeApprovalIds, setSettledRuntimeApprovalIds] = useState<string[]>([]);
  const [settledBrowserApprovalKeys, setSettledBrowserApprovalKeys] = useState<string[]>([]);
  const [pendingCommandApproval, setPendingCommandApproval] = useState<PendingCommandApproval | null>(null);
  const [pendingHighRiskCommand, setPendingHighRiskCommand] = useState<PendingHighRiskCommand | null>(null);
  const [commandProgressEvents, setCommandProgressEvents] = useState<Array<Record<string, unknown>>>([]);
  const [health, setHealth] = useState<{ status: string; pack: string; ts: string } | null>(null);
  const [backendConnectionState, setBackendConnectionState] = useState<BackendConnectionState>("online");
  const [backendConnectionNote, setBackendConnectionNote] = useState<string | null>(null);
  const [operationsStatus, setOperationsStatus] = useState<OperationsCompanyStatus | null>(null);
  const [operationsBusy, setOperationsBusy] = useState(false);
  const [mimoCodingStatus, setMimoCodingStatus] = useState<MimoCodingCompanyStatus | null>(null);
  const [mimoCodingBusy, setMimoCodingBusy] = useState(false);
  const [activeSidebarItemId, setActiveSidebarItemId] = useState<string | null>(null);
  const [sidebarSelectionTick, setSidebarSelectionTick] = useState(0);
  const [yoloMode, setYoloMode] = useLocalStorage("rumi-yolo-mode", false);
  const [ultraYoloMode, setUltraYoloMode] = useLocalStorage("rumi-ultra-yolo-mode", false);
  const [ultraYoloRestoreYoloMode, setUltraYoloRestoreYoloMode] = useLocalStorage("rumi-ultra-yolo-restore-yolo-mode", false);
  const [mode, setMode] = useLocalStorage<AppMode>("rumi-app-mode", "agent");
  const [codingContext, setCodingContext] = useState<CodingContext | null>(null);
  const [codingWorkspaces, setCodingWorkspaces] = useState<CodingWorkspaceRecord[]>([]);
  const [selectedCodingWorkspaceId, setSelectedCodingWorkspaceId] = useState<string | null>(null);
  const [pendingNewTaskContext, setPendingNewTaskContext] = useState<PendingNewTaskContext | null>(null);
  const [projects, setProjects] = useState<ProjectInfo[]>(() => loadProjects());
  const [codingDirectory, setCodingDirectory] = useState(".");
  const [attachedFiles, setAttachedFiles] = useState<AttachedFile[]>([]);
  const [isWorkspaceFileDragActive, setIsWorkspaceFileDragActive] = useState(false);
  const workspaceFileDragDepthRef = useRef(0);
  const [pendingMentionAttachmentPaths, setPendingMentionAttachmentPaths] = useState<string[]>([]);
  const [droppedWidgets, setDroppedWidgets] = useState<DroppedWidget[]>([]);
  const [composerEntityReferences, setComposerEntityReferences] = useState<ComposerEntityReference[]>([]);
  const [storedSelectedToolIds, setStoredSelectedToolIds] = useLocalStorage<string[]>("rumi-selected-tool-ids", []);
  const pendingStorageKey = "rumi-pending-chat-requests";
  const [pendingRequests, setPendingRequests] = useLocalStorage<Record<string, PendingChatRequest>>(pendingStorageKey, {});
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesScrollRef = useRef<HTMLDivElement>(null);
  const shouldFollowMessagesRef = useRef(true);
  const isUnloadingRef = useRef(false);
  const humanOperatorAutoOpenedPreviewRef = useRef<string | null>(null);
  const currentAbortControllerRef = useRef<AbortController | null>(null);
  const streamingConversationIdRef = useRef<string | null>(null);
  const activeRuntimeApprovalActionRef = useRef<string | null>(null);
  const activeBrowserApprovalActionRef = useRef<string | null>(null);
  const highRiskPrepareInFlightRef = useRef(false);
  const highRiskResumeStartedRef = useRef(new Set<string>());
  const highRiskCancelStartedRef = useRef(new Set<string>());
  const highRiskApprovalWindowOpenedRequestRef = useRef<string | null>(null);
  const lastHealthyAtRef = useRef<number | null>(null);
  const consecutiveHealthFailuresRef = useRef(0);
  const authorityApprovalWindowRequestRef = useRef<string | null>(null);
  const dismissedComposerMentionToolsRef = useRef<Map<string, string[]>>(
    new Map(),
  );
  const composerDraftGenerationRef = useRef(0);
  const mentionAttachmentTokenRef = useRef(0);
  const pendingMentionAttachmentRequestsRef = useRef<
    Map<string, PendingMentionAttachmentRequest>
  >(new Map());

  const syncPendingMentionAttachmentPaths = () => {
    setPendingMentionAttachmentPaths([
      ...pendingMentionAttachmentRequestsRef.current.keys(),
    ]);
  };

  const cancelPendingMentionAttachments = (path?: string) => {
    if (path) {
      pendingMentionAttachmentRequestsRef.current.delete(path);
    } else {
      composerDraftGenerationRef.current += 1;
      pendingMentionAttachmentRequestsRef.current.clear();
    }
    syncPendingMentionAttachmentPaths();
  };

  const semanticAttachmentPathsIncludingPending = (files: AttachedFile[]) => [
    ...new Set([
      ...semanticAttachmentPaths(files),
      ...pendingMentionAttachmentRequestsRef.current.keys(),
    ]),
  ];

  useEffect(() => {
    const refreshProjects = () => setProjects(loadProjects());
    window.addEventListener(PROJECTS_CHANGED_EVENT, refreshProjects);
    window.addEventListener("storage", refreshProjects);
    return () => {
      window.removeEventListener(PROJECTS_CHANGED_EVENT, refreshProjects);
      window.removeEventListener("storage", refreshProjects);
    };
  }, []);

  const allowManualRuntimeModeSelection = manualRuntimeModeSelectionEnabled(settingsValues);

  useEffect(() => {
    if (
      !allowManualRuntimeModeSelection
      && mode !== "agent"
      && window.location.pathname !== "/coding"
    ) {
      setMode("agent");
    }
  }, [allowManualRuntimeModeSelection, mode, setMode]);

  useEffect(() => {
    if (!shareDialogOpen) return;
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") setShareDialogOpen(false);
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [shareDialogOpen]);

  const rawSidebarItems: SidebarItem[] = catalog?.sidebar.items ?? [];
  const chatItems = buildChatItems(conversations);
  const recentSpotlightResults = useMemo(
    () => conversations
      .filter((conversation) => conversationMatchesSpotlightFilter(conversation, spotlightFilter))
      .slice(0, 10)
      .map(conversationToSearchResult),
    [conversations, spotlightFilter],
  );
  const visibleSpotlightResults = spotlightQuery.trim() ? spotlightResults : recentSpotlightResults;
  const activeModelId = activeConversation?.model ?? String(settingsValues.models?.preferred_model ?? "stub/default").trim();
  const activeProfile = findProfile(modelProfiles, activeModelId);
  const orderedMessages = useMemo(
    () => activeConversation ? orderConversationMessages(activeConversation.messages) : [],
    [activeConversation?.messages],
  );
  const conversationIntegrity = useMemo(
    () => activeConversation
      ? inspectConversationIntegrity(activeConversation.messages)
      : {
          collapsedCount: 0,
          duplicateIdCount: 0,
          duplicateSequenceCount: 0,
          duplicateKeys: [],
        },
    [activeConversation?.messages],
  );
  useEffect(() => {
    if (!activeConversationId || conversationIntegrity.collapsedCount === 0) return;
    void reportClientDiagnostic({
      source: "webapp",
      category: "conversation_integrity",
      level: "warning",
      message: "Frontend collapsed duplicate conversation messages before rendering.",
      fingerprint: `conversation-integrity:${activeConversationId}:${conversationIntegrity.duplicateKeys.join("|")}`,
      conversationId: activeConversationId,
      detail: conversationIntegrity,
    });
  }, [activeConversationId, conversationIntegrity]);
  const latestActiveMessage = activeConversation?.messages[activeConversation.messages.length - 1];
  const latestActiveMetadata = latestActiveMessage?.metadata && typeof latestActiveMessage.metadata === "object"
    ? latestActiveMessage.metadata as Record<string, unknown>
    : {};
  const latestActiveThinking = latestActiveMetadata.thinking && typeof latestActiveMetadata.thinking === "object"
    ? latestActiveMetadata.thinking as Record<string, unknown>
    : {};
  const latestActivePendingSignature = latestActiveMessage
    ? `${latestActiveMessage.id}:${latestActiveMessage.role}:${latestActiveMessage.finish_reason ?? ""}:${String(latestActiveThinking.state ?? "")}`
    : "";
  const messages = orderedMessages.map((message) => toUiMessage(message, activeProfile));
  const backendConnectionBanner = backendConnectionCopy(
    backendConnectionState,
    lastHealthyAtRef.current,
    backendConnectionNote,
  );
  const activeChatTitle = activeConversation?.title ?? "New Conversation";
  const activeWorkspaceTab = workspaceTabs.find((tab) => tab.id === activeWorkspaceTabId) ?? workspaceTabs[0] ?? null;
  const activeWorkspaceKind = activeWorkspaceTab?.kind ?? "chat";
  const isChatWorkspace = activeWorkspaceKind === "chat";
  const isCodingWorkspace = activeWorkspaceKind === "coding";
  const isSubagentWorkspace = activeWorkspaceKind === "subagents";
  const isCanvasWorkspace = activeWorkspaceKind === "canvas";
  const isDesktopsWorkspace = activeWorkspaceKind === "desktops";
  const isToolsWorkspace = activeWorkspaceKind === "tools";
  const isNewConversation = activeConversation === null || activeConversation.messages.length === 0;
  useEffect(() => {
    setWorkspaceTabs((current) => current.map((tab) => {
      if (tab.id !== activeWorkspaceTabId || tab.kind !== "chat") return tab;
      const nextTitle = activeConversationId ? activeChatTitle : "New Conversation";
      if (tab.conversationId === activeConversationId && tab.title === nextTitle) return tab;
      return {
        ...tab,
        conversationId: activeConversationId,
        title: nextTitle,
      };
    }));
  }, [activeChatTitle, activeConversationId, activeWorkspaceTabId]);
  const activePromptUsage = latestActiveMetadata.prompt_usage && typeof latestActiveMetadata.prompt_usage === "object" && !Array.isArray(latestActiveMetadata.prompt_usage)
    ? latestActiveMetadata.prompt_usage as PromptUsageSummary
    : null;
  const activePromptProfileId = String(activeConversation?.metadata?.profile_id ?? activePromptUsage?.profile_id ?? "").trim() || undefined;
  const placeholder = String(settingsValues.general?.composer_placeholder ?? "メッセージを入力...");
  const locale = normalizeLocale(settingsValues.general?.language);
  const keyboardButtonNavigation = parseCommandBoolean(settingsValues.general?.keyboard_button_navigation, true);
  const spotlightShortcut = String(settingsValues.general?.spotlight_shortcut ?? "Ctrl+K").trim() || "Ctrl+K";
  const spotlightShortcutEnabled = parseCommandBoolean(settingsValues.general?.spotlight_shortcut_enabled, true);
  const spotlightShortcutTextInput = parseCommandBoolean(settingsValues.general?.spotlight_shortcut_text_input, true);
  const spotlightShortcutLabel = spotlightShortcutEnabled ? shortcutLabel(spotlightShortcut) : "Off";
  const composerMode = mode as ComposerCommandMode;
  const templateAiInputMetadata = useMemo(
    () => selectTemplateAiInput(catalog, composerMode),
    [catalog, composerMode],
  );
  const composerInputMetadata = useMemo(
    () => selectTemplateComposerInput(catalog, composerMode, templateAiInputMetadata),
    [catalog, composerMode, templateAiInputMetadata],
  );
  const effectiveStructuredComposerValues = useMemo(() => {
    const fields = normalizeComposerFields(composerInputMetadata?.fields);
    return structuredComposerPayload(fields, {
      ...initialComposerFieldValues(fields),
      ...structuredComposerValues,
    });
  }, [composerInputMetadata?.fields, structuredComposerValues]);
  const slashCommandsEnabled = useMemo(
    () => templateFeatureFlagEnabled(composerInputMetadata, "slash_commands", true),
    [composerInputMetadata],
  );
  const templateToolPolicyMetadata = useMemo(
    () => selectTemplateToolPolicy(catalog, composerMode, templateAiInputMetadata),
    [catalog, composerMode, templateAiInputMetadata],
  );
  const activeTemplateToolPolicy = useMemo(
    () => templateToolPolicySettings(templateToolPolicyMetadata),
    [templateToolPolicyMetadata],
  );
  const templatePolicyReferencePayload = useMemo(
    () => templateToolPolicyReferencePayload(templateAiInputMetadata, templateToolPolicyMetadata),
    [templateAiInputMetadata, templateToolPolicyMetadata],
  );
  const templateAiInputParams = useMemo(
    () => templateAiInputParamsPayload(templateAiInputMetadata),
    [templateAiInputMetadata],
  );
  const disabledToolIds = settingList(settingsValues.tools?.disabled_tool_ids);
  const hiddenToolIds = settingList(settingsValues.tools?.hidden_tool_ids);
  const templateDisabledToolIds = useMemo(
    () => [...new Set([
      ...activeTemplateToolPolicy.defaultDisabledToolIds,
      ...activeTemplateToolPolicy.deniedToolIds,
    ])],
    [activeTemplateToolPolicy.defaultDisabledToolIds, activeTemplateToolPolicy.deniedToolIds],
  );
  const effectiveDisabledToolIds = useMemo(
    () => [...new Set([...disabledToolIds, ...templateDisabledToolIds])],
    [disabledToolIds, templateDisabledToolIds],
  );
  const disabledToolIdSet = useMemo(() => new Set(effectiveDisabledToolIds), [effectiveDisabledToolIds]);
  const hiddenToolIdSet = useMemo(() => new Set(hiddenToolIds), [hiddenToolIds]);
  const templateAllowedToolIdSet = useMemo(
    () => new Set(activeTemplateToolPolicy.allowedToolIds),
    [activeTemplateToolPolicy.allowedToolIds],
  );
  const templateHasToolAllowlist = activeTemplateToolPolicy.hasAllowedToolRestriction;
  const sidebarItems: SidebarItem[] = useMemo(
    () => rawSidebarItems.filter((item) => item.category !== "tool" || !hiddenToolIdSet.has(item.id)),
    [hiddenToolIdSet, rawSidebarItems],
  );
  const preferredModel = activeModelId;
  const modelSelectorSchema = useMemo(() => modelSelectorSchemaFromCatalog(catalog), [catalog]);
  const userFacingProfiles = userFacingModelProfiles(modelProfiles, preferredModel);
  const selectableModelProfiles = filterModelProfilesBySelector(
    userFacingProfiles,
    modelSelectorSchema,
    "composer",
  );
  const settingsModelProfiles = filterModelProfilesBySelector(
    userFacingProfiles,
    modelSelectorSchema,
    "settings",
  );
  const favoriteProfiles = favoriteModelProfiles(settingsValues.models?.favorite_profiles, selectableModelProfiles, preferredModel);
  const thinkingLevels = (settingsValues.models?.thinking_level_by_profile ?? {}) as Record<string, unknown>;
  const selectedThinkingLevel = String(
    thinkingLevels[profileKey(activeProfile, preferredModel)]
    ?? settingsValues.models?.thinking_level
    ?? activeProfile?.default_thinking_level
    ?? "medium",
  );
  const deepthinkEnabled = parseCommandBoolean(settingsValues.models?.deepthink_enabled, false);
  const commandStateRevisionsRef = useRef<Record<string, number>>({});
  const deepthinkMutationQueueRef = useRef<Promise<unknown>>(Promise.resolve());
  const deepthinkDesiredStateRef = useRef(deepthinkEnabled);
  const deepthinkPendingCountRef = useRef(0);
  const commandClientSequenceRef = useRef(0);
  useEffect(() => {
    if (deepthinkPendingCountRef.current === 0) {
      deepthinkDesiredStateRef.current = deepthinkEnabled;
    }
  }, [deepthinkEnabled]);
  const contextUsage = contextUsageFor(activeConversation, activeProfile);
  const composerExtensions = useMemo(
    () => composerExtensionItems(sidebarItems)
      .filter((item) => !disabledToolIdSet.has(item.id))
      .filter((item) => !templateHasToolAllowlist || templateAllowedToolIdSet.has(item.id)),
    [disabledToolIdSet, sidebarItems, templateAllowedToolIdSet, templateHasToolAllowlist],
  );
  const templateComposerWidgets = useMemo(
    () => templateComposerWidgetsForInput(catalog, templateAiInputMetadata, composerInputMetadata, composerExtensions),
    [catalog, templateAiInputMetadata, composerInputMetadata, composerExtensions],
  );
  const activeDroppedWidgets = useMemo(() => {
    const byId = new Map<string, DroppedWidget>();
    for (const widget of templateComposerWidgets) byId.set(widget.id, widget);
    for (const widget of droppedWidgets) byId.set(widget.id, widget);
    return Array.from(byId.values());
  }, [droppedWidgets, templateComposerWidgets]);
  const composerSkills = useMemo<ComposerSkillItem[]>(() => (
    (catalog?.skills ?? []).map((skill) => ({
      id: skill.id,
      label: skill.label ?? skill.id,
      description: skill.description,
      triggers: skill.triggers ?? [],
      appliesToTools: skill.applies_to_tools ?? [],
      aliases: skill.aliases ?? [],
      metadata: skill.metadata,
    }))
  ), [catalog?.skills]);
  const settingsAssistantSkill = useMemo<ComposerSkillItem>(() => (
    resolveSettingsAssistantSkill(composerSkills)
  ), [composerSkills]);
  const composerPosition = isNewConversation
    ? composerInputMetadata?.layout?.home?.position ?? "center"
    : composerInputMetadata?.layout?.conversation?.position ?? "bottom";
  const transientAlertPlacement = alertPlacementForComposerPosition(composerPosition);
  const composerHomeTitle = useMemo(
    () => resolveComposerHomeTitle(
      input,
      composerSkills,
      normalizeComposerHomeTitle(customHomeTitle),
    ),
    [composerSkills, customHomeTitle, input],
  );
  const selectedTools = useMemo(() => storedSelectedToolIds
    .map((toolId) => composerExtensions.find((tool) => tool.id === toolId))
    .filter((tool): tool is ComposerExtensionItem => Boolean(tool)), [composerExtensions, storedSelectedToolIds]);
  const selectedToolIds = useMemo(() => selectedTools.map((tool) => tool.id), [selectedTools]);
  const selectedToolIdSet = useMemo(() => new Set(selectedToolIds), [selectedToolIds]);
  const activeConversationToolPreferences = useMemo(
    () => parseConversationToolPreferences(activeConversation?.metadata),
    [activeConversation?.id, activeConversation?.metadata],
  );
  const toolSelectionController = useToolSelectionController({
    settingsValues,
    selectedToolIds,
    setSelectedToolIds: setStoredSelectedToolIds,
    conversationPreferences: activeConversationToolPreferences,
  });
  useEffect(() => {
    if (isGenerating) return;
    const reconciled = reconcileComposerSemanticDraft({
      droppedWidgets,
      selectedToolIds,
      text: input,
    });
    if (
      reconciled.droppedWidgets.length !== droppedWidgets.length
      || reconciled.droppedWidgets.some((widget, index) => widget !== droppedWidgets[index])
    ) {
      setDroppedWidgets(reconciled.droppedWidgets);
    }
    if (
      reconciled.selectedToolIds.length !== selectedToolIds.length
      || reconciled.selectedToolIds.some((toolId, index) => toolId !== selectedToolIds[index])
    ) {
      setStoredSelectedToolIds(reconciled.selectedToolIds);
    }
  }, [droppedWidgets, input, isGenerating, selectedToolIds, setStoredSelectedToolIds]);
  const pendingRequest = activeConversationId ? pendingRequests[activeConversationId] : null;
  const isConversationPending = Boolean(
    pendingRequest && Date.now() - pendingRequest.startedAt < PENDING_CHAT_REQUEST_TTL_MS,
  );
  const rawBrowserApproval = pendingBrowserApproval(messages);
  const rawAuthorityApproval = pendingAuthorityApproval(messages);
  const rawRuntimeApproval = pendingRuntimeApproval(messages);
  const settledRuntimeApprovalIdSet = useMemo(() => new Set(settledRuntimeApprovalIds), [settledRuntimeApprovalIds]);
  const settledBrowserApprovalKeySet = useMemo(() => new Set(settledBrowserApprovalKeys), [settledBrowserApprovalKeys]);
  const rawBrowserApprovalKey = rawBrowserApproval ? browserApprovalSettlementKey(rawBrowserApproval) : "";
  const browserApproval = rawBrowserApproval && !settledBrowserApprovalKeySet.has(rawBrowserApprovalKey)
    ? rawBrowserApproval
    : null;
  const authorityApproval = rawAuthorityApproval && !settledRuntimeApprovalIdSet.has(rawAuthorityApproval.requestId)
    ? rawAuthorityApproval
    : null;
  const runtimeApproval = rawRuntimeApproval && !settledRuntimeApprovalIdSet.has(rawRuntimeApproval.requestId)
    ? rawRuntimeApproval
    : null;
  const staleRuntimeApprovalNotice = !ultraYoloMode && !rawRuntimeApproval ? staleRuntimeApproval(messages) : null;
  const visibleBrowserApproval = !ultraYoloMode ? browserApproval : null;
  const latestAssistantFinal = useMemo(() => {
    if (isGenerating || isConversationPending) return null;
    for (const message of [...messages].reverse()) {
      if (message.role === "user") return null;
      if (message.role !== "agent") continue;
      const rawText = message.rawText.trim();
      if (!rawText) continue;
      if (rawText === AUTHORITY_WAITING_TEXT && pendingAuthorityApproval([message])) return null;
      const text = sanitizeAssistantAuthorityBoilerplate(rawText).trim();
      if (!text) continue;
      return {
        messageId: message.id,
        createdAt: message.createdAt ?? 0,
        text,
      };
    }
    return null;
  }, [isConversationPending, isGenerating, messages]);

  useEffect(() => {
    if (!latestAssistantFinal) return;
    publishAmbientFinalAnswer(latestAssistantFinal.text, activeConversationId, {
      messageId: latestAssistantFinal.messageId,
      messageCreatedAt: latestAssistantFinal.createdAt,
      updatedAt: latestAssistantFinal.createdAt || Date.now(),
    });
  }, [activeConversationId, latestAssistantFinal]);

  // The approval control is the single visible source of truth.  Full Access
  // must not also appear as a model/status chip beside it.
  const composerModelStatusIndicators: ComposerModelStatusIndicator[] = [];
  const messageToolPreviews = useMemo(
    () => toolPreviewsFromMessages(activeConversation?.messages ?? []),
    [activeConversation?.messages],
  );
  const liveBrowserState = useMemo(
    () => reduceBrowserStateFromEvents((activeConversation?.messages ?? []).flatMap((message) => message.events ?? [])),
    [activeConversation?.messages],
  );
  const latestToolFilterContext = useMemo(
    () => extractLatestToolFilterContext(activeConversation?.messages ?? []),
    [activeConversation?.messages],
  );
  const runtimeCapabilitySnapshot = latestToolFilterContext.snapshot;
  const toolFilterEntries = latestToolFilterContext.entries;
  const preferredVisionCandidate = useMemo(
    () => visionCandidateForProfile(activeProfile, selectableModelProfiles),
    [activeProfile, selectableModelProfiles],
  );
  const canvasPreviews = useMemo(() => {
    const seenIds = new Set(previews.map((preview) => preview.id));
    const seenIdentities = new Set(previews.map(canvasPreviewIdentity));
    return [
      ...previews,
      ...messageToolPreviews.filter((preview) => {
        const identity = canvasPreviewIdentity(preview);
        if (seenIds.has(preview.id) || seenIdentities.has(identity)) return false;
        seenIds.add(preview.id);
        seenIdentities.add(identity);
        return true;
      }),
    ].sort((a, b) => b.timestamp - a.timestamp);
  }, [messageToolPreviews, previews]);
  const canShowCanvas = hasCanvasItems(canvasPreviews, canvasMemo) || liveBrowserState.state_revision >= 0;
  const effectiveShowPreview = showPreview && canShowCanvas;
  const effectiveCommandCatalog = useMemo(() => (
    usesResolvedCommandProtocol
      ? commandCatalog
      : mergeRegisteredSlashCommands(
          commandCatalog,
          registeredSlashCommandsFromSettings(settingsValues.commands?.registered_slash_commands),
        )
  ), [commandCatalog, settingsValues.commands?.registered_slash_commands, usesResolvedCommandProtocol]);

  useEffect(() => {
    if (!usesResolvedCommandProtocol || pendingCommandApproval || effectiveCommandCatalog.length === 0) return;
    let cancelled = false;
    void api.pendingCommandApprovals()
      .then(({ pending_approvals: approvals }) => {
        if (cancelled || approvals.length === 0) return;
        const pending = approvals[0];
        const result = pending.result;
        const requestId = result?.approval?.request_id ?? pending.approval_request_id;
        const commandRef = result?.command_ref;
        const details = result?.approval?.details;
        if (!requestId || !commandRef || !details) return;
        const command = effectiveCommandCatalog.find((candidate) => (
          candidate.canonical_id === commandRef
          || candidate.id === commandRef
          || candidate.name === commandRef
        ));
        if (!command) return;
        const restoredMode = details.mode;
        setPendingCommandApproval({
          requestId,
          invocationId: pending.invocation_id,
          commandRef,
          command,
          args: details.approved_arguments ?? details.args ?? {},
          conversationId: typeof details.conversation_id === "string"
            ? details.conversation_id
            : null,
          mode: restoredMode === "chat" || restoredMode === "coding" || restoredMode === "agent"
            ? restoredMode
            : mode as ComposerCommandMode,
          approvalKind: result?.approval?.kind === "authority"
            ? "authority"
            : "coding",
        });
      })
      .catch((restoreError) => {
        if (!cancelled) console.error("Failed to restore pending command approval", restoreError);
      });
    return () => {
      cancelled = true;
    };
  }, [effectiveCommandCatalog, mode, pendingCommandApproval, usesResolvedCommandProtocol]);

  useEffect(() => {
    if (!pendingHighRiskCommand) return;
    let disposed = false;
    let retryTimer: number | null = null;
    const pending = pendingHighRiskCommand;

    const clearPending = () => {
      setPendingHighRiskCommand((current) => (
        current?.invocationId === pending.invocationId ? null : current
      ));
    };
    const schedulePoll = () => {
      if (!disposed) retryTimer = window.setTimeout(() => void poll(), 750);
    };
    const poll = async () => {
      try {
        const approval = await api.getInteractiveApproval(pending.requestId);
        if (disposed) return;
        if (approval.request_id !== pending.requestId) {
          throw new Error("高リスク操作の承認リクエストが一致しません。");
        }
        const invocation = await api.highRiskCommandStatus(pending.invocationId);
        if (disposed) return;
        if (
          invocation.invocation_id !== pending.invocationId
          || invocation.approval_request_id !== pending.requestId
        ) {
          throw new Error("高リスク操作の実行状態が一致しません。");
        }
        if (HIGH_RISK_TERMINAL_STATES.has(invocation.state)) {
          clearPending();
          if (invocation.state === "succeeded") {
            transientAlertSequenceRef.current += 1;
            setTransientAlert({
              id: `high-risk-succeeded-${transientAlertSequenceRef.current}`,
              message: `/${pending.commandLabel} を実行しました。`,
              tone: "success",
            });
          } else {
            setError(`/${pending.commandLabel} は ${invocation.state} のため実行されませんでした。`);
          }
          return;
        }
        if (["denied", "expired", "cancelled", "stale", "failed"].includes(approval.state)) {
          if (!beginHighRiskAttempt(highRiskCancelStartedRef.current, pending.invocationId)) return;
          let cancelled: Awaited<ReturnType<typeof api.cancelHighRiskCommand>>;
          try {
            cancelled = await api.cancelHighRiskCommand(pending.invocationId);
          } catch (cancelError) {
            releaseHighRiskAttempt(highRiskCancelStartedRef.current, pending.invocationId);
            throw cancelError;
          }
          if (disposed) return;
          if (cancelled.invocation_id !== pending.invocationId) {
            releaseHighRiskAttempt(highRiskCancelStartedRef.current, pending.invocationId);
            throw new Error("高リスク操作の取消状態が一致しません。");
          }
          clearPending();
          if (approval.state !== "denied") {
            setError(`/${pending.commandLabel} の承認は ${approval.state} のため取り消されました。`);
          }
          return;
        }
        if (approval.state === "approved" && invocation.state === "approved") {
          if (!beginHighRiskAttempt(highRiskResumeStartedRef.current, pending.invocationId)) return;
          let resumed: Awaited<ReturnType<typeof api.resumeHighRiskCommand>>;
          try {
            resumed = await api.resumeHighRiskCommand(pending.invocationId);
          } catch (resumeError) {
            // The HTTP response may have been lost before or after the Host
            // claimed the effect. Its CAS path is idempotent, so a later
            // authoritative poll may safely make the same resume request.
            releaseHighRiskAttempt(highRiskResumeStartedRef.current, pending.invocationId);
            throw resumeError;
          }
          if (disposed) return;
          if (resumed.invocation_id !== pending.invocationId) {
            releaseHighRiskAttempt(highRiskResumeStartedRef.current, pending.invocationId);
            throw new Error("高リスク操作の再開状態が一致しません。");
          }
          clearPending();
          if (resumed.state === "succeeded") {
            transientAlertSequenceRef.current += 1;
            setTransientAlert({
              id: `high-risk-succeeded-${transientAlertSequenceRef.current}`,
              message: `/${pending.commandLabel} を実行しました。`,
              tone: "success",
            });
          } else {
            setError(`/${pending.commandLabel} は ${resumed.state} のため完了しませんでした。`);
          }
          return;
        }
        schedulePoll();
      } catch (pollError) {
        if (disposed) return;
        setError(
          pollError instanceof Error
            ? pollError.message
            : "高リスク操作の承認状態を確認できませんでした。",
        );
        schedulePoll();
      }
    };

    void poll();
    return () => {
      disposed = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
    };
  }, [pendingHighRiskCommand]);

  useEffect(() => {
    if (!usesResolvedCommandProtocol || pendingHighRiskCommand) return;
    let disposed = false;
    void api.listHighRiskCommands()
      .then(({ invocations }) => {
        if (disposed || highRiskPrepareInFlightRef.current) return;
        const pending = invocations.find((item) => (
          Boolean(item.invocation_id)
          && Boolean(item.approval_request_id)
          && !HIGH_RISK_TERMINAL_STATES.has(item.state)
        ));
        const approvalRequestId = pending?.approval_request_id;
        if (!pending || !approvalRequestId) return;
        setPendingHighRiskCommand((current) => current ?? {
          requestId: approvalRequestId,
          invocationId: pending.invocation_id,
          commandLabel: "高リスク操作",
        });
        if (highRiskApprovalWindowOpenedRequestRef.current === approvalRequestId) return;
        // Claim before awaiting the native window open so React Strict Mode or
        // a state refresh cannot create duplicate approval windows.
        highRiskApprovalWindowOpenedRequestRef.current = approvalRequestId;
        void openAuthorityApprovalWindow(approvalRequestId)
          .then((opened) => {
            if (!disposed && !opened) {
              setError("専用の承認ウィンドウを開けませんでした。承認待ち表示から再試行してください。");
            }
          })
          .catch((restoreOpenError) => {
            if (!disposed) {
              console.error("Failed to open restored high-risk approval", restoreOpenError);
              setError("専用の承認ウィンドウを開けませんでした。承認待ち表示から再試行してください。");
            }
          });
      })
      .catch((restoreError) => {
        if (!disposed) console.error("Failed to restore pending high-risk command", restoreError);
      });
    return () => {
      disposed = true;
    };
  }, [pendingHighRiskCommand, usesResolvedCommandProtocol]);

  useEffect(() => {
    const preview = canvasPreviews.find(isHumanOperatorCanvasPreview);
    if (!preview) {
      humanOperatorAutoOpenedPreviewRef.current = null;
      return;
    }
    if (humanOperatorAutoOpenedPreviewRef.current === preview.id) return;
    humanOperatorAutoOpenedPreviewRef.current = preview.id;
    setActivePreviewId(preview.id);
    setShowPreview(true);
  }, [canvasPreviews]);

  const composerCommands = useMemo(() => {
    if (!slashCommandsEnabled) return [];
    const showAdvanced = settingsValues.commands?.show_advanced_commands === true;
    const fastCandidate = fastCandidateForProfile(activeProfile, selectableModelProfiles);
    const priceLowCandidate = priceCandidateForProfile(activeProfile, selectableModelProfiles, "low");
    const priceHighCandidate = priceCandidateForProfile(activeProfile, selectableModelProfiles, "high");
    return effectiveCommandCatalog
      .filter((command) => command.visibility !== "hidden")
      .filter((command) => showAdvanced || command.visibility === "default")
      .filter((command) => !command.modes?.length || command.modes.includes(mode as ComposerCommandMode))
      .filter((command) => command.id !== "fast" || Boolean(fastCandidate))
      .filter((command) => command.id !== "price" || Boolean(priceLowCandidate || priceHighCandidate))
      .filter((command) => command.id !== "think" || profileSupportsThinking(activeProfile))
      .map((command) => {
        const stateRef = protocolCommandStateRef(command);
        const protocolState = stateRef === "host:approval.full_access"
          ? ultraYoloMode
          : stateRef === "defaultspack:models.deepthink_enabled"
            ? deepthinkEnabled
            : settingsStateRefValue(stateRef, settingsValues);
        const legacyState = command.id === "yolo" || command.id === "ultra_yolo"
          ? ultraYoloMode
          : command.id === "deepthink"
            ? deepthinkEnabled
            : command.id === mode;
        const active = protocolState ?? legacyState;
        return { ...command, active, enabled: active };
      });
  }, [activeProfile, deepthinkEnabled, effectiveCommandCatalog, mode, selectableModelProfiles, settingsValues, slashCommandsEnabled, ultraYoloMode]);
  const modelCommandCandidates = composerCandidateMenu?.mode === "model" ? composerCandidateMenu.candidates : [];
  const unknownBlockStrategy = String(settingsValues.chat_rendering?.unknown_block_strategy ?? "placeholder");
  const showWidgets = settingsValues.chat_rendering?.show_widgets !== false;
  const showActivityInMessages = settingsValues.general?.show_activity_in_messages !== false;
  const showRegion = (regionId: string) => !catalog?.shell || hasShellRegion(catalog, regionId);
  const isActivityPreviewVisible =
    showRegion("activity_preview") &&
    effectiveShowPreview &&
    !isCanvasWorkspace &&
    !isDesktopsWorkspace &&
    !isSubagentWorkspace;
  const activityPreviewWidthPx = clampNumber(activityPreviewWidth, 220, 720, 340);
  const operationsProfileAvailable = hasOperationsProfile(catalog);
  const mimoCodingProfileAvailable = hasMimoCodingProfile(catalog);

  const startActivityPreviewResize = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      event.preventDefault();
      const startX = event.clientX;
      const startWidth = activityPreviewWidthPx;
      const handlePointerMove = (moveEvent: PointerEvent) => {
        const nextWidth = clampNumber(startWidth + (startX - moveEvent.clientX), 220, 720, startWidth);
        setActivityPreviewWidth(nextWidth);
      };
      const handlePointerUp = () => {
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        window.removeEventListener("pointermove", handlePointerMove);
        window.removeEventListener("pointerup", handlePointerUp);
      };
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      window.addEventListener("pointermove", handlePointerMove);
      window.addEventListener("pointerup", handlePointerUp, { once: true });
    },
    [activityPreviewWidthPx, setActivityPreviewWidth],
  );

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(max-width: 759px)");
    const applyMobileHistoryLayout = () => {
      if (shouldAutoCompactHistory(window.innerWidth)) {
        setIsHistoryMinimized(true);
      }
    };
    applyMobileHistoryLayout();
    media.addEventListener("change", applyMobileHistoryLayout);
    return () => media.removeEventListener("change", applyMobileHistoryLayout);
  }, [setIsHistoryMinimized]);

  useEffect(() => {
    const validIds = new Set(composerExtensions.map((tool) => tool.id));
    setStoredSelectedToolIds((current) => {
      const next = current.filter((toolId) => validIds.has(toolId));
      return next.length === current.length ? current : next;
    });
  }, [composerExtensions, setStoredSelectedToolIds]);

  useEffect(() => {
    const validIds = new Set(composerExtensions.map((tool) => tool.id));
    const defaults = activeTemplateToolPolicy.defaultEnabledToolIds.filter((toolId) => validIds.has(toolId));
    if (defaults.length === 0) return;
    setStoredSelectedToolIds((current) => {
      let changed = false;
      const next = [...current];
      for (const toolId of defaults) {
        if (next.includes(toolId)) continue;
        next.push(toolId);
        changed = true;
      }
      return changed ? next : current;
    });
  }, [activeTemplateToolPolicy.defaultEnabledToolIds, composerExtensions, setStoredSelectedToolIds]);

  const updatePendingRequests = (updater: (current: Record<string, PendingChatRequest>) => Record<string, PendingChatRequest>) => {
    setPendingRequests((current) => {
      const next = updater(current);
      writeJsonLocalStorage(pendingStorageKey, next);
      return next;
    });
  };

  const rememberPendingRequest = (request: PendingChatRequest) => {
    updatePendingRequests((current) => ({
      ...current,
      [request.conversationId]: request,
    }));
  };

  const forgetPendingRequest = (conversationId: string) => {
    updatePendingRequests((current) => {
      const next = { ...current };
      delete next[conversationId];
      return next;
    });
  };

  const loadCodingWorkspaces = useCallback(async () => {
    try {
      const result = await api.listCodingWorkspaces();
      setCodingWorkspaces(result.workspaces);
      let selectedWorkspaceId = result.selected_workspace_id ?? result.workspaces[0]?.workspace_id ?? null;
      setSelectedCodingWorkspaceId((current) => {
        selectedWorkspaceId = current ?? selectedWorkspaceId;
        return selectedWorkspaceId;
      });
      return { ...result, selected_workspace_id: selectedWorkspaceId };
    } catch {
      setCodingWorkspaces([]);
      return { workspaces: [], selected_workspace_id: null };
    }
  }, []);

  const activeConversationWorkspaceContext = useMemo(
    () => workspaceContextFromConversation(activeConversation),
    [activeConversation],
  );
  const effectiveWorkspaceId = pendingNewTaskContext?.workspaceId
    ?? activeConversationWorkspaceContext.workspaceId
    ?? selectedCodingWorkspaceId;
  const effectiveGroupId = pendingNewTaskContext?.groupId
    ?? activeConversationWorkspaceContext.groupId
    ?? undefined;
  const effectiveConsoleKey = `${effectiveGroupId ?? "ungrouped"}:${effectiveWorkspaceId ?? "no-workspace"}`;

  const loadCodingContext = useCallback(async (workspaceIdOverride?: string | null) => {
    const workspaceId = workspaceIdOverride ?? effectiveWorkspaceId;
    try {
      const [result, branchInfo] = await Promise.all([
        api.getCodingContext({ directory: codingDirectory, workspace_id: workspaceId }),
        api.getGitBranch({ workspace_id: workspaceId }).catch(() => null),
      ]);
      setCodingContext({
        branch: result.branch,
        rootFolder: result.root_folder,
        workspaceId: result.workspace_id ?? workspaceId,
        directory: result.directory ?? codingDirectory,
        branches: branchInfo?.branches ?? [],
        files: result.files,
        entries: result.entries,
        git: result.git,
      });
    } catch {
      setCodingContext(null);
    }
  }, [codingDirectory, effectiveWorkspaceId]);

  useEffect(() => {
    if (!activeConversationId) {
      shouldFollowMessagesRef.current = false;
      return;
    }
    const saved = conversationScrollState.get(activeConversationId);
    shouldFollowMessagesRef.current = saved?.follow ?? true;
    if (!saved || saved.follow) return;
    const frame = window.requestAnimationFrame(() => {
      if (messagesScrollRef.current) {
        messagesScrollRef.current.scrollTop = saved.scrollTop;
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeConversationId]);

  const handleMessagesScroll = useCallback(() => {
    const scroller = messagesScrollRef.current;
    if (!scroller) return;
    const follow = isMessageScrollerNearBottom(scroller);
    shouldFollowMessagesRef.current = follow;
    if (activeConversationId) {
      conversationScrollState.set(activeConversationId, {
        follow,
        scrollTop: scroller.scrollTop,
      });
    }
  }, [activeConversationId]);

  useEffect(() => {
    if (!shouldFollowMessagesRef.current) return;
    messagesEndRef.current?.scrollIntoView({ block: "end" });
  }, [activeConversationId, messages, isGenerating]);

  useEffect(() => {
    const markUnloading = () => {
      isUnloadingRef.current = true;
    };
    window.addEventListener("beforeunload", markUnloading);
    window.addEventListener("pagehide", markUnloading);
    return () => {
      window.removeEventListener("beforeunload", markUnloading);
      window.removeEventListener("pagehide", markUnloading);
    };
  }, []);

  useEffect(() => {
    const handleOauthMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      const payload = event.data;
      if (!payload || typeof payload !== "object") return;
      if ((payload as Record<string, unknown>).type === "rumi_human_operator_sync") {
        const conversationId = String((payload as Record<string, unknown>).conversation_id ?? "").trim();
        if (conversationId && conversationId === activeConversationId) {
          void api.getConversation(conversationId)
            .then((conversation) => {
              setActiveConversation(conversation);
              void refreshPreview(conversationId);
            })
            .catch(console.error);
        }
        return;
      }
      if ((payload as Record<string, unknown>).type !== "rumi_provider_oauth") return;
      const providerId = String((payload as Record<string, unknown>).provider_id ?? "").trim();
      if (providerId) {
        void refreshProviderOAuthStatus(providerId).catch(console.error);
        return;
      }
      void refreshCatalog().catch(console.error);
    };
    window.addEventListener("message", handleOauthMessage);
    return () => {
      window.removeEventListener("message", handleOauthMessage);
    };
  }, [activeConversationId]);

  useEffect(() => {
    if (mode === "coding") {
      void loadCodingWorkspaces().then((result) => loadCodingContext(result.selected_workspace_id ?? null));
    }
  }, [mode, loadCodingContext, loadCodingWorkspaces]);

  useEffect(() => {
    if (window.location.pathname !== "/coding") return;
    setMode("coding");
  }, [setMode]);

  useEffect(() => {
    if (!isSettingsOpen) return;
    let cancelled = false;
    // The bootstrap response deliberately omits dynamic provider metadata.  Fetch
    // the full registry when Settings opens so built-in Provider/API controls do
    // not look like empty extension slots while the shell is still settling.
    setSettingsLoadState({ status: "loading" });
    void api.uiSettings({ full: true })
      .then((settings) => {
        if (cancelled) return;
        setSettingsSections(settings.sections);
        // A full refresh can finish after a failed/queued save. Do not replace
        // the user's recoverable local edits with an older server snapshot.
        if (settingsDirtyKeysRef.current.length === 0) {
          const nextValues = withCalendarSettingsValues(settings.values);
          settingsValuesRef.current = nextValues;
          setSettingsValues(nextValues);
        }
        setSettingsLoadState({ status: "ready" });
      })
      .catch((settingsError) => {
        console.error(settingsError);
        if (!cancelled) {
          setSettingsLoadState({
            status: "error",
            message: settingsError instanceof Error ? settingsError.message : "Failed to refresh Settings.",
          });
        }
      });
    void fetchDesktopSystemInfo()
      .then((info) => {
        if (!cancelled) setDesktopSystemInfo(info);
      })
      .catch((infoError) => {
        console.error(infoError);
        if (!cancelled) setDesktopSystemInfo(null);
      });
    return () => {
      cancelled = true;
    };
  }, [isSettingsOpen]);

  const refreshHealth = useCallback(async (reason: "bootstrap" | "poll" | "focus" = "poll") => {
    try {
      const nextHealth = await api.health();
      consecutiveHealthFailuresRef.current = 0;
      lastHealthyAtRef.current = Date.now();
      setHealth(nextHealth);
      setBackendConnectionState("online");
      setBackendConnectionNote(null);
    } catch (healthError) {
      console.error(healthError);
      consecutiveHealthFailuresRef.current += 1;
      const hadHealthyConnection = lastHealthyAtRef.current !== null;
      const nextState: BackendConnectionState = hadHealthyConnection && consecutiveHealthFailuresRef.current < 3
        ? "degraded"
        : "offline";
      const message = healthError instanceof Error ? healthError.message : "backend connection lost";
      setBackendConnectionState(nextState);
      setBackendConnectionNote(
        hadHealthyConnection
          ? `最後に安定していた backend から切れました。再接続を試しています。${message}`
          : `backend の応答をまだ確認できていません。${message}`,
      );
      if (reason !== "poll" || nextState === "offline") {
        void reportClientDiagnostic({
          source: "webapp",
          category: "backend_connection",
          level: nextState === "offline" ? "error" : "warning",
          message: nextState === "offline"
            ? "The frontend lost its backend connection and entered offline protection."
            : "The frontend detected backend instability and entered degraded mode.",
          fingerprint: `backend-connection:${nextState}:${message}`,
          conversationId: activeConversationId,
          detail: {
            reason,
            error: message,
            consecutiveFailures: consecutiveHealthFailuresRef.current,
            lastHealthyAt: lastHealthyAtRef.current,
          },
        });
      }
    }
  }, [activeConversationId]);

  useEffect(() => {
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") {
        void refreshHealth("focus");
      }
    };
    const interval = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        void refreshHealth("poll");
      }
    }, backendConnectionState === "online" ? 15_000 : 4_000);
    window.addEventListener("focus", refreshWhenVisible);
    window.addEventListener("online", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener("focus", refreshWhenVisible);
      window.removeEventListener("online", refreshWhenVisible);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [backendConnectionState, refreshHealth]);

  function mergeProviderOAuthStatus(providerId: string, oauthStatus: Record<string, unknown>) {
    setSettingsValues((current) => {
      const apiSection = current.apis;
      const apiKeys = apiSection?.api_keys;
      if (!Array.isArray(apiKeys)) return current;

      let updated = false;
      const nextApiKeys = apiKeys.map((entry) => {
        if (!entry || typeof entry !== "object") return entry;
        const provider = entry as Record<string, unknown>;
        if (String(provider.provider_id ?? "").trim() !== providerId) return provider;

        updated = true;
        const existingOauth = provider.oauth && typeof provider.oauth === "object" && !Array.isArray(provider.oauth)
          ? provider.oauth as Record<string, unknown>
          : {};
        return {
          ...provider,
          oauth: {
            ...existingOauth,
            ...oauthStatus,
          },
        };
      });

      if (!updated) return current;
      return {
        ...current,
        apis: {
          ...(apiSection ?? {}),
          api_keys: nextApiKeys,
        },
      };
    });
  }

  async function refreshProviderOAuthStatus(providerId: string, options: { activeDiagnostics?: boolean } = {}) {
    const result = await api.providerOAuthStatus(providerId, options);
    if (result.provider && typeof result.provider === "object" && !Array.isArray(result.provider)) {
      mergeProviderOAuthStatus(providerId, result.provider as Record<string, unknown>);
    }
    void refreshCatalog().catch(console.error);
  }

  async function refreshCatalog(): Promise<CatalogRefreshResult | null> {
    const requestSequence = ++refreshCatalogSequenceRef.current;
    setSettingsLoadState({ status: "loading" });
    setModelProfilesLoadState({ status: "loading" });
    const [catalogResult, settingsResult, profilesResult, commandsResult] = await Promise.allSettled([
      api.uiCatalog(),
      api.uiSettings(),
      api.listModelProfiles(),
      api.resolvedUiCommands(),
    ]);
    if (requestSequence !== refreshCatalogSequenceRef.current) return null;
    const nextCatalog = catalogResult.status === "fulfilled" ? catalogResult.value : null;
    const nextSettings = settingsResult.status === "fulfilled" ? settingsResult.value : null;
    if (nextCatalog) {
      setCatalog(nextCatalog);
    } else if (catalogResult.status === "rejected") {
      // Keep the last validated catalog visible during transient provider or
      // registry failures; the individual load states communicate staleness.
      console.error(catalogResult.reason);
    }
    if (profilesResult.status === "fulfilled") {
      setModelProfiles(profilesResult.value.profiles);
      setModelProfilesLoadState({ status: "ready" });
    } else {
      console.error(profilesResult.reason);
      setModelProfilesLoadState({
        status: "error",
        message: profilesResult.reason instanceof Error ? profilesResult.reason.message : "Failed to load model profiles.",
      });
    }
    if (nextSettings) {
      setSettingsSections(nextSettings.sections);
      // Provider/OAuth refreshes run independently of settings saves. Preserve
      // dirty values until the existing save/retry flow has resolved them.
      if (settingsDirtyKeysRef.current.length === 0) {
        const nextValues = withCalendarSettingsValues(nextSettings.values);
        settingsValuesRef.current = nextValues;
        setSettingsValues(nextValues);
      }
      setSettingsLoadState({ status: "ready" });
    } else {
      if (settingsResult.status === "rejected") console.error(settingsResult.reason);
      setSettingsLoadState({
        status: "error",
        message: settingsResult.status === "rejected" && settingsResult.reason instanceof Error
          ? settingsResult.reason.message
          : "Failed to load Settings.",
      });
    }
    if (commandsResult.status === "rejected") {
      console.error(commandsResult.reason);
    }
    const resolvedCommandsResponse = commandsResult.status === "fulfilled"
      ? commandsResult.value
      : null;
    const resolvedProtocol = resolvedCommandsResponse?.protocol ?? null;
    setCommandProtocolInfo(resolvedProtocol);
    setUsesResolvedCommandProtocol(Boolean(resolvedProtocol));
    setCommandCatalog(
      resolvedProtocol
        ? resolvedCommandsResponse?.commands ?? []
        : mergeComposerCommands(
            commandsResult.status === "fulfilled" ? commandsResult.value.commands ?? [] : [],
            nextCatalog?.commands ?? [],
          ),
    );
    const defaultMode = nextSettings?.values.preview?.default_mode;
    if (defaultMode === "auto" || defaultMode === "manual") {
      setPreviewMode(defaultMode);
    }
    const fallbackCommandsAvailable = Boolean(nextCatalog?.commands?.length);
    const commandReady = commandsResult.status === "fulfilled" || fallbackCommandsAvailable;
    const readinessFailures = [
      !nextCatalog ? "UIカタログ" : "",
      settingsResult.status === "rejected" ? "設定" : "",
      profilesResult.status === "rejected" ? "モデル" : "",
      !commandReady ? "コマンド" : "",
    ].filter(Boolean);
    return {
      catalog: nextCatalog,
      ready: readinessFailures.length === 0,
      degraded: commandsResult.status === "rejected" && fallbackCommandsAvailable,
      errorMessage: readinessFailures.length > 0
        ? `${readinessFailures.join("・")}の初期化を完了できませんでした。`
        : null,
    };
  }

  async function refreshOperationsStatus() {
    try {
      setOperationsStatus(await api.getOperationsCompanyStatus());
    } catch (statusError) {
      console.error(statusError);
    }
  }

  async function refreshMimoCodingStatus() {
    try {
      setMimoCodingStatus(await api.getMimoCodingCompanyStatus());
    } catch (statusError) {
      console.error(statusError);
    }
  }

  async function refreshPreview(conversationId: string | null) {
    if (!conversationId) {
      setPreviews([]);
      setActivePreviewId(null);
      return;
    }
    try {
      const result = await api.conversationPreview(conversationId);
      const limit = Number(settingsValues.preview?.max_items ?? 12);
      const nextPreviews = result.previews.slice(0, limit);
      setPreviews(nextPreviews);
      setActivePreviewId(nextPreviews[0]?.id ?? null);
      if (settingsValues.preview?.auto_open && nextPreviews.length > 0) {
        setShowPreview(true);
      }
    } catch (previewError) {
      console.error(previewError);
      setPreviews([]);
      setActivePreviewId(null);
    }
  }

  async function loadConversation(conversationId: string | null, updateUrl = true) {
    if (!conversationId) {
      setActiveConversationId(null);
      setActiveConversation(null);
      void refreshPreview(null);
      if (updateUrl) replaceChatIdInUrl(null, false);
      return;
    }
    const conversation = await api.getConversation(conversationId);
    const supersededTargetId = resolveSupersededConversationRedirect(conversation, conversationId);
    if (supersededTargetId) {
      await loadConversation(supersededTargetId, updateUrl);
      return;
    }
    setActiveConversationId(conversationId);
    setActiveConversation(conversation);
    if (updateUrl) replaceChatIdInUrl(conversationId);
    void refreshPreview(conversationId);
  }

  async function refreshConversations(preferredId?: string | null) {
    const result = await api.listConversations();
    setConversations(result.conversations);
    await loadConversationForRefresh({
      preferredId,
      activeConversationId,
      locationChatId: chatIdFromLocation(),
      listedConversations: result.conversations,
      loadConversation,
    });
  }

  useEffect(() => subscribeAuthorityApprovalSettlements((event) => {
    setSettledRuntimeApprovalIds((ids) => (
      ids.includes(event.requestId) ? ids : [...ids, event.requestId].slice(-50)
    ));
    if (event.conversationId && event.conversationId === activeConversationId) {
      void refreshConversations(event.conversationId);
    }
  }), [activeConversationId]);

  useEffect(() => {
    let cancelled = false;

    async function bootstrap() {
      setIsLoading(true);
      setStartupError(null);
      setStartupSteps([
        { id: "backend", label: "バックエンドとの接続を確認しています…", status: "loading" },
        { id: "capabilities", label: "ツール・スキル・@候補を読み込みます", status: "pending" },
        { id: "commands", label: "/コマンド・モデル・設定を準備します", status: "pending" },
        { id: "conversations", label: "会話とワークスペースを復元します", status: "pending" },
      ]);
      const updateStartupStep = (
        id: string,
        status: TobkiriLoadingStep["status"],
        label?: string,
      ) => {
        if (cancelled) return;
        setStartupSteps((current) => current.map((step) => (
          step.id === id ? { ...step, status, ...(label ? { label } : {}) } : step
        )));
      };
      const pendingConversationId = chatIdFromLocation();
      if (pendingConversationId && isPendingInLocation()) {
        // A reload can arrive through the pending URL after the transport has
        // already committed the request. Keep the operation id persisted in
        // local storage so a retry replays that logical send instead of
        // creating a second user turn. The URL only carries the conversation
        // id and therefore cannot reconstruct this identity by itself.
        const storedPending = pendingRequests[pendingConversationId];
        rememberPendingRequest({
          ...storedPending,
          conversationId: pendingConversationId,
          startedAt: storedPending?.startedAt ?? Date.now(),
          status: storedPending?.status ?? "Processing...",
          toolNames: storedPending?.toolNames ?? [],
          recoveredFromLocation: true,
        });
      }
      const backendBootstrap = refreshHealth("bootstrap").then(() => {
        updateStartupStep("backend", "ready", "バックエンドの接続状態を確認しました");
      });
      updateStartupStep("capabilities", "loading", "ツール・スキル・@候補を読み込んでいます…");
      updateStartupStep("commands", "loading", "/コマンド・モデル・設定を読み込んでいます…");
      const interfaceBootstrap = refreshCatalog()
        .then(async (result) => {
          if (cancelled) return null;
          if (!result?.ready || !result.catalog) {
            updateStartupStep("capabilities", "error", "ツール・スキル・@候補を準備できませんでした");
            updateStartupStep("commands", "error", "/コマンド・モデル・設定を準備できませんでした");
            throw new Error(
              result?.errorMessage
              ?? "インターフェース情報を取得できませんでした。バックエンド接続を確認してください。",
            );
          }
          updateStartupStep("capabilities", "ready", "ツール・スキル・@候補を準備しました");
          updateStartupStep(
            "commands",
            "ready",
            result.degraded
              ? "/コマンドを互換カタログから準備しました"
              : "/コマンド・モデル・設定を準備しました",
          );
          const statusRefreshes: Array<Promise<unknown>> = [];
          if (hasOperationsProfile(result.catalog)) {
            statusRefreshes.push(refreshOperationsStatus());
          }
          if (hasMimoCodingProfile(result.catalog)) {
            statusRefreshes.push(refreshMimoCodingStatus());
          }
          if (statusRefreshes.length > 0) {
            await Promise.all(statusRefreshes);
          }
          return result;
        });
      updateStartupStep("conversations", "loading", "会話とワークスペースを復元しています…");
      const conversationBootstrap = refreshConversations(null).then(() => {
        updateStartupStep("conversations", "ready", "会話とワークスペースを復元しました");
      });
      try {
        const [, interfaceResult] = await Promise.all([
          backendBootstrap,
          interfaceBootstrap,
          conversationBootstrap,
        ]);
        if (!cancelled) {
          setIsLoading(false);
          if (interfaceResult?.degraded) {
            transientAlertSequenceRef.current += 1;
            setTransientAlert({
              id: `startup-degraded-${transientAlertSequenceRef.current}`,
              message: "最新のコマンド経路を取得できなかったため、互換カタログを使用しています。",
              tone: "warning",
              durationMs: 6000,
            });
          }
        }
      } catch (bootstrapError) {
        if (!cancelled) {
          const message = bootstrapError instanceof Error
            ? bootstrapError.message
            : "起動準備を完了できませんでした。";
          setStartupError(message);
          setError(message);
          setStartupSteps((current) => current.map((step) => (
            step.status === "loading" ? { ...step, status: "error" } : step
          )));
        }
      }
    }

    void bootstrap();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!operationsProfileAvailable) return;
    void refreshOperationsStatus();
  }, [operationsProfileAvailable]);

  useEffect(() => {
    if (!mimoCodingProfileAvailable) return;
    void refreshMimoCodingStatus();
  }, [mimoCodingProfileAvailable]);

  useEffect(() => {
    const handlePopState = () => {
      setError(null);
      const routeKind = workspaceKindForPathname(window.location.pathname) ?? "chat";
      if (routeKind !== "chat") {
        const routeTabId = `workspace-tab-route-${routeKind}`;
        setWorkspaceTabs((current) => (
          current.some((tab) => tab.id === routeTabId)
            ? current
            : [...current, createWorkspaceTab(routeKind, { id: routeTabId })]
        ));
        setActiveWorkspaceTabId(routeTabId);
        setMode(routeKind === "coding" ? "coding" : "agent");
      } else {
        setActiveWorkspaceTabId(DEFAULT_WORKSPACE_TAB_ID);
        setMode("agent");
      }
      void loadConversation(chatIdFromLocation(), false).catch((loadError) => {
        setError(loadError instanceof Error ? loadError.message : "会話の読み込みに失敗しました。");
      });
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    if (!activeConversationId) return;
    void refreshPreview(activeConversationId);
  }, [settingsValues.preview?.max_items, settingsValues.preview?.auto_open, activeConversationId]);

  useEffect(() => {
    const handleGlobalKeyDown = (event: KeyboardEvent) => {
      if (!spotlightShortcutEnabled) return;
      if (!shortcutSpecMatchesEvent(spotlightShortcut, event, { allowTextInput: spotlightShortcutTextInput })) return;
      event.preventDefault();
      setIsSpotlightOpen(true);
      setSpotlightSelectedIndex(0);
    };
    document.addEventListener("keydown", handleGlobalKeyDown);
    return () => document.removeEventListener("keydown", handleGlobalKeyDown);
  }, [spotlightShortcut, spotlightShortcutEnabled, spotlightShortcutTextInput]);

  useEffect(() => {
    if (!isSpotlightOpen) return;
    const query = spotlightQuery.trim();
    if (!query) {
      setSpotlightResults([]);
      setSpotlightLoading(false);
      return;
    }
    let cancelled = false;
    setSpotlightLoading(true);
    const timeout = window.setTimeout(() => {
      void api.searchConversations(query, {
        date_filter: spotlightFilter === "starred" ? "all" : spotlightFilter,
        is_starred: spotlightFilter === "starred" ? true : undefined,
        role: "all",
        limit: 12,
      }).then((result) => {
        if (cancelled) return;
        setSpotlightResults(result.results);
      }).catch((searchError) => {
        if (cancelled) return;
        console.error(searchError);
        setSpotlightResults([]);
      }).finally(() => {
        if (!cancelled) setSpotlightLoading(false);
      });
    }, 120);
    return () => {
      cancelled = true;
      window.clearTimeout(timeout);
    };
  }, [isSpotlightOpen, spotlightFilter, spotlightQuery]);

  useEffect(() => {
    setSpotlightSelectedIndex(0);
  }, [spotlightFilter, spotlightQuery, spotlightResults.length]);

  useEffect(() => {
    if (!activeConversationId || !isConversationPending) return;
    const latestKnown = latestActiveMessage;
    if (shouldClearPendingAfterConversationRefresh(latestKnown, pendingRequest, Date.now())) {
      forgetPendingRequest(activeConversationId);
      replaceChatIdInUrl(activeConversationId, false);
      setIsGenerating(false);
      return;
    }
    if (streamingConversationIdRef.current === activeConversationId) return;
    setIsGenerating(true);
    const pollPendingConversation = () => {
      void api.getConversation(activeConversationId).then((conversation) => {
        setActiveConversation(conversation);
        const latest = conversation.messages[conversation.messages.length - 1];
        if (shouldClearPendingAfterConversationRefresh(latest, pendingRequest, Date.now())) {
          forgetPendingRequest(activeConversationId);
          replaceChatIdInUrl(activeConversationId, false);
          setIsGenerating(false);
          void refreshConversations(conversation.id);
        }
      }).catch((pollError) => {
        console.error(pollError);
        if (shouldForgetPendingAfterPollError(pollError)) {
          forgetPendingRequest(activeConversationId);
          replaceChatIdInUrl(activeConversationId, false);
          setIsGenerating(false);
          setError(pollError instanceof Error ? pollError.message : "stream 状態の確認に失敗しました。");
          return;
        }
        updatePendingRequests((current) => {
          const existing = current[activeConversationId];
          return existing ? {
            ...current,
            [activeConversationId]: {
              ...existing,
              status: "接続を待っています。同じ送信として再試行できます",
            },
          } : current;
        });
        setBackendConnectionState("degraded");
        setBackendConnectionNote("送信結果を確認できません。operation IDを保持して接続回復を待っています。");
      });
    };
    pollPendingConversation();
    const interval = window.setInterval(pollPendingConversation, 1500);
    return () => window.clearInterval(interval);
  }, [activeConversationId, isConversationPending, latestActivePendingSignature, pendingRequest]);

  useEffect(() => {
    const staleIds = Object.entries(pendingRequests)
      .filter(([, request]) => Date.now() - request.startedAt >= PENDING_CHAT_REQUEST_TTL_MS)
      .map(([id]) => id);
    if (staleIds.length === 0) return;
    updatePendingRequests((current) => {
      const next = { ...current };
      for (const id of staleIds) delete next[id];
      return next;
    });
    if (activeConversationId && staleIds.includes(activeConversationId)) {
      setIsGenerating(false);
      replaceChatIdInUrl(activeConversationId, false);
    }
  }, [pendingRequests, activeConversationId]);

  const handleNewTask = (options?: HistoryBoardNewTaskOptions) => {
    const nextContext = workspaceContextFromHistoryOptions(options);
    const nextTab = createWorkspaceTab("chat", { title: "New Conversation" });
    setWorkspaceTabs((current) => [...current, nextTab]);
    setActiveWorkspaceTabId(nextTab.id);
    setPendingNewTaskContext(nextContext);
    if (nextContext?.workspaceId) {
      setMode("coding");
    }
    setActiveConversationId(null);
    setActiveConversation(null);
    setPreviews([]);
    setError(null);
    setIsGenerating(false);
    cancelPendingMentionAttachments();
    setAttachedFiles([]);
    setDroppedWidgets([]);
    dismissedComposerMentionToolsRef.current.clear();
    setComposerEntityReferences([]);
    replaceChatIdInUrl(null, false);
  };

  const startSettingsChat = () => {
    const draft = createSettingsModeDraft(settingsAssistantSkill);
    handleNewTask();
    setMode("agent");
    setInput(draft.input);
    setDroppedWidgets(draft.widgets);
    setComposerEntityReferences(draft.references);
    setIsSettingsOpen(false);
  };

  const handleStopGenerating = () => {
    const conversationId = activeConversationId;
    if (conversationId) {
      void api.stopMessage(conversationId).catch(console.error);
    }
    currentAbortControllerRef.current?.abort();
    currentAbortControllerRef.current = null;
    if (conversationId) {
      forgetPendingRequest(conversationId);
      replaceChatIdInUrl(conversationId, false);
    }
    setIsGenerating(false);
    setIsNewChatLaunching(false);
  };

  const handleHistoryClick = (conversationId: string) => {
    setError(null);
    setPendingNewTaskContext(null);
    setActiveHistoryCompanyId(null);
    const activeTab = workspaceTabs.find((tab) => tab.id === activeWorkspaceTabId);
    if (activeTab?.kind === "chat") {
      setWorkspaceTabs((current) => current.map((tab) => tab.id === activeWorkspaceTabId ? { ...tab, conversationId } : tab));
    } else {
      const nextTab = createWorkspaceTab("chat", { conversationId, title: "AI Chat" });
      setWorkspaceTabs((current) => [...current, nextTab]);
      setActiveWorkspaceTabId(nextTab.id);
    }
    void loadConversation(conversationId);
  };

  const handleHistoryMetadataChange = (conversationId: string, updates: { is_pinned?: boolean; is_starred?: boolean; tags?: string[] }) => {
    setError(null);
    void api.updateConversation(conversationId, updates as Partial<Conversation>)
      .then((conversation) => {
        setConversations((current) => current.map((item) => item.id === conversation.id ? { ...conversation, messages: [] } : item));
        if (activeConversationId === conversation.id) setActiveConversation(conversation);
      })
      .catch((updateError) => setError(updateError instanceof Error ? updateError.message : "会話メタデータの更新に失敗しました。"));
  };

  const handleHistoryGroupSelect = (group: ChatGroup) => {
    setActiveHistoryCompanyId(resolveCompanyWorkspaceHintFromGroup(group));
  };

  const handleComposerProjectSelect = (project: ProjectInfo | null) => {
    const context = projectTaskContext(project);
    if (!activeConversationId || !activeConversation) {
      setPendingNewTaskContext(context);
      if (project?.workspaceId) setSelectedCodingWorkspaceId(project.workspaceId);
      return;
    }

    const metadata = { ...(activeConversation.metadata ?? {}) };
    delete metadata.groupId;
    if (project) {
      metadata.group_id = project.id;
      metadata.group_title = project.title;
      if (project.workspaceId) metadata.workspace_id = project.workspaceId;
      if (project.workspaceLabel) metadata.workspace_label = project.workspaceLabel;
      if (project.workspaceRoot) metadata.workspace_root = project.workspaceRoot;
      if (project.rumiDataPath) metadata.rumi_data_path = project.rumiDataPath;
    } else {
      delete metadata.group_id;
      delete metadata.group_title;
    }

    setError(null);
    void api.updateConversation(activeConversationId, {
      group_id: project?.id ?? null,
      metadata,
    }).then((conversation) => {
      setConversations((current) => current.map((item) => item.id === conversation.id ? { ...conversation, messages: [] } : item));
      setActiveConversation(conversation);
      if (project?.workspaceId) setSelectedCodingWorkspaceId(project.workspaceId);
    }).catch((updateError) => {
      setError(updateError instanceof Error ? updateError.message : "Project update failed.");
    });
  };

  const closeSpotlight = () => {
    setIsSpotlightOpen(false);
    setSpotlightQuery("");
    setSpotlightResults([]);
    setSpotlightSelectedIndex(0);
  };

  const openSpotlightResult = (result: ConversationSearchResult | undefined) => {
    if (!result?.conversation_id) return;
    closeSpotlight();
    setError(null);
    void loadConversation(result.conversation_id);
  };

  const handleSpotlightKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeSpotlight();
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setSpotlightSelectedIndex((index) => Math.min(index + 1, Math.max(visibleSpotlightResults.length - 1, 0)));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setSpotlightSelectedIndex((index) => Math.max(index - 1, 0));
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      openSpotlightResult(visibleSpotlightResults[spotlightSelectedIndex] ?? visibleSpotlightResults[0]);
    }
  };

  const applySettingsValues = (next: Record<string, Record<string, unknown>>) => {
    settingsValuesRef.current = next;
    setSettingsValues(next);
  };

  const settingsErrorMessage = (errorValue: unknown, fallback: string) => (
    errorValue instanceof Error && errorValue.message.trim() ? errorValue.message : fallback
  );

  const persistSettingsValues = (
    next: Record<string, Record<string, unknown>>,
    dirtyKey?: string,
    explicitPatches?: Array<{ section: string; field: string; value: unknown }>,
  ) => {
    const revision = ++settingsSaveRevisionRef.current;
    const dirtyKeys = [...new Set([
      ...settingsDirtyKeysRef.current,
      ...(dirtyKey ? [dirtyKey] : []),
      ...(explicitPatches ?? []).map((patch) => `${patch.section}.${patch.field}`),
    ])];
    settingsDirtyKeysRef.current = dirtyKeys;
    setSettingsSaveState({
      status: "saving",
      dirtyKeys,
      message: dirtyKeys.length > 1 ? `${dirtyKeys.length} settings are being saved.` : null,
    });
    const requestedPatches = explicitPatches ?? dirtyKeys.flatMap((key) => {
      const dot = key.indexOf(".");
      if (dot <= 0 || dot >= key.length - 1) return [];
      const section = key.slice(0, dot);
      const field = key.slice(dot + 1);
      return [{ section, field, value: next[section]?.[field] }];
    });
    const saveRequest = settingsSaveQueueRef.current
      .catch(() => undefined)
      .then(() => requestedPatches.length > 0
        ? api.updateUiSettingsPatches(requestedPatches)
        : api.updateUiSettings(next))
      .then((result) => {
        if (revision !== settingsSaveRevisionRef.current) return result;
        const persisted = withCalendarSettingsValues(result.values);
        settingsDirtyKeysRef.current = [];
        applySettingsValues(persisted);
        setSettingsSaveState({ status: "saved", dirtyKeys: [], lastSavedAt: Date.now(), message: null });
        return result;
      })
      .catch((saveError) => {
        if (revision !== settingsSaveRevisionRef.current) return undefined;
        const message = settingsErrorMessage(saveError, "Failed to save Settings.");
        setSettingsSaveState({ status: "error", dirtyKeys: settingsDirtyKeysRef.current, message });
        throw saveError;
      });
    settingsSaveQueueRef.current = saveRequest.then(() => undefined, () => undefined);
    return saveRequest;
  };

  const retrySettingsSave = () => {
    if (settingsDirtyKeysRef.current.length === 0) return;
    void persistSettingsValues(settingsValuesRef.current).catch(() => undefined);
  };

  const handleSettingChange = (sectionId: string, fieldId: string, value: unknown) => {
    if (sectionId === "sidebar" && fieldId === "ui_placements") {
      const previous = settingsValuesRef.current;
      const previousPlacements = normalizePinnedPlacements(previous.sidebar?.ui_placements);
      const next = withPinnedPlacements(previous, normalizePinnedPlacements(value));
      const revision = ++pinnedPlacementSaveRevisionRef.current;
      const dirtyKey = "sidebar.ui_placements";
      applySettingsValues(next);
      void persistSettingsValues(next, dirtyKey)
        .catch((updateError) => {
          if (revision !== pinnedPlacementSaveRevisionRef.current) return;
          const rolledBack = withPinnedPlacements(settingsValuesRef.current, previousPlacements);
          applySettingsValues(rolledBack);
          settingsDirtyKeysRef.current = settingsDirtyKeysRef.current.filter((key) => key !== dirtyKey);
          const message = settingsErrorMessage(updateError, "Failed to save pinned widgets; the placement change was reverted.");
          setSettingsSaveState({ status: "error", dirtyKeys: settingsDirtyKeysRef.current, message });
          setError(message);
        });
      return;
    }
    {
      const current = settingsValuesRef.current;
      const section = settingsSections.find((item) => item.id === sectionId);
      const field = section?.fields.find((item) => item.id === fieldId);
      const fieldType = String(field?.type ?? "");
      const actionPayload = value && typeof value === "object" && !Array.isArray(value)
        ? value as Record<string, unknown>
        : {};
      if (String(actionPayload.action ?? "") === "refresh" && (fieldType === "secret" || fieldType === "external_tokens")) {
        void refreshCatalog().catch(console.error);
        return;
      }
      const sectionPatch = {
        ...(current[sectionId] ?? {}),
        [fieldId]: fieldType === "secret" || fieldType === "api_keys" || fieldType === "api_key_setup" || fieldType === "external_tokens" ? "" : value,
      };
      if (sectionId === "models" && fieldId === "preferred_model") {
        const preferredModel = String(value ?? "").trim();
        if (preferredModel) {
          sectionPatch.main_model = preferredModel;
          sectionPatch.model_slots = {
            ...((current.models?.model_slots as Record<string, unknown> | undefined) ?? {}),
            main: preferredModel,
          };
        }
      }
      if (sectionId === "external_input" && fieldId === "input_provider") {
        const provider = String(value ?? "line");
        const template = firstExternalIoTemplateForProvider(catalog, "input", provider)
          ?? firstExternalIoTemplateForProvider(catalog, "input", "line");
        if (template) {
          const resolvedProvider = String(template.provider ?? provider);
          sectionPatch.input_provider = resolvedProvider;
          sectionPatch.input_template_id = String(template.id ?? "");
          sectionPatch.input_profile_id = String(template.input_profile_id ?? `${resolvedProvider}.default`);
          sectionPatch.input_endpoint_id = externalIoInputEndpointId(template, resolvedProvider);
          const route = externalIoTemplateRoute(template);
          if (route) {
            sectionPatch.public_url_launcher = {
              ...((current.external_input?.public_url_launcher as Record<string, unknown> | undefined) ?? {}),
              route_path: route,
            };
          }
        }
      } else if (sectionId === "external_input" && fieldId === "input_template_id") {
        const templateId = String(value ?? "");
        const template = externalIoTemplateById(catalog, "input", templateId);
        if (template) {
          const provider = String(template.provider ?? (templateId.split(".")[0] || "line"));
          sectionPatch.input_provider = provider;
          sectionPatch.input_profile_id = String(template.input_profile_id ?? `${provider}.default`);
          sectionPatch.input_endpoint_id = externalIoInputEndpointId(template, provider);
          const route = externalIoTemplateRoute(template);
          if (route) {
            sectionPatch.public_url_launcher = {
              ...((current.external_input?.public_url_launcher as Record<string, unknown> | undefined) ?? {}),
              route_path: route,
            };
          }
        }
      } else if (sectionId === "external_input" && fieldId === "input_response_preset") {
        const preset = String(value ?? "");
        const template = externalIoTemplateForResponsePreset(catalog, preset);
        if (template) {
          const provider = String(template.provider ?? "line");
          sectionPatch.input_provider = provider;
          sectionPatch.input_template_id = String(template.id ?? "");
          sectionPatch.input_profile_id = String(template.input_profile_id ?? `${provider}.default`);
          sectionPatch.input_endpoint_id = externalIoInputEndpointId(template, provider);
          const route = externalIoTemplateRoute(template);
          if (route) {
            sectionPatch.public_url_launcher = {
              ...((current.external_input?.public_url_launcher as Record<string, unknown> | undefined) ?? {}),
              route_path: route,
            };
          }
        }
      } else if (sectionId === "external_output" && fieldId === "output_provider") {
        const provider = String(value ?? "line");
        const template = firstExternalIoTemplateForProvider(catalog, "output", provider)
          ?? firstExternalIoTemplateForProvider(catalog, "output", "line");
        if (template) {
          const resolvedProvider = String(template.provider ?? provider);
          sectionPatch.output_provider = resolvedProvider;
          sectionPatch.output_template_id = String(template.id ?? "");
          sectionPatch.output_profile_id = String(template.output_profile_id ?? `${resolvedProvider}.default`);
          sectionPatch.output_send_mode = externalIoOutputMode(template) || String(sectionPatch.output_send_mode ?? "reply_to_origin");
        }
      } else if (sectionId === "external_output" && fieldId === "output_template_id") {
        const templateId = String(value ?? "");
        const template = externalIoTemplateById(catalog, "output", templateId);
        if (template) {
          const provider = String(template.provider ?? (templateId.split(".")[0] || "line"));
          sectionPatch.output_provider = provider;
          sectionPatch.output_profile_id = String(template.output_profile_id ?? `${provider}.default`);
          sectionPatch.output_send_mode = externalIoOutputMode(template) || String(sectionPatch.output_send_mode ?? "reply_to_origin");
        }
      }
      const next = {
        ...current,
        [sectionId]: sectionPatch,
      };
      if (fieldType === "api_keys" || fieldType === "api_key_setup") {
        const payload = value && typeof value === "object" ? value as Record<string, unknown> : {};
        const providerId = String(payload.provider_id ?? "").trim();
        const apiId = String(payload.api_id ?? payload.name ?? "").trim();
        const name = String(payload.name ?? apiId).trim();
        const secret = String(payload.value ?? "");
        const action = String(payload.action ?? "upsert").trim();
        const kind = String(payload.kind ?? "").trim() || undefined;
        if (action === "oauth_refresh") {
          const activeDiagnostics = Boolean(payload.active_diagnostics);
          if (providerId) {
            void refreshProviderOAuthStatus(providerId, { activeDiagnostics }).catch(console.error);
          } else {
            void refreshCatalog().catch(console.error);
          }
          return current;
        } else if (action === "register_provider" && providerId) {
          void api.registerCustomProvider(providerId, {
            label: String(payload.label ?? "").trim() || undefined,
            kind,
          })
            .then(() => refreshCatalog())
            .catch(console.error);
        } else if (action === "delete_provider" && providerId) {
          void api.deleteCustomProvider(providerId)
            .then(() => refreshCatalog())
            .catch(console.error);
        } else if (action === "delete" && providerId && apiId) {
          void api.deleteProviderApiKey(providerId, apiId)
            .then(() => refreshCatalog())
            .catch(console.error);
        } else if (action === "rename" && providerId && apiId && name) {
          void api.renameProviderApiKey(providerId, apiId, name)
            .then(() => refreshCatalog())
            .catch(console.error);
        } else if (providerId && name && secret.trim()) {
          void api.saveProviderApiKey(providerId, secret, {
            apiId: name,
            name,
            baseUrl: String(payload.base_url ?? "").trim() || undefined,
            allowedModels: Array.isArray(payload.allowed_models)
              ? payload.allowed_models.map((item) => String(item ?? "").trim()).filter(Boolean)
              : undefined,
            defaultModel: String(payload.default_model ?? "").trim() || undefined,
            quotaLabel: String(payload.quota_label ?? "").trim() || undefined,
            notes: String(payload.notes ?? "").trim() || undefined,
            kind,
          })
            .then(() => refreshCatalog())
            .catch(console.error);
        }
      } else if (field?.type === "external_tokens") {
        const payload = value && typeof value === "object" ? value as Record<string, unknown> : {};
        const providerId = String(payload.provider_id ?? "").trim();
        const tokenId = String(payload.token_id ?? payload.name ?? "").trim();
        const name = String(payload.name ?? tokenId).trim();
        const kind = String(payload.kind ?? "token").trim();
        const secret = String(payload.value ?? "");
        const action = String(payload.action ?? "upsert").trim();
        if (action === "delete" && providerId && tokenId) {
          void api.deleteExternalToken(providerId, tokenId)
            .then(() => refreshCatalog())
            .catch(console.error);
        } else if (action === "rename" && providerId && tokenId && name) {
          void api.renameExternalToken(providerId, tokenId, name)
            .then(() => refreshCatalog())
            .catch(console.error);
        } else if (providerId && name && secret.trim()) {
          void api.saveExternalToken(providerId, secret, { tokenId: name, name, kind })
            .then(() => refreshCatalog())
            .catch(console.error);
        }
      } else if (field?.type === "secret") {
        const providerId = field.provider_id ?? fieldId.replace(/_api_key$/, "");
        void api.saveProviderApiKey(providerId, String(value ?? ""))
          .then(() => refreshCatalog())
          .catch(console.error);
      } else {
        if (sectionId === "ambient" && fieldId === "ambient.monitor.enabled") {
          void (Boolean(value)
            ? ambientTriggerClient.startMonitor({ voice_wake: true, gesture_pinch: true })
            : ambientTriggerClient.stopMonitor()
          ).catch(console.error);
        }
        const ambientRoutingKey = sectionId === "ambient" ? AMBIENT_ROUTING_SETTING_KEYS[fieldId] : undefined;
        if (ambientRoutingKey) {
          void ambientTriggerClient.configure({ [ambientRoutingKey]: value } as AmbientRoutingConfig).catch(console.error);
        }
        const currentSection = current[sectionId] ?? {};
        const changedPatches = Object.entries(sectionPatch)
          .filter(([field, nextValue]) => currentSection[field] !== nextValue)
          .map(([field, nextValue]) => ({ section: sectionId, field, value: nextValue }));
        void persistSettingsValues(
          next,
          `${sectionId}.${fieldId}`,
          changedPatches,
        ).catch(() => undefined);
      }
      applySettingsValues(next);
    }
  };

  const updateModelSettings = (updates: Record<string, unknown>) => {
    const current = settingsValuesRef.current;
    const preferredModelUpdate = String(updates.preferred_model ?? "").trim();
    const normalizedUpdates = preferredModelUpdate
      ? {
          ...updates,
          main_model: preferredModelUpdate,
          model_slots: {
            ...((current.models?.model_slots as Record<string, unknown> | undefined) ?? {}),
            main: preferredModelUpdate,
          },
        }
      : updates;
    const next = withCalendarSettingsValues({
      ...current,
      models: {
        ...(current.models ?? {}),
        ...normalizedUpdates,
      },
    });
    applySettingsValues(next);
    void persistSettingsValues(
      next,
      preferredModelUpdate ? "models.preferred_model" : "models",
      Object.entries(normalizedUpdates).map(([field, value]) => ({ section: "models", field, value })),
    ).catch(() => undefined);
  };

  const handleModelProfileSelect = (profileId: string) => {
    updateModelSettings({ preferred_model: profileId });
    // New-conversation placeholders have no persisted conversation id yet, but
    // still carry the bootstrap model (usually stub/default). Keep that local
    // placeholder in sync so it cannot immediately override the newly selected
    // preferred model on the next render.
    setActiveConversation((current) => current ? { ...current, model: profileId } : current);
    if (activeConversationId) {
      void api.updateConversation(activeConversationId, { model: profileId }).then((conversation) => {
        setActiveConversation(conversation);
        void refreshConversations(conversation.id);
      }).catch(console.error);
    }
  };

  const handleProviderApiKeySave = async (providerId: string, value: string) => {
    await api.saveProviderApiKey(providerId, value);
    await refreshCatalog();
  };

  const handleThinkingLevelChange = (level: string | null) => {
    const key = profileKey(activeProfile, preferredModel);
    updateModelSettings({
      thinking_level: level ?? "medium",
      thinking_level_by_profile: {
        ...thinkingLevels,
        [key]: level,
      },
    });
  };

  const openSettingsSection = useCallback((sectionId: string) => {
    setRequestedSettingsSectionId(sectionId);
    setIsSettingsOpen(true);
  }, []);

  const openSettingsHome = useCallback(() => {
    setRequestedSettingsSectionId("quick_setup");
    setIsSettingsOpen(true);
  }, []);

  const actionApprovalMode: ActionApprovalMode = ultraYoloMode ? "full" : yoloMode ? "agent" : "ask";

  const setFullAccessEnabled = useCallback((enabled: boolean) => {
    const nextState = resolveUltraYoloModeState(
      {
        yoloMode,
        ultraYoloMode,
        restoreYoloMode: ultraYoloRestoreYoloMode,
      },
      enabled,
    );
    setYoloMode(nextState.yoloMode);
    setUltraYoloMode(nextState.ultraYoloMode);
    setUltraYoloRestoreYoloMode(nextState.restoreYoloMode);
  }, [setUltraYoloMode, setUltraYoloRestoreYoloMode, setYoloMode, ultraYoloMode, ultraYoloRestoreYoloMode, yoloMode]);

  const handleActionApprovalModeChange = useCallback((nextMode: ActionApprovalMode) => {
    if (nextMode === "custom") {
      openSettingsSection("tools");
      return;
    }
    if (nextMode === "full") {
      setFullAccessEnabled(true);
      return;
    }
    setUltraYoloMode(false);
    setUltraYoloRestoreYoloMode(false);
    setYoloMode(nextMode === "agent");
  }, [openSettingsSection, setFullAccessEnabled, setUltraYoloMode, setUltraYoloRestoreYoloMode, setYoloMode]);

  const handleSwitchToVisionModel = useCallback(() => {
    if (preferredVisionCandidate) {
      handleModelProfileSelect(preferredVisionCandidate.profile_id);
      return;
    }
    setError("Vision対応モデルが見つかりません。Model設定から追加してください。");
  }, [handleModelProfileSelect, preferredVisionCandidate]);

  const refreshSteerQueue = useCallback(async (conversationIdOverride?: string) => {
    const conversationId = conversationIdOverride ?? activeConversationId;
    if (!conversationId) {
      setSteerItems([]);
      return;
    }
    setModelSteerBusy(true);
    try {
      const result = await api.conversationSteer({
        action: "list",
        conversation_id: conversationId,
      });
      const items = "items" in result && Array.isArray(result.items) ? result.items : [];
      setSteerItems(items);
      const queuedCount = items.filter((item) => item.status === "queued").length;
      setModelSteerStatus(queuedCount ? {
        kind: "success",
        message: `${queuedCount}件のステアが待機中`,
      } : null);
    } catch (steerError) {
      setModelSteerStatus({
        kind: "error",
        message: steerError instanceof Error ? steerError.message : "Steer refresh failed",
      });
    } finally {
      setModelSteerBusy(false);
    }
  }, [activeConversationId]);

  const queueConversationSteer = useCallback(async (promptOverride?: string) => {
    const prompt = String(promptOverride ?? input).trim();
    if (!activeConversationId || !prompt) return;
    setModelSteerBusy(true);
    try {
      await api.conversationSteer({
        action: "enqueue",
        prompt,
        target_type: "conversation",
        target_id: activeConversationId,
        conversation_id: activeConversationId,
        visible: true,
        auto_send: true,
        metadata: {
          source: "composer_steer",
          live: isGenerating || isConversationPending,
        },
      });
      setInput("");
      setModelSteerStatus({
        kind: "success",
        message: isGenerating || isConversationPending ? "ステアを送りました" : "ステアを予約しました",
      });
      await refreshSteerQueue();
    } catch (steerError) {
      setModelSteerStatus({
        kind: "error",
        message: steerError instanceof Error ? steerError.message : "Steer queue failed",
      });
    } finally {
      setModelSteerBusy(false);
    }
  }, [activeConversationId, input, isConversationPending, isGenerating, refreshSteerQueue, setInput]);

  useEffect(() => {
    if (!activeConversationId) return;
    void refreshSteerQueue();
  }, [activeConversationId, refreshSteerQueue]);

  const handleComposerExtensionSelect = (item: ComposerExtensionItem) => {
    setActiveSidebarItemId(item.id);
    setSidebarSelectionTick((value) => value + 1);
    toggleSelectedTool(item);
  };

  const toggleSelectedTool = (item: ComposerExtensionItem) => {
    if (disabledToolIdSet.has(item.id)) {
      setError(`${item.label || item.id} は機能と接続の権限設定でブロックされています。`);
      return;
    }
    const semanticMentionToolIds = new Set(
      composerMentionToolIdsFromWidgets(droppedWidgets),
    );
    if (selectedToolIdSet.has(item.id) && semanticMentionToolIds.has(item.id)) {
      dismissedComposerMentionToolsRef.current.set(
        item.id,
        composerMentionSyntaxesForToolId(droppedWidgets, item.id),
      );
    } else if (!selectedToolIdSet.has(item.id)) {
      dismissedComposerMentionToolsRef.current.delete(item.id);
    }
    toolSelectionController.setTurnMode("manual");
    setStoredSelectedToolIds((current) => {
      if (current.includes(item.id)) {
        return current.filter((selectedId) => selectedId !== item.id);
      }
      return [...current, item.id];
    });
  };

  const runFrontendCommandAction = (
    action: string | undefined,
    command: ComposerCommandItem,
    args: Record<string, unknown>,
  ) => {
    switch (action) {
      case "open_model_picker": {
        const query = String(args.query ?? "").trim().toLowerCase();
        setComposerCandidateMenu(null);
        if (!query) {
          setModelPickerRequestId((value) => value + 1);
          return;
        }
        if (query) {
          const profile = selectableModelProfiles.find((item) => commandSearchText({
            id: item.profile_id,
            name: item.profile_id,
            aliases: [item.qualified_model_id ?? "", `${item.provider_id ?? ""}/${item.model_id ?? ""}`],
            label: item.display_name,
            description: item.provider_display_name,
            category: "model",
            visibility: "default",
            risk: "low",
            execution: { type: "frontend", action: "open_model_picker" },
          }).includes(query));
          if (profile) {
            handleModelProfileSelect(profile.profile_id);
          } else {
            setError(`"${query}" に一致する model が見つかりません。`);
          }
        }
        return;
      }
      case "set_fast_mode": {
        const enabled = parseCommandBoolean(args.enabled, true);
        if (!enabled) {
          handleThinkingLevelChange("medium");
          return;
        }
        const candidate = fastCandidateForProfile(activeProfile, selectableModelProfiles);
        if (!candidate) {
          setError("このモデルには fast 対応モデル/プロバイダーがありません。");
          return;
        }
        if (profileIdentity(candidate) !== profileIdentity(activeProfile)) {
          handleModelProfileSelect(candidate.profile_id);
        }
        if (candidate.supports_thinking) {
          const levels = candidate.thinking_levels?.length ? candidate.thinking_levels : ["low", "medium", "high"];
          if (levels.includes("low")) handleThinkingLevelChange("low");
        }
        return;
      }
      case "set_price_mode": {
        const tier = String(args.tier ?? "low").trim().toLowerCase() === "high" ? "high" : "low";
        const candidate = priceCandidateForProfile(activeProfile, selectableModelProfiles, tier);
        if (!candidate) {
          setError(`このモデルには price=${tier} の候補がありません。`);
          return;
        }
        if (profileIdentity(candidate) !== profileIdentity(activeProfile)) {
          handleModelProfileSelect(candidate.profile_id);
        }
        return;
      }
      case "new_conversation":
        handleNewTask();
        return;
      case "clear_composer_state":
        setInput("");
        cancelPendingMentionAttachments();
        setAttachedFiles([]);
        setDroppedWidgets([]);
        dismissedComposerMentionToolsRef.current.clear();
        setComposerEntityReferences([]);
        if (activeConversationId) {
          forgetPendingRequest(activeConversationId);
          replaceChatIdInUrl(activeConversationId, false);
        }
        return;
      case "set_home_title": {
        const requestedTitle = String(args.value ?? "").replace(/\s+/g, " ").trim();
        if (!requestedTitle) {
          transientAlertSequenceRef.current += 1;
          setTransientAlert({
            id: String(transientAlertSequenceRef.current),
            message: `現在のホームタイトル: ${normalizeComposerHomeTitle(customHomeTitle)}`,
            tone: "info" satisfies TransientAlertTone,
          });
          return;
        }
        const nextTitle = normalizeComposerHomeTitle(requestedTitle);
        setCustomHomeTitle(nextTitle);
        transientAlertSequenceRef.current += 1;
        setTransientAlert({
          id: String(transientAlertSequenceRef.current),
          message: nextTitle === DEFAULT_COMPOSER_HOME_TITLE
            ? "ホームタイトルをTobkiriへ戻しました。"
            : `ホームタイトルを「${nextTitle}」へ変更しました。`,
          tone: "success",
        });
        return;
      }
      case "set_mode_coding":
        handleModeChange(mode === "coding" ? "agent" : "coding");
        return;
      case "set_mode_chat":
        handleModeChange("agent");
        return;
      case "set_mode_agent":
        handleModeChange("agent");
        return;
      case "toggle_yolo":
      case "toggle_ultra_yolo": {
        setFullAccessEnabled(parseCommandBoolean(args.enabled, !ultraYoloMode));
        return;
      }
      case "open_tool_picker": {
        const query = String(args.query ?? "").trim().toLowerCase();
        if (query) {
          const item = composerExtensions.find((candidate) => (
            `${candidate.id} ${candidate.label} ${candidate.description ?? ""}`.toLowerCase().includes(query)
          ));
          if (item) {
            handleComposerExtensionSelect(item);
          } else {
            setError(`"${query}" に一致する tool が見つかりません。`);
          }
        }
        return;
      }
      case "show_status":
        setError(
          `status: mode=${mode}, model=${activeProfile?.display_name ?? preferredModel}, thinking=${selectedThinkingLevel}, deepthink=${deepthinkEnabled ? "on" : "off"}, yolo=${yoloMode ? "on" : "off"}, ultra_yolo=${ultraYoloMode ? "on" : "off"}, tools=${selectedTools.length}`,
        );
        return;
      case "open_context_viewer":
      case "show_usage":
        setActiveSidebarItemId("__context_usage__");
        setSidebarSelectionTick((value) => value + 1);
        return;
      case "open_settings":
      case "open_permissions":
      case "open_theme_settings":
      case "open_keymap_settings":
        if (action === "open_settings" && args.section) {
          const requested = String(args.section).trim().toLowerCase();
          const matchedSection = settingsSections.find((section) => (
            section.id.toLowerCase() === requested
            || section.label.toLowerCase() === requested
          ));
          setRequestedSettingsSectionId(matchedSection?.id ?? requested);
        } else if (action === "open_permissions") {
          setRequestedSettingsSectionId("permissions");
        } else if (action === "open_theme_settings") {
          setRequestedSettingsSectionId("theme");
        } else if (action === "open_keymap_settings") {
          setRequestedSettingsSectionId("keymap");
        } else {
          setRequestedSettingsSectionId("quick_setup");
        }
        setIsSettingsOpen(true);
        return;
      case "open_command_help":
        setError(composerCommands.map((item) => `/${item.name}: ${item.description ?? item.label}`).join("\n"));
        return;
      case "open_diff_preview":
        handleModeChange("coding");
        setInput("Preview the current git diff.");
        return;
      case "start_review":
        handleModeChange("coding");
        setInput("Review the current diff and call out bugs, risks, and missing tests.");
        return;
      case "open_branch_picker":
        handleModeChange("coding");
        if (args.name) setInput(`Create or switch to branch ${String(args.name)}.`);
        return;
      case "prepare_test_run":
        handleModeChange("coding");
        setInput(args.target ? `Run tests for ${String(args.target)}.` : "Run the recommended tests.");
        return;
      case "prepare_lint_run":
        handleModeChange("coding");
        setInput("Run lint and formatting checks.");
        return;
      case "open_file_search":
        handleModeChange("coding");
        if (args.query) setInput(`Find workspace files matching ${String(args.query)}.`);
        return;
      case "open_history":
        setIsHistoryMinimized(false);
        return;
      case "export_conversation":
        if (!activeConversationId) {
          setError("エクスポートする会話がありません。");
          return;
        }
        void handlePanelAction(
          {} as SidebarItem,
          { id: "conversation.export" } as SidebarAction,
        );
        return;
      case "fork_conversation":
        if (!activeConversationId) {
          setError("forkする会話がありません。");
          return;
        }
        void api.createConversation({
          model: preferredModel || "stub/default",
          parent_conversation_id: activeConversationId,
          metadata: { forked_from: activeConversationId },
        }).then((conversation) => {
          setActiveConversationId(conversation.id);
          void loadConversation(conversation.id, false);
          void refreshConversations(conversation.id);
        }).catch((forkError) => {
          setError(forkError instanceof Error ? forkError.message : "会話のforkに失敗しました。");
        });
        return;
      case "resume_conversation":
        if (activeConversationId) {
          void loadConversation(activeConversationId, false);
          return;
        }
        setIsHistoryMinimized(false);
        setError("履歴から再開する会話を選択してください。");
        return;
      case "rename_conversation": {
        const title = String(args.title ?? "").replace(/\s+/g, " ").trim();
        if (!activeConversationId || !title) {
          setError("現在の会話と新しいtitleを指定してください。");
          return;
        }
        void api.updateConversation(activeConversationId, { title }).then((conversation) => {
          setActiveConversation(conversation);
          void refreshConversations(conversation.id);
        }).catch((renameError) => {
          setError(renameError instanceof Error ? renameError.message : "会話名の変更に失敗しました。");
        });
        return;
      }
      case "open_memory_inspector":
        setActiveSidebarItemId("__context_usage__");
        setSidebarSelectionTick((value) => value + 1);
        return;
      case "open_approvals":
        setRequestedSettingsSectionId("permissions");
        setIsSettingsOpen(true);
        return;
      case "open_debug":
      case "open_logs":
      case "show_raw":
        pushActionPreview(
          { id: `command.${action}`, label: command.label, icon: "terminal" },
          command.label,
          action === "show_raw"
            ? activeConversation
            : {
                mode,
                conversation_id: activeConversationId,
                pending_request: activeConversationId ? pendingRequests[activeConversationId] ?? null : null,
                error,
              },
        );
        return;
      case "run_doctor":
        void api.health().then((health) => {
          pushActionPreview(
            { id: "command.doctor", label: "Doctor", icon: "activity" },
            "Tobkiri diagnostics",
            health,
          );
        }).catch((doctorError) => {
          setError(doctorError instanceof Error ? doctorError.message : "diagnosticsの実行に失敗しました。");
        });
        return;
      case "open_plugins":
      case "open_mcp":
      case "open_skills":
      case "open_hooks": {
        const keyword = action.replace(/^open_/, "").replace(/s$/, "");
        const panel = composerExtensions.find((item) => (
          `${item.id} ${item.label}`.toLowerCase().includes(keyword)
        ));
        if (panel) {
          setActiveSidebarItemId(panel.id);
          setSidebarSelectionTick((value) => value + 1);
          return;
        }
        setActiveSidebarItemId("__tool_manager__");
        setSidebarSelectionTick((value) => value + 1);
        return;
      }
      case "request_commit_approval":
        handleModeChange("coding");
        setInput("Commit the reviewed workspace changes.");
        return;
      case "request_push_approval":
        handleModeChange("coding");
        setInput("Push the reviewed current branch.");
        return;
      case "request_terminal_approval":
        handleModeChange("coding");
        setInput("Run the approved terminal command.");
        return;
      case "request_patch_approval":
        handleModeChange("coding");
        setInput("Apply the approved workspace patch.");
        return;
      case "request_restore_approval":
        handleModeChange("coding");
        setInput("Restore the approved workspace checkpoint.");
        return;
      default:
        if (command.risk === "high") {
          setError(`/${command.name} は high risk command のため approval center 経由で実行してください。`);
          return;
        }
        setError(`/${command.name} は現在のFrontendに実行handlerがないため利用できません。`);
    }
  };

  const applyAuthoritativeCommandState = (result: ComposerCommandExecuteResult): string[] => {
    const applied = applyCommandStateSnapshots(
      settingsValuesRef.current,
      commandStateRevisionsRef.current,
      result.state_changes,
    );
    commandStateRevisionsRef.current = applied.revisions;
    if (applied.values !== settingsValuesRef.current) {
      settingsDirtyKeysRef.current = settingsDirtyKeysRef.current.filter(
        (dirtyKey) => !applied.appliedPaths.includes(dirtyKey),
      );
      applySettingsValues(applied.values);
    }
    return applied.appliedPaths;
  };

  const followCommandProgress = async (invocationId: string) => {
    try {
      for await (const event of api.streamCommandInvocationEvents(invocationId)) {
        setCommandProgressEvents((current) => [...current, event].slice(-12));
      }
    } catch (streamError) {
      if (streamError instanceof DOMException && streamError.name === "AbortError") return;
      setError(streamError instanceof Error ? streamError.message : "Command progress stream failed.");
    }
  };

  const executeComposerCommand = async (commandId: string, rawInput = `/${commandId}`): Promise<boolean | void> => {
    const parsed = parseSlashCommandInput(rawInput, effectiveCommandCatalog) ?? {
      command: effectiveCommandCatalog.find((command) => command.id === commandId || command.name === commandId),
      args: {},
      raw: rawInput,
    };
    if (!parsed.command) {
      setError(`/${commandId} は未登録の command です。`);
      return;
    }
    try {
      setError(null);
      const highRiskRef = highRiskCommandRef(parsed.command);
      if (highRiskRef) {
        if (highRiskPrepareInFlightRef.current || pendingHighRiskCommand) {
          setError("高リスク操作の承認がすでに保留中です。先にその操作を完了または拒否してください。");
          return;
        }
        const invocationId = createCommandInvocationId(highRiskRef);
        const commandArgs = { ...parsed.args };
        highRiskPrepareInFlightRef.current = true;
        try {
          const prepared = await api.prepareHighRiskCommand({
            invocation_id: invocationId,
            command_ref: highRiskRef,
            arguments: highRiskPrepareArguments(highRiskRef, commandArgs, {
              workspaceId: effectiveWorkspaceId,
              currentBranch: codingContext?.branch,
            }),
            presentation: {
              title: parsed.command.label,
              summary: parsed.command.description ?? `${parsed.command.label} を実行します。`,
            },
          });
          if (
            prepared.invocation_id !== invocationId
            || !prepared.approval_request_id
            || prepared.state !== "approval_pending"
          ) {
            throw new Error("高リスク操作の承認準備に失敗しました。");
          }
          setPendingHighRiskCommand({
            requestId: prepared.approval_request_id,
            invocationId,
            commandLabel: parsed.command.label,
          });
          highRiskApprovalWindowOpenedRequestRef.current = prepared.approval_request_id;
          const opened = await openAuthorityApprovalWindow(prepared.approval_request_id);
          if (!opened) {
            setError("専用の承認ウィンドウを開けませんでした。下の承認待ち表示から再試行してください。");
          }
          return true;
        } finally {
          highRiskPrepareInFlightRef.current = false;
        }
      }
      if (isRegisteredSlashCommand(parsed.command) && !parsed.command.canonical_id) {
        const frontendAction = parsed.command.execution.type === "frontend" ? parsed.command.execution.action : undefined;
        runFrontendCommandAction(frontendAction, parsed.command, parsed.args);
        return true;
      }
      const commandArgs = { ...parsed.args };
      if (parsed.command.id === "think" && commandArgs.level && activeProfile) {
        commandArgs.scope = "profile";
        commandArgs.profile_id = profileKey(activeProfile, preferredModel);
      }
      const isDeepthinkMutation = parsed.command.protocol_execution?.kind === "state_mutation"
        ? parsed.command.protocol_execution.state_ref === "defaultspack:models.deepthink_enabled"
        : parsed.command.id === "deepthink" && parsed.command.execution.type === "rumi_function";
      const resolvedCommandName = parsed.command.canonical_id ?? parsed.command.name ?? parsed.command.id;
      let result: ComposerCommandExecuteResult;
      if (isDeepthinkMutation) {
        const desired = Object.prototype.hasOwnProperty.call(commandArgs, "enabled")
          ? parseCommandBoolean(commandArgs.enabled, !deepthinkDesiredStateRef.current)
          : !deepthinkDesiredStateRef.current;
        deepthinkDesiredStateRef.current = desired;
        commandArgs.enabled = desired;
        const invocationId = createCommandInvocationId("deepthink");
        void followCommandProgress(invocationId);
        const clientSequence = ++commandClientSequenceRef.current;
        deepthinkPendingCountRef.current += 1;
        const executeMutation = () => {
          const expectedRevision = commandStateRevisionsRef.current[
            "defaultspack:models.deepthink_enabled"
          ];
          return api.executeResolvedUiCommand({
            command: resolvedCommandName,
            args: commandArgs,
            conversation_id: activeConversationId,
            mode: mode as ComposerCommandMode,
            invocation_id: invocationId,
            idempotency_key: invocationId,
            client_sequence: clientSequence,
            expected_revision: Number.isInteger(expectedRevision) ? expectedRevision : undefined,
          });
        };
        const queued = deepthinkMutationQueueRef.current
          .catch(() => undefined)
          .then(executeMutation);
        deepthinkMutationQueueRef.current = queued.then(() => undefined, () => undefined);
        try {
          result = await queued;
        } finally {
          deepthinkPendingCountRef.current = Math.max(0, deepthinkPendingCountRef.current - 1);
        }
      } else {
        const invocationId = createCommandInvocationId(parsed.command.id);
        void followCommandProgress(invocationId);
        result = await api.executeResolvedUiCommand({
          command: resolvedCommandName,
          args: commandArgs,
          conversation_id: activeConversationId,
          mode: mode as ComposerCommandMode,
          invocation_id: invocationId,
        });
      }
      const appliedStatePaths = applyAuthoritativeCommandState(result);
      const feedbackMessage = composerCommandResultMessage(result);
      if (result.requires_approval) {
        if (result.approval_request_id && result.operation_id) {
          setPendingCommandApproval({
            requestId: result.approval_request_id,
            invocationId: result.operation_id,
            commandRef: resolvedCommandName,
            command: parsed.command,
            args: commandArgs,
            conversationId: activeConversationId,
            mode: mode as ComposerCommandMode,
            approvalKind: result.approval_kind === "authority"
              ? "authority"
              : "coding",
          });
        }
        setError(feedbackMessage ?? `/${parsed.command.name} は approval center 経由で実行してください。`);
        return;
      }
      if (isModelCommand(parsed.command)) {
        if (result.action === "show_model_candidates") {
          setComposerCandidateMenu({
            mode: "model",
            query: String(result.args?.query ?? commandArgs.query ?? "").trim(),
            candidates: Array.isArray(result.candidates) ? result.candidates : [],
          });
          if (feedbackMessage) setError(feedbackMessage);
          return false;
        }
        if (result.action === "open_model_picker") {
          setComposerCandidateMenu(null);
          setModelPickerRequestId((value) => value + 1);
          if (feedbackMessage) setError(feedbackMessage);
          return true;
        }
        if (result.executed) {
          const selectedProfileId = selectedModelProfileId(result.selected_model);
          setComposerCandidateMenu(null);
          setInput("");
          if (feedbackMessage) setError(feedbackMessage);
          await refreshCatalog();
          if (activeConversationId && selectedProfileId) {
            const conversation = await api.updateConversation(activeConversationId, { model: selectedProfileId });
            setActiveConversation(conversation);
            await refreshConversations(conversation.id);
          } else if (activeConversationId) {
            await refreshConversations(activeConversationId);
          }
          return true;
        }
      }

      if (result.action || parsed.command.execution.type === "frontend") {
        const frontendAction = parsed.command.execution.type === "frontend" ? parsed.command.execution.action : undefined;
        runFrontendCommandAction(
          result.action ?? frontendAction,
          parsed.command,
          resolvedFrontendCommandArgs(parsed.command, parsed.args, result.args),
        );
      }
      if (parsed.command.execution.type === "rumi_function" && appliedStatePaths.length === 0) {
        await refreshCatalog();
      }
      if (feedbackMessage) {
        const tone = composerCommandFeedbackTone(result);
        if (tone === "error") {
          setError(feedbackMessage);
        } else {
          transientAlertSequenceRef.current += 1;
          setTransientAlert({
            id: String(transientAlertSequenceRef.current),
            message: feedbackMessage,
            tone,
          });
        }
      }
    } catch (commandError) {
      setError(commandError instanceof Error ? commandError.message : "command execution に失敗しました。");
    }
  };

  const handleComposerCommand = (commandId: string, rawInput?: string) => {
    if (!slashCommandsEnabled) return;
    void executeComposerCommand(commandId, rawInput);
  };

  const handleModelCommandCandidateSelect = (candidate: ModelCommandCandidate) => {
    const profileId = modelCandidateProfileId(candidate);
    if (!profileId) {
      setError("Selected model candidate is missing a profile id.");
      return;
    }
    void executeComposerCommand("model", `/model ${profileId}`);
  };

  const handleComposerInputChange = (value: string) => {
    if (value !== input) {
      for (const [toolId, syntaxes] of dismissedComposerMentionToolsRef.current) {
        if (!syntaxes.some((syntax) => hasUnescapedMentionSyntax(value, syntax))) {
          dismissedComposerMentionToolsRef.current.delete(toolId);
        }
      }
    }
    setInput(value);
    if (isGenerating || isConversationPending) {
      setComposerCandidateMenu(null);
      return;
    }
    const modelQuery = modelCommandInputQuery(value);
    if (composerCandidateMenu && modelQuery !== composerCandidateMenu.query) {
      setComposerCandidateMenu(null);
    }
  };

  const handleModeChange = (newMode: AppMode, updateRoute = true) => {
    if (newMode !== "coding") cancelPendingMentionAttachments();
    setMode(newMode);
    if (!updateRoute) return;
    if (newMode === "coding" && window.location.pathname !== "/coding") {
      const url = new URL(window.location.href);
      url.pathname = "/coding";
      window.history.pushState({ mode: "coding", conversationId: activeConversationId }, "", `${url.pathname}${url.search}${url.hash}`);
    } else if (newMode !== "coding" && window.location.pathname === "/coding") {
      const url = new URL(window.location.href);
      url.pathname = "/chat";
      if (activeConversationId) url.searchParams.set("chat", activeConversationId);
      else url.searchParams.delete("chat");
      url.searchParams.delete("pending");
      window.history.pushState({ mode: newMode, conversationId: activeConversationId }, "", `${url.pathname}${url.search}${url.hash}`);
    }
  };

  const activateWorkspaceTab = (tab: WorkspaceTab) => {
    setActiveWorkspaceTabId(tab.id);
    setError(null);
    if (tab.kind === "chat") {
      handleModeChange("agent", false);
      pushWorkspaceRoute("chat", tab.conversationId ?? null);
      void loadConversation(tab.conversationId ?? null, false);
      return;
    }
    if (tab.kind === "coding") {
      handleModeChange("coding", false);
      pushWorkspaceRoute("coding", activeConversationId);
      return;
    }
    handleModeChange("agent", false);
    pushWorkspaceRoute(tab.kind, activeConversationId);
    if (tab.kind === "calendar" || tab.kind === "kanban") {
      return;
    }
    if (tab.kind === "canvas") {
      setShowPreview(true);
    }
    if (tab.kind === "tools") {
      setActiveSidebarItemId("__tool_manager__");
      setSidebarSelectionTick((value) => value + 1);
    }
  };

  const handleWorkspaceTabSelect = (tabId: string) => {
    const tab = workspaceTabs.find((candidate) => candidate.id === tabId);
    if (tab) activateWorkspaceTab(tab);
  };

  const handleWorkspaceTabCreate = (kind: WorkspaceTabKind) => {
    const option = WORKSPACE_TAB_CREATE_OPTIONS.find((candidate) => candidate.kind === kind);
    if (option?.disabled) return;
    const tab = createWorkspaceTab(kind, {
      title: kind === "chat" ? "New Conversation" : option?.label,
    });
    setWorkspaceTabs((current) => [...current, tab]);
    activateWorkspaceTab(tab);
  };

  const handleWorkspaceTabClose = (tabId: string) => {
    if (workspaceTabs.length <= 1) return;
    const closedIndex = workspaceTabs.findIndex((tab) => tab.id === tabId);
    const nextTabs = workspaceTabs.filter((tab) => tab.id !== tabId);
    setWorkspaceTabs(nextTabs);
    if (activeWorkspaceTabId === tabId) {
      const nextTab = nextTabs[Math.max(0, closedIndex - 1)] ?? nextTabs[0];
      if (nextTab) activateWorkspaceTab(nextTab);
    }
  };

  const handleCodingBranchSwitch = (branch: string, create = false) => {
    void api.switchGitBranch(branch, create, { workspace_id: effectiveWorkspaceId })
      .then(() => loadCodingContext())
      .catch((branchError) => setError(branchError instanceof Error ? branchError.message : "ブランチ切り替えに失敗しました。"));
  };

  const handleCodingDirectoryChange = (directory: string) => {
    setCodingDirectory(directory || ".");
  };

  const handleFileAttach = (files: AttachedFile[]) => {
    setAttachedFiles((prev) => [...prev, ...files]);
  };

  const handleWorkspaceFileDragEnter = (event: ReactDragEvent<HTMLElement>) => {
    if (!isChatWorkspace || !event.dataTransfer.types.includes("Files")) return;
    event.preventDefault();
    workspaceFileDragDepthRef.current += 1;
    setIsWorkspaceFileDragActive(true);
  };

  const handleWorkspaceFileDragOver = (event: ReactDragEvent<HTMLElement>) => {
    if (!isChatWorkspace || !event.dataTransfer.types.includes("Files")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  };

  const handleWorkspaceFileDragLeave = (event: ReactDragEvent<HTMLElement>) => {
    if (!event.dataTransfer.types.includes("Files")) return;
    workspaceFileDragDepthRef.current = Math.max(0, workspaceFileDragDepthRef.current - 1);
    if (workspaceFileDragDepthRef.current === 0) setIsWorkspaceFileDragActive(false);
  };

  const handleWorkspaceFileDrop = (event: ReactDragEvent<HTMLElement>) => {
    workspaceFileDragDepthRef.current = 0;
    setIsWorkspaceFileDragActive(false);
    if (event.defaultPrevented || event.dataTransfer.files.length === 0) return;
    event.preventDefault();
    void Promise.all(Array.from(event.dataTransfer.files).map(fileToAttachment))
      .then(handleFileAttach)
      .catch((attachmentError) => {
        setError(attachmentError instanceof Error ? attachmentError.message : "ファイルを添付できませんでした。");
      });
  };

  const handleAtFileAttach = (path: string) => {
    const normalizedPath = path.trim();
    if (mode !== "coding" || !normalizedPath) return;
    if (hasWorkspaceAttachment(attachedFiles, normalizedPath)) return;
    if (pendingMentionAttachmentRequestsRef.current.has(normalizedPath)) return;

    const request: PendingMentionAttachmentRequest = {
      generation: composerDraftGenerationRef.current,
      token: mentionAttachmentTokenRef.current + 1,
    };
    mentionAttachmentTokenRef.current = request.token;
    pendingMentionAttachmentRequestsRef.current.set(normalizedPath, request);
    syncPendingMentionAttachmentPaths();

    void api.readWorkspaceFile(normalizedPath, {
      workspace_id: effectiveWorkspaceId,
    })
      .then((result) => {
        const currentRequest = pendingMentionAttachmentRequestsRef.current.get(
          normalizedPath,
        );
        if (
          !currentRequest
          || currentRequest.token !== request.token
          || currentRequest.generation !== request.generation
          || composerDraftGenerationRef.current !== request.generation
        ) return;
        setAttachedFiles((prev) => {
          if (hasWorkspaceAttachment(prev, normalizedPath)) return prev;
          return [
            ...prev,
            workspaceFileToAttachment(
              result.path || normalizedPath,
              result.content,
              result.size,
            ),
          ];
        });
      })
      .catch((readError) => {
        const currentRequest = pendingMentionAttachmentRequestsRef.current.get(
          normalizedPath,
        );
        if (currentRequest?.token !== request.token) return;
        setError(
          readError instanceof Error
            ? readError.message
            : "workspace file の添付に失敗しました。",
        );
      })
      .finally(() => {
        const currentRequest = pendingMentionAttachmentRequestsRef.current.get(
          normalizedPath,
        );
        if (currentRequest?.token !== request.token) return;
        pendingMentionAttachmentRequestsRef.current.delete(normalizedPath);
        syncPendingMentionAttachmentPaths();
      });
  };

  const handlePendingMentionAttachmentRemove = (path: string) => {
    cancelPendingMentionAttachments(path);
    const reconciled = reconcileComposerSemanticDraft({
      attachmentPaths: semanticAttachmentPathsIncludingPending(attachedFiles),
      droppedWidgets,
      requireFileAttachment: true,
      selectedToolIds,
      text: input,
    });
    setDroppedWidgets(reconciled.droppedWidgets);
    setStoredSelectedToolIds(reconciled.selectedToolIds);
  };

  const handleCodingWorkspaceSelect = (workspaceId: string) => {
    cancelPendingMentionAttachments();
    handleModeChange("coding");
    setSelectedCodingWorkspaceId(workspaceId);
    void api.selectCodingWorkspace(workspaceId)
      .then((selected) => loadCodingWorkspaces().then(() => loadCodingContext(selected.selected_workspace_id ?? workspaceId)))
      .catch((workspaceError) => setError(workspaceError instanceof Error ? workspaceError.message : "workspace selection failed."));
  };

  const handleCodingWorkspaceTrust = (workspaceId: string) => {
    void api.trustCodingWorkspace(workspaceId)
      .then(() => loadCodingWorkspaces())
      .then(() => loadCodingContext(workspaceId))
      .catch((workspaceError) => setError(workspaceError instanceof Error ? workspaceError.message : "workspace trust failed."));
  };

  const handleCodingWorkspaceCreate = async (rootPathOverride?: string) => {
    const rootPath = rootPathOverride?.trim() || codingContext?.rootFolder;
    if (!rootPath) {
      setError("Current coding context has no workspace root to add.");
      return null;
    }
    try {
      const created = await api.createCodingWorkspace({ root_path: rootPath, trusted: false });
      const selected = await api.selectCodingWorkspace(created.workspace.workspace_id);
      setSelectedCodingWorkspaceId(selected.selected_workspace_id);
      await loadCodingWorkspaces();
      await loadCodingContext(selected.selected_workspace_id);
      return created.workspace;
    } catch (workspaceError) {
      setError(workspaceError instanceof Error ? workspaceError.message : "workspace creation failed.");
      throw workspaceError;
    }
  };

  const handleDirectorySelect = async () => {
    const selected = await api.selectDirectory("Project に紐づける既存フォルダを選択");
    return selected.cancelled ? null : selected.path;
  };

  const handleCodingWorkspacePickCreate = async () => {
    const selected = await handleDirectorySelect();
    if (!selected) return null;
    return handleCodingWorkspaceCreate(selected);
  };

  const handlePrepareChatGroupStorage = async (rootPath: string) => {
    const prepared = await api.prepareChatGroupStorage(rootPath);
    return {
      rootPath: prepared.root_path,
      rumiDataPath: prepared.rumi_data_path,
    };
  };

  const handleFileRemove = (fileId: string) => {
    const remainingFiles = attachedFiles.filter((file) => file.id !== fileId);
    const reconciled = reconcileComposerSemanticDraft({
      attachmentPaths: semanticAttachmentPathsIncludingPending(remainingFiles),
      droppedWidgets,
      requireFileAttachment: true,
      selectedToolIds,
      text: input,
    });
    setAttachedFiles(remainingFiles);
    setDroppedWidgets(reconciled.droppedWidgets);
    setStoredSelectedToolIds(reconciled.selectedToolIds);
  };

  const handleDropWidget = (widget: DroppedWidget) => {
    const ownedWidget = withComposerMentionSelectionOwnership(widget, selectedToolIds);
    for (const toolId of composerMentionToolIdsFromWidgets([ownedWidget])) {
      dismissedComposerMentionToolsRef.current.delete(toolId);
    }
    setDroppedWidgets((prev) => {
      if (prev.some((w) => w.id === ownedWidget.id)) return prev;
      return [...prev, { ...ownedWidget, enabled: ownedWidget.enabled ?? true }];
    });
    if ((ownedWidget.widgetKind === "tool_toggle" || ownedWidget.type === "tool") && ownedWidget.enabled !== false) {
      const toolId = ownedWidget.sourceItemId || ownedWidget.id;
      const item = composerExtensions.find((candidate) => candidate.id === toolId);
      if (item) {
        toolSelectionController.setTurnMode("manual");
        setStoredSelectedToolIds((current) => current.includes(item.id) ? current : [...current, item.id]);
      }
    }
    if (ownedWidget.type === "service" && ownedWidget.metadata?.source === "composer_at_mention") {
      const serviceId = ownedWidget.sourceItemId || ownedWidget.id.replace(/^mention-service:/, "");
      const serviceToolIds = composerExtensions
        .filter((item) => !item.disabled && toolGroupFor(item).id === serviceId)
        .map((item) => item.id);
      if (serviceToolIds.length > 0) {
        toolSelectionController.setTurnMode("manual");
        setStoredSelectedToolIds((current) => [...new Set([...current, ...serviceToolIds])]);
      }
    }
  };

  const handleWidgetToggle = (widgetId: string) => {
    const widget = activeDroppedWidgets.find((candidate) => candidate.id === widgetId);
    if (widget?.widgetKind === "tool_toggle" || widget?.type === "tool") {
      const toolId = widget.sourceItemId || widgetId;
      const item = composerExtensions.find((candidate) => candidate.id === toolId);
      if (item) {
        toggleSelectedTool(item);
        return;
      }
    }
    setDroppedWidgets((prev) => prev.map((w) => (w.id === widgetId ? { ...w, enabled: !w.enabled } : w)));
  };

  const handleToolBatchSet = (toolIds: string[], enabled: boolean) => {
    const validIds = new Set(composerExtensions.map((tool) => tool.id));
    const requestedIds = [...new Set(toolIds.filter((toolId) => validIds.has(toolId)))];
    if (requestedIds.length === 0) return;
    const semanticMentionToolIds = new Set(
      composerMentionToolIdsFromWidgets(droppedWidgets),
    );
    for (const toolId of requestedIds) {
      if (enabled) dismissedComposerMentionToolsRef.current.delete(toolId);
      else if (semanticMentionToolIds.has(toolId)) {
        dismissedComposerMentionToolsRef.current.set(
          toolId,
          composerMentionSyntaxesForToolId(droppedWidgets, toolId),
        );
      }
    }
    toolSelectionController.setTurnMode("manual");
    setStoredSelectedToolIds((current) => {
      if (enabled) return [...new Set([...current, ...requestedIds])];
      const requestedIdSet = new Set(requestedIds);
      return current.filter((toolId) => !requestedIdSet.has(toolId));
    });
  };

  const handleToolSelectionTargetRemove = (target: ToolTarget) => {
    if (
      target.kind === "tool"
      && composerMentionToolIdsFromWidgets(droppedWidgets).includes(target.id)
    ) {
      dismissedComposerMentionToolsRef.current.set(
        target.id,
        composerMentionSyntaxesForToolId(droppedWidgets, target.id),
      );
    }
    toolSelectionController.removeTarget(target);
  };

  const handleComposerEndpointAction = async (widget: DroppedWidget, action: Extract<ComposerWidgetAction, { type: "call_endpoint" }>) => {
    if (!canExecuteComposerEndpointAction(action)) {
      setError("この widget action は安全な /api/ endpoint ではないか、承認が必要なため直接実行できません。");
      return;
    }

    const method = (action.method ?? "GET").toUpperCase();
    const result = await defaultspackApiFetch(defaultspackContractRoute(action.endpoint), {
      method,
      body: method === "GET" ? undefined : JSON.stringify(action.payload ?? {}),
    }).then((response) => response.json());

    if (action.result_surface === "silent") return;
    pushActionPreview(
      { id: `composer.${widget.id}`, label: widget.label, icon: widget.icon },
      widget.label,
      result,
    );
  };

  const handleWidgetAction = (widget: DroppedWidget) => {
    const trustedAction = trustedComposerActionForWidget(widget, composerExtensions);
    const action = trustedAction ?? (widget.action?.type === "call_endpoint" ? undefined : widget.action);

    if (!action) {
      const target = widget.sourceItemId || widget.id;
      setActiveSidebarItemId(target);
      setSidebarSelectionTick((value) => value + 1);
      return;
    }

    if (action.type === "open_panel") {
      const target = action.target_item_id || widget.sourceItemId || widget.id;
      setActiveSidebarItemId(target);
      setSidebarSelectionTick((value) => value + 1);
      return;
    }

    if (action.type === "toggle_tool") {
      const toolId = action.tool_id || widget.sourceItemId || widget.id;
      const item = composerExtensions.find((candidate) => candidate.id === toolId);
      if (item) toggleSelectedTool(item);
      return;
    }

    if (action.type === "select_model") {
      if (action.profile_id) handleModelProfileSelect(action.profile_id);
      return;
    }

    if (action.type === "call_endpoint") {
      setError(null);
      void handleComposerEndpointAction(widget, action).catch((actionError) => {
        setError(actionError instanceof Error ? actionError.message : "composer widget action に失敗しました。");
      });
    }
  };

  const settleBrowserApproval = (approval: BrowserApproval) => {
    const settlementKey = browserApprovalSettlementKey(approval);
    setSettledBrowserApprovalKeys((keys) => (
      keys.includes(settlementKey) ? keys : [...keys, settlementKey].slice(-50)
    ));
    if (approval.requestId) {
      setSettledRuntimeApprovalIds((ids) => (
        ids.includes(approval.requestId ?? "")
          ? ids
          : [...ids, approval.requestId ?? ""].filter(Boolean).slice(-50)
      ));
    }
  };

  const approveBrowserAction = async () => {
    if (!browserApproval) return;
    if (!activeConversationId) return;
    const currentApproval = browserApproval;
    setError(null);
    setIsGenerating(true);
    const approvalToolIds = selectedToolIds.length
      ? selectedToolIds
      : [currentApproval.toolName].filter(Boolean);
    rememberPendingRequest({
      conversationId: activeConversationId,
      startedAt: Date.now(),
      status: "ユーザー承認をAIへ伝えています",
      toolNames: approvalToolIds,
    });
    try {
      const approvalWorkspace = workspaceContextFromConversation(activeConversation);
      let approvalToken = currentApproval.token ?? "";
      if (currentApproval.requestId) {
        const decision = await api.approveCodingApproval(currentApproval.requestId);
        if (!decision.approved) {
          throw new Error(decision.reason || "approval failed");
        }
        approvalToken = decision.token ?? "";
        settleBrowserApproval(currentApproval);
      }
      await api.streamMessage(activeConversationId, "ユーザーが許可しました。承認済みの操作を踏まえて続行してください。", {
        tool_choice: "required",
        tool_policy: {
          ...templatePolicyReferencePayload,
          action_approval_mode: actionApprovalMode,
          // Delegated approval is reviewed server-side; only full access uses yolo.
          ...(ultraYoloMode ? { yolo_mode: true, allow_shell: true, allow_file_write: true, write_actions_require_approval: false } : {}),
          ...(ultraYoloMode ? { full_access: true } : {}),
          ...(approvalWorkspace.workspaceId ? { workspace_id: approvalWorkspace.workspaceId } : {}),
          ...(effectiveDisabledToolIds.length ? { disabled_tools: effectiveDisabledToolIds } : {}),
          ...(approvalToolIds.length ? { selected_tools: approvalToolIds } : {}),
        },
        tools: approvalToolIds.length ? approvalToolIds : undefined,
        metadata: {
          mode,
          ...(approvalWorkspace.workspaceId ? {
            workspace_id: approvalWorkspace.workspaceId,
            workspace_label: approvalWorkspace.workspaceLabel,
            workspace_root: approvalWorkspace.workspaceRoot,
          } : {}),
          approval_followup: {
            action: currentApproval.action,
            operation: currentApproval.action,
            approval_token: approvalToken,
            payload: currentApproval.payload,
            request_id: currentApproval.requestId,
            tool_call_id: currentApproval.toolCallId,
            tool_name: currentApproval.toolName,
          },
          runtime_content: browserApprovalRuntimeContent(currentApproval, approvalToken),
          selected_tools: approvalToolIds,
        },
      });
      forgetPendingRequest(activeConversationId);
      replaceChatIdInUrl(activeConversationId, false);
      await loadConversation(activeConversationId, false);
      await refreshConversations(activeConversationId);
    } catch (approvalError) {
      forgetPendingRequest(activeConversationId);
      const staleMessage = currentApproval.requestId ? approvalStaleUiMessage(approvalError) : null;
      if (staleMessage) {
        settleBrowserApproval(currentApproval);
        setError(staleMessage);
      } else {
        console.error(approvalError);
        setError("許可を保存できませんでした。リクエストの状態を更新して再試行してください。");
      }
    } finally {
      setIsGenerating(false);
    }
  };

  const denyBrowserAction = async () => {
    if (!browserApproval) return;
    const currentApproval = browserApproval;
    const actionKey = browserApprovalSettlementKey(currentApproval);
    if (activeBrowserApprovalActionRef.current === actionKey) return;
    activeBrowserApprovalActionRef.current = actionKey;
    setError(null);
    try {
      if (currentApproval.requestId) {
        await api.denyCodingApproval(currentApproval.requestId, "User denied the request from the shared approval surface");
      }
      settleBrowserApproval(currentApproval);
      if (activeConversationId) {
        await loadConversation(activeConversationId, false);
        await refreshConversations(activeConversationId);
      }
    } catch (approvalError) {
      const staleMessage = currentApproval.requestId ? approvalStaleUiMessage(approvalError) : null;
      if (staleMessage) {
        settleBrowserApproval(currentApproval);
        setError(staleMessage);
      } else {
        console.error(approvalError);
        setError("拒否を保存できませんでした。リクエストの状態を更新して再試行してください。");
      }
    } finally {
      activeBrowserApprovalActionRef.current = null;
    }
  };

  const approveCodingAction = async () => {
    if (!runtimeApproval) return;
    if (!activeConversationId) return;
    if (activeRuntimeApprovalActionRef.current === runtimeApproval.requestId) return;
    activeRuntimeApprovalActionRef.current = runtimeApproval.requestId;
    setError(null);
    setIsGenerating(true);
    rememberPendingRequest({
      conversationId: activeConversationId,
      startedAt: Date.now(),
      status: "承認済みの操作を続行しています",
      toolNames: [runtimeApproval.toolName],
      toolStartedAt: { [runtimeApproval.toolName]: Date.now() },
    });
    try {
      const approvalWorkspace = workspaceContextFromConversation(activeConversation);
      const decision = await api.approveCodingApproval(runtimeApproval.requestId);
      if (!decision.approved) {
        throw new Error(decision.reason || "approval failed");
      }
      setSettledRuntimeApprovalIds((ids) => (
        ids.includes(runtimeApproval.requestId) ? ids : [...ids, runtimeApproval.requestId].slice(-50)
      ));
      await api.streamMessage(activeConversationId, "ユーザーが許可しました。承認済みの操作を続行してください。", {
        tool_choice: "required",
        tool_policy: {
          ...templatePolicyReferencePayload,
          action_approval_mode: actionApprovalMode,
          ...(ultraYoloMode ? { yolo_mode: true, allow_shell: true, allow_file_write: true, write_actions_require_approval: false } : {}),
          ...(ultraYoloMode ? { full_access: true } : {}),
          ...(approvalWorkspace.workspaceId ? { workspace_id: approvalWorkspace.workspaceId } : {}),
          ...(effectiveDisabledToolIds.length ? { disabled_tools: effectiveDisabledToolIds } : {}),
          selected_tools: [runtimeApproval.toolName],
        },
        tools: [runtimeApproval.toolName],
        metadata: {
          mode,
          ...(approvalWorkspace.workspaceId ? {
            workspace_id: approvalWorkspace.workspaceId,
            workspace_label: approvalWorkspace.workspaceLabel,
            workspace_root: approvalWorkspace.workspaceRoot,
          } : {}),
          approval_followup: {
            action: runtimeApproval.action,
            operation: runtimeApproval.operation,
            approval_token: decision.token,
            payload: runtimeApproval.payload,
            request_id: runtimeApproval.requestId,
            tool_call_id: runtimeApproval.toolCallId,
            tool_name: runtimeApproval.toolName,
          },
          runtime_content: runtimeApprovalRuntimeContent(runtimeApproval, decision.token),
          selected_tools: [runtimeApproval.toolName],
        },
      });
      forgetPendingRequest(activeConversationId);
      replaceChatIdInUrl(activeConversationId, false);
      await loadConversation(activeConversationId, false);
      await refreshConversations(activeConversationId);
    } catch (approvalError) {
      forgetPendingRequest(activeConversationId);
      const staleMessage = approvalStaleUiMessage(approvalError);
      if (staleMessage) {
        setSettledRuntimeApprovalIds((ids) => (
          ids.includes(runtimeApproval.requestId) ? ids : [...ids, runtimeApproval.requestId].slice(-50)
        ));
        setError(staleMessage);
      } else {
        console.error(approvalError);
        setError("許可を保存できませんでした。リクエストの状態を更新して再試行してください。");
      }
    } finally {
      activeRuntimeApprovalActionRef.current = null;
      setIsGenerating(false);
    }
  };

  const approveCommandAction = async () => {
    if (!pendingCommandApproval) return;
    const pending = pendingCommandApproval;
    setError(null);
    try {
      const decision = pending.approvalKind === "authority"
        ? await api.approveAuthorityApproval(pending.requestId, { scope: "once" })
        : await api.approveCodingApproval(pending.requestId);
      if (!decision.approved || !decision.token) {
        throw new Error(("reason" in decision ? decision.reason : undefined) || "approval failed");
      }
      const authorityToken = pending.approvalKind === "authority"
        ? decision.token
        : pending.authorityToken;
      const authorityRequestId = pending.approvalKind === "authority"
        ? pending.requestId
        : pending.authorityRequestId;
      const codingToken = pending.approvalKind === "coding"
        ? decision.token
        : pending.codingToken;
      const resumed = await api.resumeResolvedUiCommand({
        command: pending.commandRef,
        approval_token: codingToken,
        authority_request_id: authorityRequestId,
        authority_approval_token: authorityToken,
        args: pending.args,
        conversation_id: pending.conversationId,
        mode: pending.mode,
        invocation_id: pending.invocationId,
      });
      if (resumed.status === "approval_required" && resumed.approval?.request_id) {
        setPendingCommandApproval({
          ...pending,
          requestId: resumed.approval.request_id,
          approvalKind: resumed.approval.kind === "authority"
            ? "authority"
            : "coding",
          authorityRequestId,
          authorityToken,
          codingToken,
        });
        return;
      }
      if (resumed.status !== "succeeded" || !resumed.legacy_result) {
        throw new Error(resumed.error?.message || "command resume failed");
      }
      applyAuthoritativeCommandState(resumed.legacy_result);
      if (resumed.legacy_result.executed !== true) {
        runFrontendCommandAction(
          resumed.legacy_result.action,
          pending.command,
          resumed.legacy_result.args ?? pending.args,
        );
      }
      setPendingCommandApproval(null);
    } catch (approvalError) {
      setError(
        approvalError instanceof Error
          ? approvalError.message
          : "コマンドの承認再開に失敗しました。",
      );
    }
  };

  const denyCommandAction = async () => {
    if (!pendingCommandApproval) return;
    const pending = pendingCommandApproval;
    try {
      if (pending.approvalKind === "authority") {
        await api.denyAuthorityApproval(
          pending.requestId,
          "Denied from the command approval card",
        );
      } else {
        await api.denyCodingApproval(
          pending.requestId,
          "Denied from the command approval card",
        );
      }
      await api.cancelResolvedUiCommand({
        invocation_id: pending.invocationId,
        command_ref: pending.commandRef,
        conversation_id: pending.conversationId,
        mode: pending.mode,
        action: "deny",
        reason: "Denied from the command approval card",
      });
      setPendingCommandApproval(null);
    } catch (approvalError) {
      setError(
        approvalError instanceof Error
          ? approvalError.message
          : "コマンドの拒否に失敗しました。",
      );
    }
  };

  const denyCodingAction = async () => {
    if (!runtimeApproval) return;
    if (!activeConversationId) return;
    if (activeRuntimeApprovalActionRef.current === runtimeApproval.requestId) return;
    activeRuntimeApprovalActionRef.current = runtimeApproval.requestId;
    setError(null);
    try {
      await api.denyCodingApproval(runtimeApproval.requestId, "Denied from chat approval card");
      setSettledRuntimeApprovalIds((ids) => (
        ids.includes(runtimeApproval.requestId) ? ids : [...ids, runtimeApproval.requestId].slice(-50)
      ));
      await loadConversation(activeConversationId, false);
      await refreshConversations(activeConversationId);
    } catch (approvalError) {
      console.error(approvalError);
      setError("拒否を保存できませんでした。リクエストの状態を更新して再試行してください。");
    } finally {
      activeRuntimeApprovalActionRef.current = null;
    }
  };

  const openAuthorityApprovalWindowAction = async () => {
    if (!authorityApproval) return;
    setError(null);
    try {
      const opened = await openAuthorityApprovalWindow(authorityApproval.requestId);
      if (!opened) {
        setError("専用の承認ウィンドウを開けませんでした。Tobkiri Launcher に戻り、ポップアップを許可して再試行してください。");
      }
    } catch (openError) {
      console.error(openError);
      setError("専用の承認ウィンドウを開けませんでした。Tobkiri Launcher から再試行してください。");
    }
  };

  const openPendingHighRiskApproval = async () => {
    if (!pendingHighRiskCommand) return;
    setError(null);
    try {
      const opened = await openAuthorityApprovalWindow(pendingHighRiskCommand.requestId);
      if (!opened) {
        setError("専用の承認ウィンドウを開けませんでした。Tobkiri Launcher から再試行してください。");
      }
    } catch (openError) {
      console.error(openError);
      setError("専用の承認ウィンドウを開けませんでした。Tobkiri Launcher から再試行してください。");
    }
  };

  const pushActionPreview = (action: SidebarAction, title: string, data: unknown) => {
    const preview = previewFromAction(action, title, data);
    setPreviews((current) => [preview, ...current].slice(0, 30));
    setActivePreviewId(preview.id);
    setShowPreview(true);
  };

  const operationsHeartbeatSchedule = () => (
    (operationsStatus?.schedules ?? []).find((schedule) => String(schedule.name ?? "").toLowerCase().includes("heartbeat"))
  );

  const preferredOperationsModel = () => {
    const allowlist = settingList(settingsValues.operations_company?.model_allowlist);
    const manifestAllowlist = operationsStatus?.manifest.model_self_selection?.allowlist ?? [];
    const effectiveAllowlist = allowlist.length ? allowlist : manifestAllowlist;
    if (effectiveAllowlist.includes(preferredModel)) return preferredModel;
    if (effectiveAllowlist.includes("stub/default")) return "stub/default";
    return effectiveAllowlist[0] ?? "stub/default";
  };

  const handleStartOperationsCompany = async () => {
    setOperationsBusy(true);
    setError(null);
    try {
      const status = await api.bootstrapOperationsCompany({
        start_nonstop: true,
        heartbeat_minutes: Math.max(1, Math.min(1440, settingNumber(settingsValues.operations_company?.heartbeat_minutes, 15))),
        model: preferredOperationsModel(),
      });
      setOperationsStatus(status);
      await refreshConversations(status.conversation_id ?? null);
    } catch (startError) {
      setError(startError instanceof Error ? startError.message : "Operations Company の起動に失敗しました。");
    } finally {
      setOperationsBusy(false);
    }
  };

  const handleOpenOperationsChat = async () => {
    if (!operationsStatus?.conversation_id) {
      await handleStartOperationsCompany();
      return;
    }
    setError(null);
    await loadConversation(operationsStatus.conversation_id);
  };

  const preferredMimoCodingModel = () => {
    const allowlist = settingList(settingsValues.mimo_coding_company?.model_allowlist);
    const manifestAllowlist = mimoCodingStatus?.manifest.model_self_selection?.allowlist ?? [];
    return resolveMimoCodingModel(preferredModel, allowlist, manifestAllowlist);
  };

  const preferredMimoVisionModel = () => {
    const allowlist = settingList(settingsValues.mimo_coding_company?.model_allowlist);
    const manifestAllowlist = mimoCodingStatus?.manifest.model_self_selection?.allowlist ?? [];
    return resolveMimoVisionModel(allowlist, manifestAllowlist);
  };

  const preferredMimoFastModel = () => {
    const allowlist = settingList(settingsValues.mimo_coding_company?.model_allowlist);
    const manifestAllowlist = mimoCodingStatus?.manifest.model_self_selection?.allowlist ?? [];
    return resolveMimoFastModel(allowlist, manifestAllowlist);
  };

  const mimoCodingTargets = () => settingList(settingsValues.mimo_coding_company?.qa_targets);
  const mimoCodingPersonas = () => settingList(settingsValues.mimo_coding_company?.docker_personas);
  const mimoCodingMaxToolCalls = () => {
    const raw = settingsValues.mimo_coding_company?.max_tool_calls;
    if (raw === null || raw === undefined || raw === "" || raw === false) return null;
    const numeric = settingNumber(raw, 0);
    if (numeric <= 0) return null;
    return Math.max(1, Math.min(200, numeric));
  };
  const mimoCodingMaxToolCallsPayload = () => {
    const value = mimoCodingMaxToolCalls();
    return value === null ? {} : { max_tool_calls: value };
  };
  const selectedCodingWorkspaceRecord = () => (
    effectiveWorkspaceId
      ? codingWorkspaces.find((workspace) => workspace.workspace_id === effectiveWorkspaceId) ?? null
      : null
  );
  const mimoCodingWorkspacePayload = () => {
    const workspace = selectedCodingWorkspaceRecord();
    const workspaceId = workspace?.workspace_id ?? activeConversationWorkspaceContext.workspaceId ?? effectiveWorkspaceId;
    if (!workspaceId) return {};
    return {
      workspace_id: workspaceId,
      workspace_label: workspace?.label ?? activeConversationWorkspaceContext.workspaceLabel ?? null,
      workspace_root: workspace?.root_path ?? activeConversationWorkspaceContext.workspaceRoot ?? null,
    };
  };

  const handleStartMimoCodingCompany = async () => {
    setMimoCodingBusy(true);
    setError(null);
    try {
      const status = await api.bootstrapMimoCodingCompany({
        start_nonstop: true,
        heartbeat_minutes: Math.max(1, Math.min(1440, settingNumber(settingsValues.mimo_coding_company?.heartbeat_minutes, 30))),
        review_interval_minutes: Math.max(5, Math.min(1440, settingNumber(settingsValues.mimo_coding_company?.review_interval_minutes, 180))),
        qa_interval_minutes: Math.max(5, Math.min(1440, settingNumber(settingsValues.mimo_coding_company?.qa_interval_minutes, 240))),
        ...mimoCodingMaxToolCallsPayload(),
        model: preferredMimoCodingModel(),
        vision_model: preferredMimoVisionModel(),
        fast_model: preferredMimoFastModel(),
        qa_targets: mimoCodingTargets(),
        docker_worker_count: Math.max(1, Math.min(16, settingNumber(settingsValues.mimo_coding_company?.docker_worker_count, 3))),
        docker_personas: mimoCodingPersonas(),
        ...mimoCodingWorkspacePayload(),
        run_initial_review_now: settingsValues.mimo_coding_company?.run_initial_review_now !== false,
      });
      setMimoCodingStatus(status);
      await refreshConversations(status.conversation_id ?? null);
    } catch (startError) {
      setError(startError instanceof Error ? startError.message : "MiMo Coding Company の起動に失敗しました。");
    } finally {
      setMimoCodingBusy(false);
    }
  };

  const handleOpenMimoCodingChat = async () => {
    if (!mimoCodingStatus?.conversation_id) {
      await handleStartMimoCodingCompany();
      const refreshed = await api.getMimoCodingCompanyStatus();
      setMimoCodingStatus(refreshed);
      if (refreshed.conversation_id) {
        setError(null);
        handleHistoryClick(refreshed.conversation_id);
        return refreshed.conversation_id;
      }
      return null;
    }
    setError(null);
    handleHistoryClick(mimoCodingStatus.conversation_id);
    return mimoCodingStatus.conversation_id;
  };

  const handleTriggerOperationsHeartbeat = async () => {
    const heartbeat = operationsHeartbeatSchedule();
    if (!heartbeat?.id) return;
    setOperationsBusy(true);
    setError(null);
    try {
      const result = await api.triggerSchedule(String(heartbeat.id));
      pushActionPreview(
        { id: "operations.heartbeat", label: "Operations Heartbeat", icon: "activity" },
        "operations-heartbeat",
        result,
      );
      await refreshOperationsStatus();
      if (operationsStatus?.conversation_id) {
        await refreshConversations(operationsStatus.conversation_id);
      }
    } catch (heartbeatError) {
      setError(heartbeatError instanceof Error ? heartbeatError.message : "Operations Company heartbeat に失敗しました。");
    } finally {
      setOperationsBusy(false);
    }
  };

  const handlePanelAction = async (item: SidebarItem, action: SidebarAction) => {
    setError(null);
    try {
      let result: unknown;
      if (action.id === "conversation.export") {
        if (!activeConversationId) throw new Error("エクスポートする会話がありません。");
        const exported = await api.exportConversation(activeConversationId, "json");
        const blob = new Blob([exported.content], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = "history.json";
        anchor.click();
        URL.revokeObjectURL(url);
        result = { exported: true, format: "json" };
      } else if (action.id === "conversation.share") {
        if (!activeConversationId) throw new Error("共有する会話がありません。");
        setShareCreatedUrl(null);
        setShareCreatedToken(null);
        setShareRevoked(false);
        setShareDialogError(null);
        setShareDialogOpen(true);
        result = { dialog_opened: true };
      } else if (action.id === "artifacts.list") {
        result = await api.listArtifacts();
      } else if (action.id === "research.web") {
        result = await api.webSearch(String(input || activeChatTitle || "rumi"), false);
      } else if (action.id === "research.reddit") {
        result = await api.redditSearch(String(input || activeChatTitle || "rumi"), false);
      } else if (action.id === "browser.session") {
        result = await api.browserComputer("browser.session", { dry_run: true });
      } else if (action.id === "browser.profiles.list") {
        result = await api.browserComputer("browser.profiles.list", action.payload ?? {});
      } else if (action.id === "browser.profile.create") {
        result = await api.browserComputer("browser.profile.create", action.payload ?? {});
      } else if (action.id === "browser.cookies.list") {
        result = await api.browserComputer("browser.cookies.list", action.payload ?? {});
      } else if (action.id === "browser.profile.clear_cache.dry_run") {
        result = await api.browserComputer("browser.profile.clear_cache", { ...(action.payload ?? {}), dry_run: true });
      } else if (action.id === "browser.profile.clear_cookies.dry_run") {
        result = await api.browserComputer("browser.profile.clear_cookies", { ...(action.payload ?? {}), dry_run: true });
      } else if (action.id === "browser.screenshot.dry_run") {
        result = await api.browserComputer("computer.screenshot", { dry_run: true });
      } else if (action.id === "schedules.list") {
        result = await api.listSchedules();
      } else if (action.id === "channels.list") {
        result = await api.listChannels();
      } else if (action.id === "operations.status") {
        result = await api.getOperationsCompanyStatus();
        setOperationsStatus(result as OperationsCompanyStatus);
      } else if (action.id === "operations.bootstrap") {
        result = await api.bootstrapOperationsCompany({
          start_nonstop: true,
          heartbeat_minutes: Math.max(1, Math.min(1440, settingNumber(settingsValues.operations_company?.heartbeat_minutes, 15))),
          model: preferredOperationsModel(),
        });
        setOperationsStatus(result as OperationsCompanyStatus);
      } else if (action.id === "mimo_company.status") {
        result = await api.getMimoCodingCompanyStatus();
        setMimoCodingStatus(result as MimoCodingCompanyStatus);
      } else if (action.id === "mimo_company.bootstrap") {
        result = await api.bootstrapMimoCodingCompany({
          start_nonstop: true,
          heartbeat_minutes: Math.max(1, Math.min(1440, settingNumber(settingsValues.mimo_coding_company?.heartbeat_minutes, 30))),
          review_interval_minutes: Math.max(5, Math.min(1440, settingNumber(settingsValues.mimo_coding_company?.review_interval_minutes, 180))),
          qa_interval_minutes: Math.max(5, Math.min(1440, settingNumber(settingsValues.mimo_coding_company?.qa_interval_minutes, 240))),
          ...mimoCodingMaxToolCallsPayload(),
          model: preferredMimoCodingModel(),
          vision_model: preferredMimoVisionModel(),
          fast_model: preferredMimoFastModel(),
          qa_targets: mimoCodingTargets(),
          docker_worker_count: Math.max(1, Math.min(16, settingNumber(settingsValues.mimo_coding_company?.docker_worker_count, 3))),
          docker_personas: mimoCodingPersonas(),
          ...mimoCodingWorkspacePayload(),
          run_initial_review_now: settingsValues.mimo_coding_company?.run_initial_review_now !== false,
        });
        setMimoCodingStatus(result as MimoCodingCompanyStatus);
      } else if (action.id === "mimo_company.open_chat") {
        const conversationId = await handleOpenMimoCodingChat();
        result = { opened: true, conversation_id: conversationId };
      } else if (action.endpoint) {
        if (!isSafeLocalEndpoint(action.endpoint) || action.requires_approval) {
          throw new Error("この action は安全な /api/ endpoint ではないか、承認が必要なため直接実行できません。");
        }
        result = await defaultspackApiFetch(defaultspackContractRoute(action.endpoint), { method: action.method ?? "GET" }).then((response) => response.json());
      } else {
        result = { item: item.id, action: action.id, status: "ready" };
      }
      pushActionPreview(action, action.label, result);
      const text = typeof result === "string" ? result : JSON.stringify(result, null, 2);
      void navigator.clipboard?.writeText(text).catch(() => undefined);
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "サイドバー操作に失敗しました。");
    }
  };

  const createConversationShare = async (visibility: "local" | "tunnel") => {
    if (!activeConversationId) return;
    setShareBusy(true);
    setShareDialogError(null);
    try {
      const created = await api.createShare({
        target_type: "conversation",
        target_id: activeConversationId,
        title: activeChatTitle,
        visibility,
        expires_at: shareExpiryHours === "never" ? null : new Date(Date.now() + Number(shareExpiryHours) * 60 * 60 * 1000).toISOString(),
      });
      setShareCreatedUrl(String(created.share_url || ""));
      setShareCreatedToken(String(created.token || ""));
      setShareRevoked(false);
    } catch (reason) {
      setShareDialogError(reason instanceof Error ? reason.message : "共有リンクを作成できませんでした。");
    } finally {
      setShareBusy(false);
    }
  };

  const handleSubmit = async (event?: FormEvent, override?: SubmitOverride) => {
    event?.preventDefault();
    if (activeConversation?.metadata?.shared_read_only === true) {
      setError("This imported conversation is read-only. Import a continue copy to send messages.");
      return;
    }
    if (pendingMentionAttachmentRequestsRef.current.size > 0) {
      setError("workspace file の読み込みが終わるまでお待ちください。");
      return;
    }
    const inputForSubmit = override?.input ?? input;
    const attachmentsForSubmit = override?.attachments ?? attachedFiles;
    const requestedDroppedWidgets = override?.droppedWidgets ?? droppedWidgets;
    if ((!inputForSubmit.trim() && attachmentsForSubmit.length === 0) || isGenerating) return;
    shouldFollowMessagesRef.current = true;
    if (activeConversationId) {
      conversationScrollState.set(activeConversationId, {
        follow: true,
        scrollTop: messagesScrollRef.current?.scrollTop ?? 0,
      });
    }
    setRetryableSubmission(null);

    const commandInput = override ? null : parseSlashCommandInput(inputForSubmit, effectiveCommandCatalog, { enabled: slashCommandsEnabled });
    if (commandInput) {
      const shouldClearInput = await executeComposerCommand(commandInput.command.id, commandInput.raw);
      if (shouldClearInput !== false) setInput("");
      return;
    }

    const trimmedInput = inputForSubmit.trim();
    const userText = (trimmedInput.startsWith("//") ? trimmedInput.slice(1) : trimmedInput) || "添付ファイルを確認してください。";
    const submittedAttachments = attachmentsForSubmit;
    const wasNewConversation = isNewConversation;
    const selectionToolIdsForReconciliation = override?.toolSelectionRequest
      ? toolIdsFromSelectionRequest(override.toolSelectionRequest)
      : selectedToolIds;
    const reconciledDraft = reconcileComposerSemanticDraft({
      attachmentPaths: semanticAttachmentPaths(submittedAttachments),
      droppedWidgets: requestedDroppedWidgets,
      requireFileAttachment: true,
      selectedToolIds: selectionToolIdsForReconciliation,
      text: userText,
    });
    const droppedWidgetsForSubmit = reconciledDraft.droppedWidgets;
    const selectedToolIdsForSubmit = reconciledDraft.selectedToolIds;
    const semanticMentionToolIds = new Set(composerMentionToolIdsFromWidgets(requestedDroppedWidgets));
    const explicitToolReferenceIds = composerEntityReferences
      .filter((reference) => reference.kind === "tool")
      .map((reference) => reference.id);
    const explicitSkillReferenceIds = composerEntityReferences
      .filter((reference) => reference.kind === "skill")
      .map((reference) => reference.id);
    const mentionedToolIds = [...new Set([...explicitToolReferenceIds, ...toolMentionIdsFromText(userText, composerExtensions)
      .filter((toolId) => !semanticMentionToolIds.has(toolId))
      .filter((toolId) => !dismissedComposerMentionToolsRef.current.has(toolId))])];
    const mentionedSkillIdsFromText = [...new Set([...explicitSkillReferenceIds, ...skillMentionIdsFromText(userText, composerSkills)])];
    const toolSelectionRequest = override?.toolSelectionRequest ?? toolSelectionController.buildRequest({
      toolIds: selectedToolIdsForSubmit,
      mentionedToolIds,
    });
    if (!override?.skipReview && toolSelectionRequest.mode === "review") {
      setError(null);
      try {
        await toolSelectionController.previewReview({
          conversationId: activeConversationId,
          userText,
          attachmentMetadata: submittedAttachments.map(({ name, size, type, truncated, source, sourcePath }) => ({ name, size, type, truncated, source, sourcePath })),
          toolSelection: toolSelectionRequest,
          model: activeProfile?.profile_id ?? preferredModel ?? null,
          draft: {
            input: inputForSubmit,
            attachments: submittedAttachments,
            droppedWidgets: droppedWidgetsForSubmit,
          },
        });
        setInput("");
        cancelPendingMentionAttachments();
        setAttachedFiles([]);
        setDroppedWidgets([]);
      } catch (previewError) {
        setError(previewError instanceof Error ? previewError.message : "機能の候補を取得できませんでした。");
      }
      return;
    }
    setIsGenerating(true);
    cancelPendingMentionAttachments();
    setError(null);
    if (wasNewConversation) {
      setIsNewChatLaunching(true);
    }
    setInput("");
    setComposerEntityReferences([]);
    setAttachedFiles([]);
    let submittedConversationId: string | null = null;
    const shouldKeepSelectedToolsAfterSend = keepSelectedToolsAfterSend(settingsValues);
    const requestedToolIds = [...new Set([...selectedToolIdsForSubmit, ...mentionedToolIds, ...toolIdsFromSelectionRequest(toolSelectionRequest)])];
    const submittedToolIds = toolSelectionRequest.mode === "none" ? [] : requestedToolIds;
    const submittedToolIdSet = new Set(submittedToolIds);
    const composerToolById = new Map(composerExtensions.map((item) => [item.id, item]));
    const composerSkillById = new Map(composerSkills.map((item) => [item.id, item]));
    const droppedWidgetToolIds = new Set(droppedWidgetsForSubmit.map((widget) => widget.sourceItemId || widget.id));
    const droppedWidgetSkillIds = new Set(
      droppedWidgetsForSubmit
        .filter((widget) => widget.type === "skill" || widget.widgetKind === "skill_prompt")
        .map((widget) => widget.sourceItemId || widget.id),
    );
    const submittedSkillIds = [...new Set([...Array.from(droppedWidgetSkillIds), ...mentionedSkillIdsFromText])];
    const mentionedToolWidgets = mentionedToolIds
      .map((toolId) => composerToolById.get(toolId))
      .filter((item): item is ComposerExtensionItem => Boolean(item))
      .filter((item) => !droppedWidgetToolIds.has(item.id))
      .map((item) => composerToolMentionWidget(item));
    const mentionedSkillWidgets = mentionedSkillIdsFromText
      .map((skillId) => composerSkillById.get(skillId))
      .filter((item): item is ComposerSkillItem => Boolean(item))
      .filter((item) => !droppedWidgetSkillIds.has(item.id))
      .map((item) => composerSkillMentionWidget(item));
    const submittedDroppedWidgets = [...droppedWidgetsForSubmit, ...mentionedToolWidgets, ...mentionedSkillWidgets];
    const submittedMentions = composerMentionMetadataFromWidgets(submittedDroppedWidgets);
    const selectedToolLabels = submittedToolIds.map((toolId) => composerToolById.get(toolId)?.label || toolId);
    const activeContextForSubmit = workspaceContextFromConversation(activeConversation);
    const groupIdForSubmit = pendingNewTaskContext?.groupId ?? activeContextForSubmit.groupId;
    const workspaceIdForSubmit = pendingNewTaskContext?.workspaceId
      ?? activeContextForSubmit.workspaceId
      ?? (mode === "coding" ? selectedCodingWorkspaceId : null);
    const workspaceLabelForSubmit = pendingNewTaskContext?.workspaceLabel
      ?? activeContextForSubmit.workspaceLabel
      ?? codingWorkspaces.find((workspace) => workspace.workspace_id === workspaceIdForSubmit)?.label
      ?? null;
    const workspaceRootForSubmit = pendingNewTaskContext?.workspaceRoot
      ?? activeContextForSubmit.workspaceRoot
      ?? codingWorkspaces.find((workspace) => workspace.workspace_id === workspaceIdForSubmit)?.root_path
      ?? null;
    const rumiDataPathForSubmit = pendingNewTaskContext?.rumiDataPath ?? activeContextForSubmit.rumiDataPath ?? null;
    const isCodingWorkspaceSubmit = mode === "coding" || Boolean(workspaceIdForSubmit);
    let submittedConversationRuntimeId: string | null = null;
    let markInterruptedAssistant: ((streamError: ChatStreamInterruptedError) => void) | null = null;

    try {
      let conversation = activeConversation;
      if (!conversation) {
        conversation = await api.createConversation({
          model: preferredModel || "stub/default",
          conversation_kind: isCodingWorkspaceSubmit ? "coding" : null,
          group_id: groupIdForSubmit ?? null,
          tags: isCodingWorkspaceSubmit ? ["coding"] : undefined,
          metadata: {
            ...(groupIdForSubmit ? { group_id: groupIdForSubmit } : {}),
            ...(rumiDataPathForSubmit ? { rumi_data_path: rumiDataPathForSubmit } : {}),
            ...(isCodingWorkspaceSubmit
            ? {
                mode: "coding",
                workspace_id: workspaceIdForSubmit,
                workspace_label: workspaceLabelForSubmit,
                workspace_root: workspaceRootForSubmit,
              }
              : {}),
          },
        });
        setPendingNewTaskContext(null);
        setActiveConversationId(conversation.id);
      }
      const isOperationsMode = isOperationsConversation(conversation);
      const isMimoCodingMode = isMimoCodingConversation(conversation);
      const workspaceIdForRuntime = workspaceIdForSubmit ?? (isMimoCodingMode ? selectedCodingWorkspaceId : null);
      const workspaceRecordForRuntime = workspaceIdForRuntime
        ? codingWorkspaces.find((workspace) => workspace.workspace_id === workspaceIdForRuntime) ?? null
        : null;
      const workspaceLabelForRuntime = workspaceLabelForSubmit ?? workspaceRecordForRuntime?.label ?? null;
      const workspaceRootForRuntime = workspaceRootForSubmit ?? workspaceRecordForRuntime?.root_path ?? null;
      const shouldAttachWorkspaceToRuntime = isCodingWorkspaceSubmit || isMimoCodingMode;
      submittedConversationId = conversation.id;
      submittedConversationRuntimeId = conversation.id;
      const requestStartedAt = Date.now();
      const requestFingerprint = JSON.stringify({
        text: userText,
        attachments: submittedAttachments.map(({ name, size, type, source, sourcePath }) => (
          { name, size, type, source, sourcePath }
        )),
      });
      const recoverablePending = pendingRequests[conversation.id];
      const operationId = recoverablePending?.requestFingerprint === requestFingerprint
        && recoverablePending.operationId
        ? recoverablePending.operationId
        : typeof globalThis.crypto?.randomUUID === "function"
          ? globalThis.crypto.randomUUID()
          : `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
      rememberPendingRequest({
        conversationId: conversation.id,
        operationId,
        requestFingerprint,
        startedAt: requestStartedAt,
        status: `${activeProfile?.display_name ?? preferredModel} が思考中`,
        toolNames: [],
        toolStartedAt: {},
      });
      replaceChatIdInUrl(conversation.id, true);

      const title =
        conversation.title === "New Conversation"
          ? deriveConversationTitle(userText)
          : conversation.title;
      const optimisticConversation = {
        ...conversation,
        title,
        updated_at: Date.now(),
        messages: [
          ...conversation.messages,
          optimisticUserMessage(
            conversation.id,
            userText,
            submittedMentions.length > 0 ? { mentions: submittedMentions } : undefined,
          ),
        ],
      };
      setActiveConversation(optimisticConversation);
      setConversations((current) => {
        const item = {
          ...optimisticConversation,
          messages: [],
        };
        const withoutCurrent = current.filter((candidate) => candidate.id !== conversation.id);
        return [item, ...withoutCurrent];
      });
      const assistantDraft = optimisticAssistantMessage(conversation.id, preferredModel || "stub/default");
      const abortController = new AbortController();
      currentAbortControllerRef.current = abortController;
      streamingConversationIdRef.current = conversation.id;
      let finalStreamMessageId: string | null = null;
      let finalStreamActivityEvents: ChatActivityEvent[] = [];
      const updateStreamingAssistant = (delta: string) => {
        if (finalStreamMessageId) return;
        setActiveConversation((current) => {
          if (!current || current.id !== conversation.id) return current;
          const existing = current.messages.find((message) => message.id === assistantDraft.id);
          if (!existing) {
            return {
              ...current,
              messages: [
                ...current.messages,
                {
                  ...assistantDraft,
                  content: [{ type: "text", text: delta }],
                  raw_text: delta,
                },
              ],
            };
          }
          return {
            ...current,
            messages: current.messages.map((message) => {
              if (message.id !== assistantDraft.id) return message;
              const nextText = `${message.raw_text ?? ""}${delta}`;
              return {
                ...message,
                content: [{ type: "text", text: nextText }],
                raw_text: nextText,
              };
            }),
          };
        });
      };
      const updateStreamingThinking = (delta: string) => {
        if (finalStreamMessageId) return;
        setActiveConversation((current) => {
          if (!current || current.id !== conversation.id) return current;
          const existing = current.messages.find((message) => message.id === assistantDraft.id);
          const nextThinking = (message: ChatMessage) => {
            const metadata = { ...(message.metadata ?? {}) };
            const thinking = metadata.thinking as Record<string, unknown> | undefined;
            metadata.thinking = {
              ...(thinking ?? {}),
              state: "streaming",
              transcript: `${String(thinking?.transcript ?? "")}${delta}`,
            };
            return { ...message, metadata };
          };
          if (!existing) {
            return {
              ...current,
              messages: [...current.messages, nextThinking(assistantDraft)],
            };
          }
          return {
            ...current,
            messages: current.messages.map((message) => message.id === assistantDraft.id ? nextThinking(message) : message),
          };
        });
      };
      const updateStreamingActivity = (streamEvent: ChatStreamEvent) => {
        if (!isActivityStreamEvent(streamEvent)) return;
        const eventTimestamp = Date.now();
        const activityEvent: ChatActivityEvent = { timestamp: eventTimestamp, ...streamEvent };
        const finalizedMessageIdAtEvent = finalStreamMessageId;
        if (finalizedMessageIdAtEvent) {
          finalStreamActivityEvents = upsertStreamActivityEvent(finalStreamActivityEvents, activityEvent);
        }
        setActiveConversation((current) => {
          if (!current || current.id !== conversation.id) return current;
          const targetMessageId = finalizedMessageIdAtEvent ?? assistantDraft.id;
          const existing = current.messages.find((message) => message.id === targetMessageId);
          const appendEvent = (message: ChatMessage): ChatMessage => ({
            ...message,
            events: upsertStreamActivityEvent(message.events ?? [], activityEvent),
          });
          if (!existing) {
            if (finalizedMessageIdAtEvent) return current;
            return {
              ...current,
              messages: [...current.messages, appendEvent(assistantDraft)],
            };
          }
          return {
            ...current,
            messages: current.messages.map((message) => message.id === targetMessageId ? appendEvent(message) : message),
          };
        });

        if (activityEvent.phase === "conversation_steer") {
          const processed = Array.isArray(activityEvent.processed)
            ? activityEvent.processed.filter(isConversationSteerItem)
            : [];
          if (processed.length > 0) {
            setSteerItems((current) => {
              const byId = new Map(current.map((item) => [item.id, item]));
              for (const item of processed) byId.set(item.id, item);
              return Array.from(byId.values());
            });
            setModelSteerStatus({ kind: "success", message: "ステアを反映しました" });
          }
        }

        const status = typeof activityEvent.message === "string" && activityEvent.message.trim()
          ? activityEvent.message.trim()
          : pendingRequests[conversation.id]?.status ?? `${activeProfile?.display_name ?? preferredModel} が思考中`;
        const toolName = typeof activityEvent.tool_name === "string" ? activityEvent.tool_name.trim() : "";
        if (finalizedMessageIdAtEvent) return;
        updatePendingRequests((current) => {
          const existing = current[conversation.id] ?? {
            conversationId: conversation.id,
            startedAt: requestStartedAt,
            status,
            toolNames: [],
            toolStartedAt: {},
          };
          const toolNames = toolName ? [...new Set([...existing.toolNames, toolName])] : existing.toolNames;
          const toolStartedAt = { ...(existing.toolStartedAt ?? {}) };
          if (toolName && toolStartedAt[toolName] === undefined) {
            toolStartedAt[toolName] = eventTimestamp;
          }
          return {
            ...current,
            [conversation.id]: {
              ...existing,
              status,
              toolNames,
              toolStartedAt,
            },
          };
        });
      };
      const replaceStreamingAssistant = (message: ChatMessage) => {
        finalStreamMessageId = message.id;
        const completedAt = Date.now();
        const enhancedMessage: ChatMessage = {
          ...message,
          metadata: {
            ...(message.metadata ?? {}),
            timing: {
              ...((message.metadata?.timing && typeof message.metadata.timing === "object") ? message.metadata.timing as Record<string, unknown> : {}),
              thinking_started_at: requestStartedAt,
              completed_at: completedAt,
              thinking_duration_ms: completedAt - requestStartedAt,
              thinking_duration_label: boundedDurationLabel(requestStartedAt, completedAt),
            },
          },
        };
        setActiveConversation((current) => {
          if (!current || current.id !== conversation.id) return current;
          const withoutDraft = current.messages.filter((candidate) => candidate.id !== assistantDraft.id);
          const existingFinalMessage = withoutDraft.find((candidate) => candidate.id === enhancedMessage.id);
          const baseMergedMessage = mergeStreamingFinalMessage(existingFinalMessage, enhancedMessage);
          const mergedMessage = {
            ...baseMergedMessage,
            events: mergeChatActivityEvents(baseMergedMessage.events, finalStreamActivityEvents),
          };
          return {
            ...current,
            messages: existingFinalMessage
              ? withoutDraft.map((candidate) => candidate.id === enhancedMessage.id ? mergedMessage : candidate)
              : [...withoutDraft, mergedMessage],
          };
        });
        forgetPendingRequest(conversation.id);
        replaceChatIdInUrl(conversation.id, false);
        setIsGenerating(false);
      };
      markInterruptedAssistant = (streamError: ChatStreamInterruptedError) => {
        const completedAt = Date.now();
        setActiveConversation((current) => {
          if (!current || current.id !== conversation.id) return current;
          const existing = current.messages.find((message) => message.id === assistantDraft.id);
          const existingMetadata = existing?.metadata && typeof existing.metadata === "object"
            ? existing.metadata as Record<string, unknown>
            : {};
          const existingThinking = existingMetadata.thinking && typeof existingMetadata.thinking === "object"
            ? existingMetadata.thinking as Record<string, unknown>
            : {};
          const nextText = String(existing?.raw_text ?? "") || streamError.partialText;
          const nextTranscript = `${String(existingThinking.transcript ?? "")}${streamError.thinkingText}`;
          const interruptedMessage: ChatMessage = {
            ...(existing ?? assistantDraft),
            content: nextText ? [{ type: "text", text: nextText }] : existing?.content ?? assistantDraft.content,
            raw_text: nextText,
            finish_reason: "interrupted",
            metadata: {
              ...existingMetadata,
              thinking: {
                ...existingThinking,
                state: "interrupted",
                transcript: nextTranscript || undefined,
              },
              transport: {
                status: "interrupted",
                reason: streamError.message,
                saw_activity: streamError.sawActivity,
              },
              timing: {
                ...((existingMetadata.timing && typeof existingMetadata.timing === "object") ? existingMetadata.timing as Record<string, unknown> : {}),
                thinking_started_at: requestStartedAt,
                completed_at: completedAt,
                thinking_duration_ms: completedAt - requestStartedAt,
                thinking_duration_label: boundedDurationLabel(requestStartedAt, completedAt),
              },
            },
          };
          const hasExisting = current.messages.some((message) => message.id === assistantDraft.id);
          return {
            ...current,
            messages: hasExisting
              ? current.messages.map((message) => message.id === assistantDraft.id ? interruptedMessage : message)
              : [...current.messages, interruptedMessage],
          };
        });
      };

      const operationsModelAllowlist = settingList(settingsValues.operations_company?.model_allowlist);
      const operationsToolDenylist = settingList(settingsValues.operations_company?.tool_denylist);
      const operationsToolAllowlist = operationsStatus?.manifest.tool_policy?.allowlist ?? [];
      const operationsPolicy = isOperationsMode
        ? {
            profile_id: "defaultspack.operations_company",
            non_stop: true,
            allow_shell: false,
            allow_file_write: true,
            write_actions_require_approval: true,
            normal_status_silent: settingsValues.operations_company?.normal_status_silent !== false,
            max_concurrent_children: Math.max(1, Math.min(12, settingNumber(settingsValues.operations_company?.max_concurrent_children, 3))),
            ...(operationsModelAllowlist.length ? { model_allowlist: operationsModelAllowlist } : {}),
            ...(operationsToolAllowlist.length ? { tool_allowlist: operationsToolAllowlist } : {}),
            ...(operationsToolDenylist.length ? { tool_denylist: operationsToolDenylist } : {}),
          }
        : {};
      const mimoCodingModelAllowlist = settingList(settingsValues.mimo_coding_company?.model_allowlist);
      const mimoCodingToolAllowlist = mimoCodingStatus?.manifest.tool_policy?.allowlist ?? [];
      const mimoCodingPolicy = isMimoCodingMode
        ? {
            profile_id: "defaultspack.mimo_coding_company",
            non_stop: true,
            allow_shell: true,
            allow_file_write: true,
            write_actions_require_approval: false,
            delete_actions_require_approval: true,
            terminal_actions_require_approval: false,
            normal_status_silent: true,
            max_concurrent_children: 6,
            ...mimoCodingMaxToolCallsPayload(),
            ...(mimoCodingModelAllowlist.length ? { model_allowlist: mimoCodingModelAllowlist } : {}),
            ...(mimoCodingToolAllowlist.length ? { tool_allowlist: mimoCodingToolAllowlist } : {}),
          }
        : {};
      const templateRequestPayload = {
        params: {
          ...templateAiInputParams,
          ...(Object.keys(effectiveStructuredComposerValues).length ? { composer_fields: effectiveStructuredComposerValues } : {}),
        },
        toolPolicy: {
          ...templatePolicyReferencePayload,
          ...(composerInputMetadata?.id ? { composer_input_id: composerInputMetadata.id } : {}),
        },
      };
      const shouldSendExplicitToolSelection = toolSelectionRequest.mode === "manual" && submittedToolIds.length > 0;

      await api.streamMessage(conversation.id, userText, {
        idempotency_key: operationId,
        params: templateRequestPayload.params,
        thinking_level: activeProfile?.supports_thinking ? selectedThinkingLevel : null,
        deepthink_enabled: deepthinkEnabled,
        tool_selection: toolSelectionRequest,
        tool_policy: {
          ...templateRequestPayload.toolPolicy,
          action_approval_mode: actionApprovalMode,
          ...(ultraYoloMode ? { yolo_mode: true, allow_shell: true, allow_file_write: true, write_actions_require_approval: false } : {}),
          ...(ultraYoloMode ? { full_access: true } : {}),
          ...operationsPolicy,
          ...mimoCodingPolicy,
          ...(shouldAttachWorkspaceToRuntime && workspaceIdForRuntime ? { workspace_id: workspaceIdForRuntime } : {}),
          ...(effectiveDisabledToolIds.length ? { disabled_tools: effectiveDisabledToolIds } : {}),
          ...(shouldSendExplicitToolSelection ? { selected_tools: submittedToolIds } : {}),
        },
        attachments: submittedAttachments,
        tools: shouldSendExplicitToolSelection ? submittedToolIds : undefined,
        metadata: {
          mode: isOperationsMode ? "operations_company" : isMimoCodingMode ? "mimo_coding_company" : isCodingWorkspaceSubmit ? "coding" : mode,
          ...(groupIdForSubmit ? { group_id: groupIdForSubmit } : {}),
          ...(rumiDataPathForSubmit ? { rumi_data_path: rumiDataPathForSubmit } : {}),
          ...(isOperationsMode ? {
            profile_id: "defaultspack.operations_company",
            agent_id: "client_manager",
            conversation_strategy: "one_agent_one_conversation",
            internal_channel: "ops-company",
          } : {}),
          ...(isMimoCodingMode ? {
            profile_id: "defaultspack.mimo_coding_company",
            agent_id: "client_manager",
            conversation_strategy: "one_agent_one_conversation",
            internal_channel: "mimo-coding-company",
          } : {}),
          ...(shouldAttachWorkspaceToRuntime && workspaceIdForRuntime ? {
            workspace_id: workspaceIdForRuntime,
            workspace_label: workspaceLabelForRuntime,
            workspace_root: workspaceRootForRuntime,
          } : {}),
          ...templateRequestPayload.toolPolicy,
          ...(Object.keys(effectiveStructuredComposerValues).length ? { structured_input: effectiveStructuredComposerValues } : {}),
          attachments: submittedAttachments.map(({ name, size, type, truncated, source, sourcePath }) => ({ name, size, type, truncated, source, sourcePath })),
          ...(shouldSendExplicitToolSelection ? { selected_tools: submittedToolIds } : {}),
          ...(submittedSkillIds.length ? { skills: submittedSkillIds, skill_mentions: submittedSkillIds.map((skillId) => ({ id: skillId, label: composerSkillById.get(skillId)?.label ?? skillId })) } : {}),
          ...(submittedMentions.length ? { mentions: submittedMentions } : {}),
          dropped_widgets: submittedDroppedWidgets
            .filter((widget) => widget.widgetKind === "tool_toggle" || widget.type === "tool" ? submittedToolIdSet.has(widget.sourceItemId || widget.id) : widget.enabled !== false)
            .map(({ id, type, label, widgetKind, sourceItemId, metadata }) => ({
              id,
              type,
              label,
              widgetKind,
              sourceItemId,
              metadata: publicComposerWidgetMetadata(metadata),
            })),
        },
      }, {
        onEvent: updateStreamingActivity,
        onDelta: updateStreamingAssistant,
        onThinkingDelta: updateStreamingThinking,
        onMessage: replaceStreamingAssistant,
        signal: abortController.signal,
      });
      setAttachedFiles([]);
      setDroppedWidgets([]);
      setRetryableSubmission(null);
      dismissedComposerMentionToolsRef.current.clear();
      toolSelectionController.clearTurnStateAfterSend({ keepSelectedTools: shouldKeepSelectedToolsAfterSend });
      forgetPendingRequest(conversation.id);
      replaceChatIdInUrl(conversation.id, false);

      if (title !== conversation.title) {
        await api.updateConversation(conversation.id, { title });
      }

      await refreshConversations(conversation.id);
      await refreshSteerQueue(conversation.id).catch(console.error);
    } catch (submitError) {
      console.error("Chat error:", submitError);
      if (isCancelledStreamError(submitError)) {
        if (submittedConversationId) {
          forgetPendingRequest(submittedConversationId);
          replaceChatIdInUrl(submittedConversationId, false);
          await refreshConversations(submittedConversationId).catch(console.error);
        }
        setError(null);
        return;
      }
      if (submitError instanceof ChatStreamInterruptedError) {
        const interruptedConversationId = submittedConversationId ?? submittedConversationRuntimeId;
        markInterruptedAssistant?.(submitError);
        if (interruptedConversationId) {
          // A stream close is transport-ambiguous: the backend may already
          // have committed the turn. Keep the persisted operation id and
          // pending URL so a retry replays this logical send.
          updatePendingRequests((current) => {
            const existing = current[interruptedConversationId];
            return existing
              ? {
                  ...current,
                  [interruptedConversationId]: {
                    ...existing,
                    status: "応答ストリームが切れました。再試行すると結果を確認します",
                  },
                }
              : current;
          });
        }
        setBackendConnectionState("degraded");
        setBackendConnectionNote("応答 stream が途中で閉じました。ここまで届いた内容を保持しつつ、backend の回復を待っています。");
        void reportClientDiagnostic({
          source: "webapp",
          category: "stream_interrupted",
          level: "warning",
          message: "The frontend preserved a partial assistant response after the stream was interrupted.",
          fingerprint: `stream-interrupted:${interruptedConversationId ?? "new"}:${submitError.message}`,
          conversationId: interruptedConversationId,
          detail: {
            error: submitError.message,
            partialTextLength: submitError.partialText.length,
            thinkingTextLength: submitError.thinkingText.length,
            sawActivity: submitError.sawActivity,
          },
        });
        const interruptionMessage = submitError.partialText.trim()
          ? "応答ストリームが途中で切れたため、ここまで届いた内容を保護して着地しました。"
          : "応答ストリームが途中で切れました。画面は保護したまま、再接続の余地を残しています。";
        setRetryableSubmission({
          input: inputForSubmit,
          attachments: submittedAttachments,
          droppedWidgets: droppedWidgetsForSubmit,
          toolSelectionRequest,
          skipReview: true,
          errorMessage: interruptionMessage,
        });
        setError(interruptionMessage);
        dismissedComposerMentionToolsRef.current.clear();
        setIsNewChatLaunching(false);
        return;
      }
      const preserveOperationForRetry = isLikelyTransportFailure(submitError);
      if (submittedConversationId && !preserveOperationForRetry && !isUnloadingRef.current && document.visibilityState !== "hidden") {
        forgetPendingRequest(submittedConversationId);
        replaceChatIdInUrl(submittedConversationId, false);
        await refreshConversations(submittedConversationId).catch(console.error);
      }
      void reportClientDiagnostic({
        source: "webapp",
        category: "chat_submit_error",
        level: "error",
        message: submitError instanceof Error ? submitError.message : "Message submission failed.",
        fingerprint: `chat-submit:${submittedConversationId ?? "new"}:${submitError instanceof Error ? submitError.message : "unknown"}`,
        conversationId: submittedConversationId,
        detail: {
          mode,
          hadAttachments: submittedAttachments.length > 0,
        },
      });
      const submitErrorMessage = submitError instanceof Error
        ? submitError.message
        : "メッセージ送信に失敗しました。";
      setInput(inputForSubmit);
      setAttachedFiles(submittedAttachments);
      setDroppedWidgets(droppedWidgetsForSubmit);
      setRetryableSubmission({
        input: inputForSubmit,
        attachments: submittedAttachments,
        droppedWidgets: droppedWidgetsForSubmit,
        toolSelectionRequest,
        skipReview: true,
        errorMessage: submitErrorMessage,
      });
      setError(submitErrorMessage);
      setIsNewChatLaunching(false);
    } finally {
      streamingConversationIdRef.current = null;
      currentAbortControllerRef.current = null;
      setIsGenerating(false);
      setIsNewChatLaunching(false);
    }
  };

  const handleRetryLastFailedSubmission = () => {
    const retry = retryableSubmission;
    if (!retry || isGenerating) return;
    setError(null);
    setRetryableSubmission(null);
    void handleSubmit(undefined, {
      input: retry.input,
      attachments: retry.attachments,
      droppedWidgets: retry.droppedWidgets,
      toolSelectionRequest: retry.toolSelectionRequest,
      skipReview: true,
    });
  };

  const dismissChatError = () => {
    setError(null);
    setRetryableSubmission(null);
  };

  const handleToolReviewApprove = () => {
    const pending = toolSelectionController.state.pendingReview;
    const request = toolSelectionController.approveReview();
    if (!pending || !request) return;
    void handleSubmit(undefined, {
      input: pending.draft.input,
      attachments: pending.draft.attachments as AttachedFile[],
      droppedWidgets: pending.draft.droppedWidgets as DroppedWidget[],
      toolSelectionRequest: request,
      skipReview: true,
    });
  };

  const handleToolReviewNoTools = () => {
    const pending = toolSelectionController.state.pendingReview;
    const request = toolSelectionController.continueWithoutTools();
    if (!pending || !request) return;
    void handleSubmit(undefined, {
      input: pending.draft.input,
      attachments: pending.draft.attachments as AttachedFile[],
      droppedWidgets: pending.draft.droppedWidgets as DroppedWidget[],
      toolSelectionRequest: request,
      skipReview: true,
    });
  };

  const handleToolReviewCancel = () => {
    const pending = toolSelectionController.state.pendingReview;
    toolSelectionController.cancelReview();
    if (!pending) return;
    setInput(pending.draft.input);
    setAttachedFiles(pending.draft.attachments as AttachedFile[]);
    setDroppedWidgets(pending.draft.droppedWidgets as DroppedWidget[]);
  };

  const handleToolReviewEdit = () => {
    const pending = toolSelectionController.state.pendingReview;
    if (!pending) return;
    const selectedIds = pending.decision.selected_tools.filter((toolId) => composerExtensions.some((tool) => tool.id === toolId));
    setStoredSelectedToolIds(selectedIds);
    toolSelectionController.setTurnMode("manual");
  };

  const Renderers = useMemo(() => resolveDefaultspackRenderers(catalog), [catalog]);
  const codingSidebarPanel = mode === "coding" ? (
    <CodingCockpit
      variant="sidebar"
      workspaces={codingWorkspaces}
      selectedWorkspaceId={effectiveWorkspaceId}
      consoleScopeKey={effectiveConsoleKey}
      onWorkspaceSelect={handleCodingWorkspaceSelect}
      onWorkspaceCreate={() => void handleCodingWorkspacePickCreate()}
      onWorkspaceTrust={handleCodingWorkspaceTrust}
      onWorkspacesRefresh={() => void loadCodingWorkspaces()}
    />
  ) : null;
  const isCalendarMode = activeWorkspaceKind === "calendar";
  const isKanbanMode = activeWorkspaceKind === "kanban";
  const calendarSettings = parseCalendarSettings(settingsValues.calendar);
  const activeConversationMetadata: Record<string, unknown> = activeConversation?.metadata && typeof activeConversation.metadata === "object"
    ? activeConversation.metadata
    : {};
  const activeConversationGroupId = cleanOptionalString(activeConversation?.group_id)
    ?? cleanOptionalString(activeConversationMetadata.group_id ?? activeConversationMetadata.groupId);
  const composerProjects = effectiveGroupId && !projects.some((project) => project.id === effectiveGroupId)
    ? [{
        id: effectiveGroupId,
        title: cleanOptionalString(activeConversationMetadata.group_title ?? activeConversationMetadata.groupTitle) ?? effectiveGroupId,
        workspaceId: activeConversationWorkspaceContext.workspaceId ?? null,
        workspaceLabel: activeConversationWorkspaceContext.workspaceLabel ?? null,
        workspaceRoot: activeConversationWorkspaceContext.workspaceRoot ?? null,
        rumiDataPath: activeConversationWorkspaceContext.rumiDataPath ?? null,
      }, ...projects]
    : projects;
  const activeConversationCompanyId = resolveCompanyWorkspaceHint({
    companyId: activeConversationMetadata.company_id ?? activeConversationMetadata.companyId,
    groupId: activeConversationGroupId,
    conversationKind: activeConversation?.conversation_kind,
    profileId: activeConversationMetadata.profile_id,
    tags: activeConversation?.tags,
  });
  const activeCompanyWorkspaceHint = activeConversationCompanyId ?? activeHistoryCompanyId;
  const handleCalendarModeToggle = () => {
    const existingCalendarTab = workspaceTabs.find((tab) => tab.kind === "calendar");
    if (existingCalendarTab) {
      activateWorkspaceTab(existingCalendarTab);
      return;
    }
    handleWorkspaceTabCreate("calendar");
  };

  const openKanbanScope = (
    scope: KanbanBoardScope = { type: "global", id: "default" },
    label = "All Rumi Runs",
  ) => {
    const existingTab = workspaceTabs.find((tab) => (
      tab.kind === "kanban"
      && (tab.kanbanScope?.type ?? "global") === scope.type
      && (tab.kanbanScope?.id ?? "default") === scope.id
    ));
    if (existingTab) {
      activateWorkspaceTab(existingTab);
      return;
    }
    const tab = createWorkspaceTab("kanban", {
      title: label || "Kanban",
      kanbanScope: scope,
      kanbanScopeLabel: label || "Kanban",
    });
    setWorkspaceTabs((current) => [...current, tab]);
    activateWorkspaceTab(tab);
  };

  const handleKanbanModeToggle = () => {
    openKanbanScope();
  };

  const handleDesktopsModeOpen = () => {
    const existingDesktopsTab = workspaceTabs.find((tab) => tab.kind === "desktops");
    if (existingDesktopsTab) {
      activateWorkspaceTab(existingDesktopsTab);
      return;
    }
    handleWorkspaceTabCreate("desktops");
  };

  const handleHistoryGroupKanbanOpen = (group: ChatGroup) => {
    openKanbanScope({ type: "group", id: group.id }, group.title);
  };

  const renderComposer = (isCentered = false) => {
    if (!isCentered && activeConversation?.metadata?.shared_read_only === true) {
      return <div role="status" className="mx-3 mb-3 flex min-h-14 items-center justify-center border border-zinc-800 bg-zinc-950 px-4 text-center text-sm text-zinc-400">Read-only imported copy. Import the share again with continue mode to send messages.</div>;
    }
    return <Renderers.composer
      widgetContext={widgetContext}
      input={input}
      placeholder={isCentered ? getNewConversationPlaceholder() : placeholder}
      isNewConversation={isCentered}
      isGenerating={isGenerating || isConversationPending}
      selectedProfile={activeProfile}
      favoriteProfiles={favoriteProfiles}
      modelProfiles={selectableModelProfiles}
      modelSelectorSchema={modelSelectorSchema}
      thinkingLevel={activeProfile?.supports_thinking ? selectedThinkingLevel : null}
      contextUsage={contextUsage}
      inlineExtensions={composerExtensions}
      belowExtensions={[]}
      skillExtensions={composerSkills}
      commands={composerCommands}
      composerInput={composerInputMetadata}
      structuredInputValues={effectiveStructuredComposerValues}
      modelCommandCandidates={modelCommandCandidates}
      modelPickerRequestId={modelPickerRequestId}
      modelStatusIndicators={composerModelStatusIndicators}
      voiceInputEnabled={settingsValues.general?.voice_input_enabled !== false}
      voiceInputUseAi={settingsValues.general?.voice_input_use_ai === true}
      manualRuntimeModeSelectionEnabled={allowManualRuntimeModeSelection}
      mode={mode}
      codingContext={codingContext}
      codingWorkspaces={codingWorkspaces}
      selectedCodingWorkspaceId={effectiveWorkspaceId}
      projects={composerProjects}
      selectedProjectId={effectiveGroupId}
      attachedFiles={attachedFiles}
      pendingMentionAttachmentPaths={pendingMentionAttachmentPaths}
      droppedWidgets={activeDroppedWidgets}
      entityReferences={composerEntityReferences}
      selectedToolIds={selectedToolIds}
      actionApprovalMode={actionApprovalMode}
      toolSelectionTargets={toolSelectionController.state.overrideChips}
      toolSelectionReview={toolSelectionController.state.pendingReview}
      keyboardButtonNavigation={keyboardButtonNavigation}
      steerStatus={modelSteerStatus}
      steerBusy={modelSteerBusy}
      steerQueuedCount={steerItems.filter((item) => item.status === "queued").length}
      steerPreviewItems={isCentered ? [] : activeComposerSteerItems(steerItems, isGenerating || isConversationPending)}
      suppressPopovers={Boolean(visibleBrowserApproval || authorityApproval || runtimeApproval || staleRuntimeApprovalNotice)}
      onOpenModelManager={() => openSettingsSection("models")}
      onOpenToolSettings={() => openSettingsSection("tools")}
      onActionApprovalModeChange={handleActionApprovalModeChange}
      onToolSelectionTargetRemove={handleToolSelectionTargetRemove}
      onToolSelectionReviewApprove={handleToolReviewApprove}
      onToolSelectionReviewEdit={handleToolReviewEdit}
      onToolSelectionReviewNoTools={handleToolReviewNoTools}
      onToolSelectionReviewCancel={handleToolReviewCancel}
      onSwitchToVisionModel={handleSwitchToVisionModel}
      onExtensionSelect={handleComposerExtensionSelect}
      onCommandSelect={handleComposerCommand}
      onModelCommandCandidateSelect={handleModelCommandCandidateSelect}
      onModelCommandCandidatesClose={() => setComposerCandidateMenu(null)}
      onModelProfileSelect={handleModelProfileSelect}
      onProviderApiKeySave={handleProviderApiKeySave}
      onThinkingLevelChange={handleThinkingLevelChange}
      onInputChange={handleComposerInputChange}
      onStructuredInputChange={setStructuredComposerValues}
      onSubmit={handleSubmit}
      onStopGenerating={handleStopGenerating}
      onSteerSubmit={(prompt) => void queueConversationSteer(prompt)}
      onModeChange={handleModeChange}
      onFileAttach={handleFileAttach}
      onAtFileAttach={handleAtFileAttach}
      onPendingMentionAttachmentRemove={handlePendingMentionAttachmentRemove}
      onFileRemove={handleFileRemove}
      onDropWidget={handleDropWidget}
      onEntityReferencesChange={setComposerEntityReferences}
      onWidgetAction={handleWidgetAction}
      onWidgetToggle={handleWidgetToggle}
      onCodingBranchSwitch={handleCodingBranchSwitch}
      onCodingDirectoryChange={handleCodingDirectoryChange}
      onCodingWorkspaceSelect={handleCodingWorkspaceSelect}
      onCodingWorkspaceTrust={handleCodingWorkspaceTrust}
      onCodingWorkspaceCreate={handleCodingWorkspaceCreate}
      onCodingWorkspacesRefresh={() => void loadCodingWorkspaces()}
      onCodingContextRefresh={loadCodingContext}
      onProjectSelect={handleComposerProjectSelect}
      onProjectDirectorySelect={handleDirectorySelect}
      onProjectStoragePrepare={handlePrepareChatGroupStorage}
    />;
  };

  if (isLoading) {
    return (
      <TobkiriLoadingScreen
        error={startupError}
        onRetry={startupError ? () => window.location.reload() : undefined}
        steps={startupSteps}
      />
    );
  }

  return (
    <RendererBoundary>
    <div className="rumi-app-shell flex h-screen min-h-0 w-full flex-col overflow-hidden bg-[#09090b] font-sans text-zinc-300 selection:bg-zinc-800">
      {showRegion("title_bar") && <Renderers.titleBar appName={composerHomeTitle || catalog?.app?.name} appIcon={catalog?.app?.icon} />}

      <div className="rumi-shell-body flex min-h-0 flex-1">
        {showRegion("history") && !isHistoryMinimized && (
          <div className="rumi-history-pane rumi-layer-panel w-[286px] max-w-[30vw] min-w-[240px] flex-shrink-0 overflow-hidden border-r border-zinc-800/60 animate-in slide-in-from-left-2 fade-in duration-200 ease-out max-[900px]:w-[260px] rumi-anim-fade-left">
            <Renderers.historyBoard
              activeChatId={activeConversationId}
              chatItems={chatItems}
              account={catalog?.app?.account}
              onChatSelect={handleHistoryClick}
              onNewTask={handleNewTask}
              codingWorkspaces={codingWorkspaces}
              selectedCodingWorkspaceId={effectiveWorkspaceId}
              onCodingWorkspaceCreate={handleCodingWorkspaceCreate}
              onDirectorySelect={handleDirectorySelect}
              onGroupDataPathPrepare={handlePrepareChatGroupStorage}
              onCodingWorkspacesRefresh={async () => {
                await loadCodingWorkspaces();
              }}
              onCalendarOpen={handleCalendarModeToggle}
              isCalendarActive={isCalendarMode}
              onKanbanOpen={handleKanbanModeToggle}
              onGroupKanbanOpen={handleHistoryGroupKanbanOpen}
              onGroupSelect={handleHistoryGroupSelect}
              isKanbanActive={isKanbanMode}
              onDesktopsOpen={handleDesktopsModeOpen}
              isDesktopsActive={isDesktopsWorkspace}
              onSettingsClick={openSettingsHome}
              onChatMetadataChange={handleHistoryMetadataChange}
              onMinimize={() => setIsHistoryMinimized(true)}
            />
          </div>
        )}

        {showRegion("history") && isHistoryMinimized && (
          <div className="rumi-history-rail w-14 flex-shrink-0 overflow-visible border-r border-zinc-800/60 animate-in slide-in-from-left-1 fade-in duration-150 ease-out rumi-anim-fade-left">
            <Renderers.historyBoard
              activeChatId={activeConversationId}
              chatItems={chatItems}
              account={catalog?.app?.account}
              onChatSelect={handleHistoryClick}
              onNewTask={handleNewTask}
              codingWorkspaces={codingWorkspaces}
              selectedCodingWorkspaceId={effectiveWorkspaceId}
              onCodingWorkspaceCreate={handleCodingWorkspaceCreate}
              onDirectorySelect={handleDirectorySelect}
              onGroupDataPathPrepare={handlePrepareChatGroupStorage}
              onCodingWorkspacesRefresh={async () => {
                await loadCodingWorkspaces();
              }}
              onCalendarOpen={handleCalendarModeToggle}
              isCalendarActive={isCalendarMode}
              onKanbanOpen={handleKanbanModeToggle}
              onGroupKanbanOpen={handleHistoryGroupKanbanOpen}
              onGroupSelect={handleHistoryGroupSelect}
              isKanbanActive={isKanbanMode}
              onDesktopsOpen={handleDesktopsModeOpen}
              isDesktopsActive={isDesktopsWorkspace}
              onSettingsClick={openSettingsHome}
              onChatMetadataChange={handleHistoryMetadataChange}
              onRestore={() => setIsHistoryMinimized(false)}
              isCompact
            />
          </div>
        )}

        <main
          className={cn("rumi-workspace-main relative flex min-h-0 min-w-0 flex-1 bg-[#09090b]", isActivityPreviewVisible && "has-activity-preview")}
          style={{ "--rumi-activity-preview-width": `${activityPreviewWidthPx}px` } as CSSProperties}
          onDragEnter={handleWorkspaceFileDragEnter}
          onDragOver={handleWorkspaceFileDragOver}
          onDragLeave={handleWorkspaceFileDragLeave}
          onDrop={handleWorkspaceFileDrop}
        >
          {isWorkspaceFileDragActive && (
            <div
              role="status"
              aria-label="ファイルをここにドロップ"
              className="pointer-events-none absolute inset-0 rumi-layer-modal flex items-center justify-center bg-black/75 backdrop-blur-[2px]"
            >
              <div className="mx-6 flex max-w-md flex-col items-center rounded-3xl border border-dashed border-sky-300/55 bg-[#15171c]/95 px-10 py-9 text-center shadow-2xl">
                <Cloud className="mb-4 text-sky-200" size={42} strokeWidth={1.6} aria-hidden="true" />
                <p className="text-xl font-semibold text-zinc-50">ここにドロップ</p>
                <p className="mt-2 text-sm text-zinc-400">画像やファイルを会話に追加します</p>
              </div>
            </div>
          )}
          <div className={cn("rumi-chat-pane flex min-h-0 min-w-0 flex-1 flex-col rumi-anim-fade-up", isActivityPreviewVisible && "border-r border-zinc-800/40")}>
            <WorkspaceTabBar
              tabs={workspaceTabs}
              activeTabId={activeWorkspaceTabId}
              onSelect={handleWorkspaceTabSelect}
              onClose={handleWorkspaceTabClose}
              onCreate={handleWorkspaceTabCreate}
            />

            {showRegion("chat_header") && isChatWorkspace && !isCalendarMode && !isKanbanMode && (
              <Renderers.chatHeader
                title={activeWorkspaceTab ? workspaceTabDisplayTitle(activeWorkspaceTab) : activeChatTitle}
                showPreview={effectiveShowPreview}
                canShowPreview={showRegion("activity_preview") && canShowCanvas}
                canOpenSettings={showRegion("settings_modal")}
                onTogglePreview={() => {
                  if (canShowCanvas) setShowPreview((value) => !value);
                }}
                onOpenSettings={openSettingsHome}
              />
            )}

            {backendConnectionState !== "online" && (
              <ErrorNotice
                className="mx-3 mt-3 rounded-2xl px-4 py-3"
                copyLabel="Copy backend connection error"
                copyText={`${backendConnectionBanner.title}\n${backendConnectionBanner.detail}`}
                errorIcon={`backend-connection-${backendConnectionState}`}
                message={backendConnectionBanner.detail}
                severity={backendConnectionState === "offline" ? "error" : "warning"}
                title={backendConnectionBanner.title}
                trailing={(
                  <button
                    type="button"
                    onClick={() => void refreshHealth("focus")}
                    className="shrink-0 rounded-xl border border-current/20 px-3 py-1.5 text-[11px] font-semibold text-current transition hover:bg-white/5"
                  >
                    いま確認
                  </button>
                )}
              />
            )}

            {activeConversation?.metadata?.imported_from_share === true && provenanceDismissedFor !== activeConversation.id && (
              <ImportedConversationNotice importMode={activeConversation.metadata?.shared_import_mode} onDismiss={() => setProvenanceDismissedFor(activeConversation.id)} />
            )}

            {isDesktopsWorkspace ? (
              <DesktopMonitorWorkspace />
            ) : isKanbanMode ? (
              <KanbanWorkspacePanel
                scope={activeWorkspaceTab?.kanbanScope ?? { type: "global", id: "default" }}
                scopeLabel={activeWorkspaceTab?.kanbanScopeLabel ?? (activeWorkspaceTab ? workspaceTabDisplayTitle(activeWorkspaceTab) : "All Rumi Runs")}
                activeConversationId={activeConversationId}
                workspaceId={effectiveWorkspaceId}
                companyId={activeCompanyWorkspaceHint}
              />
            ) : isCalendarMode ? (
              <div className="flex min-h-0 flex-1 p-1.5">
                <CalendarComposerPanel
                  conversationId={activeConversationId}
                  modelId={activeModelId}
                  modelProfiles={selectableModelProfiles}
                  settings={calendarSettings}
                />
              </div>
            ) : isCodingWorkspace ? (
              <div className="flex min-h-0 flex-1 p-1.5">
                <CodingCockpit
                  variant="sidebar"
                  workspaces={codingWorkspaces}
                  selectedWorkspaceId={effectiveWorkspaceId}
                  consoleScopeKey={effectiveConsoleKey}
                  onWorkspaceSelect={handleCodingWorkspaceSelect}
                  onWorkspaceCreate={() => void handleCodingWorkspacePickCreate()}
                  onWorkspaceTrust={handleCodingWorkspaceTrust}
                  onWorkspacesRefresh={() => void loadCodingWorkspaces()}
                />
              </div>
            ) : isSubagentWorkspace ? (
              <div className="flex min-h-0 flex-1">
                <SubagentTeamWorkspace activeConversationId={activeConversationId} activeConversationTitle={activeChatTitle} />
              </div>
            ) : isCanvasWorkspace ? (
              <div className="flex min-h-0 flex-1 p-1.5">
                <div className="min-w-0 flex-1 overflow-hidden rounded-lg border border-zinc-800/70 bg-[#0a0a0c]">
                  <Renderers.toolPreviewPanel
                    widgetContext={widgetContext}
                    previews={canvasPreviews}
                    showPreview
                    onClose={() => {
                      const chatTab = workspaceTabs.find((tab) => tab.kind === "chat") ?? workspaceTabs[0];
                      if (chatTab) activateWorkspaceTab(chatTab);
                    }}
                    previewMode={previewMode}
                    onModeChange={setPreviewMode}
                    activePreviewId={activePreviewId}
                    memo={canvasMemo}
                    onMemoChange={setCanvasMemo}
                  />
                </div>
              </div>
            ) : isToolsWorkspace ? (
              <WorkspaceLaunchpad
                sidebarItems={sidebarItems}
                onCreate={handleWorkspaceTabCreate}
                onOpenSidebarItem={(itemId) => {
                  setActiveSidebarItemId(itemId);
                  setSidebarSelectionTick((value) => value + 1);
                }}
              />
            ) : isNewConversation && !isLoading ? (
              <div className={cn("rumi-new-chat-stage rumi-layer-local-popover flex flex-1 items-center justify-center px-5 pb-[10vh]", isNewChatLaunching && "is-launching")}>
                <div className="w-full">
                  <h1 className="rumi-greeting mx-auto mb-7 max-w-[720px] px-4 text-center text-[clamp(24px,3.2vw,44px)] font-medium leading-tight text-zinc-200">
                    {composerHomeTitle}
                  </h1>
                  {renderComposer(true)}
                </div>
              </div>
            ) : (
              <Renderers.chatMessages
                error={error}
                isMessagesRegionVisible={showRegion("chat_messages")}
                isLoading={isLoading}
                isNewConversation={isNewConversation}
                isGenerating={isGenerating || isConversationPending}
                pendingStatus={pendingRequest?.status ?? null}
                pendingToolNames={pendingRequest?.toolNames ?? []}
                pendingStartedAt={pendingRequest?.startedAt ?? null}
                pendingToolStartedAt={pendingRequest?.toolStartedAt ?? {}}
                messages={messages}
                messagesEndRef={messagesEndRef}
                messagesScrollRef={messagesScrollRef}
                onMessagesScroll={handleMessagesScroll}
                unknownBlockStrategy={unknownBlockStrategy}
                showActivityInMessages={showActivityInMessages}
                showWidgets={showWidgets}
                showPromptUsageInMessages={showPromptUsageInMessages}
                onSuggestionClick={(text) => setInput(text)}
                onOpenToolPreview={(previewId) => {
                  setActivePreviewId(previewId);
                  setShowPreview(true);
                }}
                onLoadPromptTrace={promptResources.getTraceUsage}
                onRetry={retryableSubmission && error === retryableSubmission.errorMessage ? handleRetryLastFailedSubmission : undefined}
                onDismissError={error ? dismissChatError : undefined}
              />
            )}

            {showRegion("composer") && isChatWorkspace && !isNewConversation && !isCalendarMode && !isKanbanMode && (
              <div className="relative">
                {showRegion("activity_preview") && !effectiveShowPreview && canShowCanvas && (
                  <CanvasPeek
                    previews={canvasPreviews}
                    memo={canvasMemo}
                    activePreviewId={activePreviewId}
                    onOpen={() => setShowPreview(true)}
                  />
                )}
                {visibleBrowserApproval && (
                  <ApprovalDecisionSurface
                    approval={browserApprovalViewModel(visibleBrowserApproval)}
                    onDeny={() => void denyBrowserAction()}
                    onApprove={() => void approveBrowserAction()}
                    keyboardShortcuts={{ deny: "2", approve: "3" }}
                    className="pointer-events-auto absolute bottom-full left-1/2 rumi-layer-modal mb-2 max-h-[min(70vh,620px)] w-[min(620px,calc(100vw-24px))] -translate-x-1/2 overflow-y-auto"
                  />
                )}
                {!visibleBrowserApproval && pendingCommandApproval && (
                  <ApprovalDecisionSurface
                    approval={commandApprovalViewModel(pendingCommandApproval)}
                    onDeny={() => void denyCommandAction()}
                    onApprove={() => void approveCommandAction()}
                    keyboardShortcuts={{ deny: "2", approve: "3" }}
                    className="pointer-events-auto absolute bottom-full left-1/2 rumi-layer-modal mb-2 max-h-[min(70vh,620px)] w-[min(620px,calc(100vw-24px))] -translate-x-1/2 overflow-y-auto"
                  />
                )}
                {!visibleBrowserApproval && !pendingCommandApproval && pendingHighRiskCommand && (
                  <section className="pointer-events-auto absolute bottom-full left-1/2 rumi-layer-modal mb-2 w-[min(560px,calc(100vw-32px))] -translate-x-1/2 rounded-xl border border-amber-500/30 bg-zinc-950 p-3 shadow-2xl">
                    <p className="text-sm font-medium text-zinc-100">高リスク操作の承認待ち</p>
                    <p className="mt-1 text-xs leading-5 text-zinc-400">
                      「{pendingHighRiskCommand.commandLabel}」は専用の承認ウィンドウで確認します。
                      この画面は承認トークンや実行対象を保持せず、承認後に Host が同じ操作を一度だけ再開します。
                    </p>
                    <button
                      type="button"
                      onClick={() => void openPendingHighRiskApproval()}
                      className="mt-3 h-9 rounded-lg border border-amber-400/35 bg-amber-400/10 px-3 text-xs font-semibold text-amber-100 hover:bg-amber-400/20"
                    >
                      承認ウィンドウを開く
                    </button>
                  </section>
                )}
                {commandProgressEvents.length > 0 && (
                  <section className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3" aria-label="Command progress">
                    <h3 className="text-xs font-semibold text-zinc-300">Command progress</h3>
                    <ol className="mt-2 space-y-1 text-[11px] text-zinc-500">
                      {commandProgressEvents.map((event, index) => (
                        <li key={`${String(event.invocation_id ?? "invocation")}:${String(event.sequence ?? index)}`}>
                          {String(event.sequence ?? "•")} · {String(event.type ?? "progress")}
                        </li>
                      ))}
                    </ol>
                  </section>
                )}
                {commandProtocolInfo && (
                  <details className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3">
                    <summary className="cursor-pointer text-xs font-semibold text-zinc-300">
                      Command catalog inspector · {commandProtocolInfo.commands.length} commands
                    </summary>
                    <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 text-[11px] text-zinc-500">
                      <dt>revision</dt><dd className="font-mono">{commandProtocolInfo.catalog_revision}</dd>
                      <dt>rollout</dt><dd>{commandProtocolInfo.rollout?.phase ?? "unavailable"}</dd>
                      <dt>diagnostics</dt><dd>{commandProtocolInfo.diagnostics?.length ?? 0}</dd>
                      <dt>events</dt><dd>{commandProgressEvents.length}</dd>
                    </dl>
                  </details>
                )}
                {!visibleBrowserApproval && !pendingCommandApproval && !pendingHighRiskCommand && authorityApproval && (
                  <AuthorityApprovalNotice
                    approval={authorityApproval}
                    title={authorityApprovalTitle(authorityApproval)}
                    onOpen={() => void openAuthorityApprovalWindowAction()}
                  />
                )}
                {!visibleBrowserApproval && !pendingCommandApproval && !pendingHighRiskCommand && !authorityApproval && runtimeApproval && (
                  <ApprovalDecisionSurface
                    approval={runtimeApprovalViewModel(runtimeApproval)}
                    onDeny={() => void denyCodingAction()}
                    onApprove={() => void approveCodingAction()}
                    keyboardShortcuts={{ deny: "2", approve: "3" }}
                    className="pointer-events-auto absolute bottom-full left-1/2 rumi-layer-modal mb-2 max-h-[min(70vh,620px)] w-[min(620px,calc(100vw-24px))] -translate-x-1/2 overflow-y-auto"
                  />
                )}
                {!visibleBrowserApproval && !pendingCommandApproval && !pendingHighRiskCommand && !authorityApproval && !runtimeApproval && staleRuntimeApprovalNotice && (
                  <div className="pointer-events-auto absolute bottom-full left-1/2 rumi-layer-modal mb-2 w-[min(560px,calc(100vw-32px))] -translate-x-1/2 rounded-xl border border-zinc-700 bg-zinc-950 p-3 shadow-2xl">
                    <div className="min-w-0">
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="shrink-0 rounded border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 text-[10px] text-zinc-400">
                          expired
                        </span>
                        <p className="truncate text-sm font-medium text-zinc-100">{staleRuntimeApprovalTitle(staleRuntimeApprovalNotice)}</p>
                      </div>
                      <p className="mt-1 truncate text-[11px] text-zinc-500">
                        古い承認カードを検出しました。最新の承認カードが届くと、この画面からそのまま許可できます。
                      </p>
                      <details className="mt-1 text-[11px] text-zinc-500">
                        <summary className="cursor-pointer select-none text-zinc-500 hover:text-zinc-300">payload を表示</summary>
                        <pre className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap rounded-md border border-zinc-800 bg-black/30 p-2 font-mono">
                          {approvalPayloadPreview(staleRuntimeApprovalNotice.payload)}
                        </pre>
                      </details>
                    </div>
                  </div>
                )}
                <div
                  ref={composerAlertAnchorRef}
                  data-testid="conversation-composer-anchor"
                  className="flex-shrink-0"
                >
                  {renderComposer(false)}
                </div>
              </div>
            )}
          </div>

          {isActivityPreviewVisible && (
            <div
              role="separator"
              aria-label="Canvas幅を変更"
              title="Canvas幅を変更"
              className="rumi-activity-preview-resize-handle"
              onPointerDown={startActivityPreviewResize}
            />
          )}

          {isActivityPreviewVisible && (
            <aside className="rumi-activity-preview-pane rumi-anim-fade-right" aria-label="Activity preview">
              <Renderers.toolPreviewPanel
                widgetContext={widgetContext}
                previews={canvasPreviews}
                showPreview={effectiveShowPreview}
                onClose={() => setShowPreview(false)}
                previewMode={previewMode}
                onModeChange={setPreviewMode}
                activePreviewId={activePreviewId}
                memo={canvasMemo}
                onMemoChange={setCanvasMemo}
              />
            </aside>
          )}
        </main>

        {showRegion("right_sidebar") && (
          <div className="rumi-anim-fade-right">
          <Renderers.rightSidebar
            widgetContext={widgetContext}
            items={sidebarItems}
            activeItemId={activeSidebarItemId ? `${activeSidebarItemId}:${sidebarSelectionTick}` : null}
            settingsValues={settingsValues}
            settingsSections={settingsSections}
            selectedToolIds={selectedToolIds}
            companyPanel={(
              <CompanyWorkspacePanel
                activeConversationId={activeConversationId}
                activeConversationTitle={activeChatTitle}
                activeCompanyIdHint={activeCompanyWorkspaceHint}
              />
            )}
            codingPanel={codingSidebarPanel}
            keyboardButtonNavigation={keyboardButtonNavigation}
            selectedProfile={activeProfile}
            toolFilterEntries={toolFilterEntries}
            runtimeCapabilitySnapshot={runtimeCapabilitySnapshot}
            contextUsage={contextUsage}
            promptUsage={activePromptUsage}
            promptProfileId={activePromptProfileId}
            conversationId={activeConversationId}
            showChatPromptUsage={showPromptUsageInMessages}
            onLoadPromptActive={promptResources.getActiveSummary}
            onTogglePromptEdge={promptResources.toggleEdge}
            onToggleChatPromptUsage={setShowPromptUsageInMessages}
            yoloMode={ultraYoloMode}
            workspaceTabs={workspaceTabs}
            activeWorkspaceTabId={activeWorkspaceTabId}
            activeConversationId={activeConversationId}
            onSettingChange={handleSettingChange}
            onOpenSettings={openSettingsHome}
            onOpenSettingsSection={openSettingsSection}
            onToggleYolo={() => setFullAccessEnabled(!ultraYoloMode)}
            onWorkspaceTabSelect={handleWorkspaceTabSelect}
            onWorkspaceTabClose={handleWorkspaceTabClose}
            onWorkspaceTabCreate={handleWorkspaceTabCreate}
            onToolToggle={(item) => toggleSelectedTool({
              id: item.id,
              label: item.label,
              category: item.category,
              description: item.description,
              tags: item.tags ?? [],
              ui: item.ui,
            })}
            onToolBatchSet={handleToolBatchSet}
            onPanelAction={handlePanelAction}
          />
          </div>
        )}
      </div>

      <ConversationSpotlight
        isOpen={isSpotlightOpen}
        query={spotlightQuery}
        filter={spotlightFilter}
        results={visibleSpotlightResults}
        selectedIndex={spotlightSelectedIndex}
        loading={spotlightLoading}
        locale={locale}
        shortcutLabel={spotlightShortcutLabel}
        onQueryChange={setSpotlightQuery}
        onFilterChange={setSpotlightFilter}
        onKeyDown={handleSpotlightKeyDown}
        onClose={closeSpotlight}
        onOpenResult={openSpotlightResult}
      />

      {showRegion("settings_modal") && (
        <Renderers.settingsModal
          isOpen={isSettingsOpen}
          activeSectionId={requestedSettingsSectionId}
          catalog={catalog}
          health={health}
          previewsCount={canvasPreviews.length}
          settingsSections={settingsSections}
          settingsValues={settingsValues}
          desktopSystemInfo={desktopSystemInfo}
          modelProfiles={settingsModelProfiles}
          activeModelProfileId={activeProfile?.profile_id ?? activeModelId}
          backendConnectionState={backendConnectionState}
          backendConnectionNote={backendConnectionNote}
          saveState={settingsSaveState}
          loadState={settingsLoadState}
          modelProfilesLoadState={modelProfilesLoadState}
          locale={locale}
          onClose={() => setIsSettingsOpen(false)}
          onStartSettingsChat={startSettingsChat}
          onOpenSection={openSettingsSection}
          onRetryLoad={() => { void refreshCatalog(); }}
          onRetrySave={retrySettingsSave}
          onSettingChange={handleSettingChange}
        />
      )}

      <TransientAlert
        alert={transientAlert}
        onDismiss={() => setTransientAlert(null)}
        placement={transientAlertPlacement}
        anchorRef={composerAlertAnchorRef}
      />

      <AmbientWindowLauncher enabled={Boolean(settingsValues.ambient?.["ambient.monitor.enabled"])} />
      {shareDialogOpen && (
        <LayerPortal layer="globalOverlay">
          <div className="fixed inset-0 flex items-center justify-center bg-black/70 p-4" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) setShareDialogOpen(false); }}>
            <section role="dialog" aria-modal="true" aria-labelledby="share-dialog-title" className="w-full max-w-lg border border-zinc-700 bg-zinc-950 p-5 shadow-2xl">
              <div className="flex items-center justify-between gap-4">
                <h2 id="share-dialog-title" className="text-lg font-semibold text-zinc-100">Share conversation</h2>
                <button autoFocus type="button" title="Close" aria-label="Close share dialog" onClick={() => setShareDialogOpen(false)} className="inline-flex h-8 w-8 items-center justify-center text-zinc-400 hover:bg-zinc-900 hover:text-white"><X size={17} /></button>
              </div>
              <p className="mt-2 text-sm leading-6 text-zinc-400">The transcript is redacted before sharing. Attachments and executable permissions are never included.</p>
              <label className="mt-4 block text-xs font-medium text-zinc-400">Link expiry
                <select value={shareExpiryHours} onChange={(event) => setShareExpiryHours(event.target.value)} className="mt-2 h-10 w-full border border-zinc-700 bg-zinc-900 px-3 text-sm text-zinc-100">
                  <option value="1">1 hour</option>
                  <option value="24">24 hours</option>
                  <option value="168">7 days</option>
                  <option value="never">No expiry</option>
                </select>
              </label>
              <div className="mt-5 grid gap-2 sm:grid-cols-2">
                <button type="button" disabled={shareBusy} onClick={() => void createConversationShare("local")} className="flex min-h-20 items-start gap-3 border border-zinc-700 p-3 text-left hover:bg-zinc-900 disabled:opacity-60"><Link size={18} className="mt-0.5 text-emerald-300" /><span><strong className="block text-sm text-zinc-100">Local share link</strong><span className="mt-1 block text-xs leading-5 text-zinc-500">Private to this defaultspack host.</span></span></button>
                <button type="button" disabled={shareBusy} onClick={() => void createConversationShare("tunnel")} className="flex min-h-20 items-start gap-3 border border-zinc-700 p-3 text-left hover:bg-zinc-900 disabled:opacity-60"><Cloud size={18} className="mt-0.5 text-sky-300" /><span><strong className="block text-sm text-zinc-100">Cloudflare Tunnel link</strong><span className="mt-1 block text-xs leading-5 text-zinc-500">Public through the configured hostname.</span></span></button>
              </div>
              {shareBusy && <p role="status" className="mt-4 flex items-center gap-2 text-sm text-zinc-400"><Loader2 size={15} className="animate-spin" /> Creating redacted bundle...</p>}
              {shareDialogError ? (
                <ErrorNotice
                  className="mt-4 text-sm"
                  copyLabel="Copy share dialog error"
                  copyText={shareDialogError}
                  errorIcon="share-dialog"
                  message={shareDialogError}
                />
              ) : null}
              {shareCreatedUrl && <div className={`mt-4 border p-3 ${shareRevoked ? "border-red-500/25 bg-red-500/10" : "border-emerald-500/25 bg-emerald-500/10"}`}><p className={`break-all text-sm ${shareRevoked ? "text-red-100 line-through" : "text-emerald-100"}`}>{shareCreatedUrl}</p><div className="mt-3 flex flex-wrap gap-2">{!shareRevoked && <button type="button" onClick={() => void navigator.clipboard.writeText(new URL(shareCreatedUrl, window.location.origin).toString())} className="inline-flex h-9 items-center gap-2 border border-emerald-300/25 px-3 text-xs font-semibold text-emerald-100 hover:bg-emerald-500/10"><Copy size={14} /> Copy link</button>}{shareCreatedToken && !shareRevoked && <button type="button" onClick={() => void api.revokeShare(shareCreatedToken).then(() => setShareRevoked(true)).catch((reason) => setShareDialogError(reason instanceof Error ? reason.message : "Could not revoke link."))} className="inline-flex h-9 items-center gap-2 border border-red-400/25 px-3 text-xs font-semibold text-red-200 hover:bg-red-500/10"><X size={14} /> Revoke link</button>}</div>{shareRevoked && <p role="status" className="mt-2 text-xs text-red-200">Revoked. This link can no longer be viewed or imported.</p>}</div>}
              <button type="button" onClick={() => { if (activeConversationId) void handlePanelAction({} as SidebarItem, { id: "conversation.export" } as SidebarAction); }} className="mt-5 inline-flex h-10 items-center gap-2 text-sm text-zinc-300 hover:text-white"><Download size={16} /> Export history.json</button>
            </section>
          </div>
        </LayerPortal>
      )}
    </div>
    </RendererBoundary>
  );
}

function AmbientWindowLauncher({ enabled }: { enabled: boolean }) {
  const [opening, setOpening] = useState(false);
  const [fallbackVisible, setFallbackVisible] = useState(false);
  if (!enabled) return null;

  const openWindow = async () => {
    if (opening) return;
    setOpening(true);
    setFallbackVisible(false);
    try {
      const opened = await openFingerRecordingWindow();
      if (opened) return;
      const popup = window.open(
        "/finger-recording",
        "rumi-finger-recording",
        "width=380,height=520,noopener,noreferrer",
      );
      if (popup) popup.focus();
      else setFallbackVisible(true);
    } catch {
      setFallbackVisible(true);
    } finally {
      setOpening(false);
    }
  };

  return (
    <LayerPortal layer="globalOverlay">
      <div className="fixed bottom-4 right-4 flex flex-col items-end gap-2">
        {fallbackVisible && (
          <div className="max-w-64 rounded-lg border border-amber-300/25 bg-zinc-950/95 px-3 py-2 text-xs leading-5 text-amber-50 shadow-xl shadow-black/40">
            Tobkiri Launcherから開くと、指録音は専用ウィンドウで表示されます。
          </div>
        )}
        <button
          type="button"
          onClick={() => void openWindow()}
          disabled={opening}
          className="inline-flex h-10 items-center gap-2 rounded-lg border border-zinc-700/80 bg-zinc-950/92 px-3 text-sm font-semibold text-zinc-100 shadow-xl shadow-black/40 backdrop-blur hover:border-zinc-500 hover:bg-zinc-900 disabled:cursor-wait disabled:opacity-70"
          title="指で録音ウィンドウを開く"
          aria-label="指で録音ウィンドウを開く"
        >
          {opening ? <Loader2 size={16} className="animate-spin" /> : <Hand size={16} />}
          指録音
        </button>
      </div>
    </LayerPortal>
  );
}

export default function App() {
  const pathname = window.location.pathname;
  const searchParams = new URLSearchParams(window.location.search);
  const fingerDebugMode = pathname === "/ambient-debug"
    || searchParams.get("debug") === "1"
    || searchParams.get("qa") === "debug";
  const explicitDebugConversationId = fingerDebugMode ? chatIdFromLocation() : null;

  if (pathname === "/approval") {
    return <AuthorityApprovalWindow />;
  }
  if (pathname === "/ui-precision" || searchParams.get("ui-precision") === "1") {
    return <UiPrecisionComparator />;
  }
  if (pathname.startsWith("/share/")) {
    return <ConversationShareLanding />;
  }
  if (pathname === "/ambient") {
    return <AmbientTriggerPanel variant="window" />;
  }
  if (pathname === "/ambient-debug" || pathname === "/finger-recording") {
    return <AmbientTriggerPanel variant="window" debugMode={fingerDebugMode} conversationId={explicitDebugConversationId} />;
  }
  if (pathname === "/console") {
    return <DefaultsConsoleWindow />;
  }
  if (pathname === "/host-permissions") {
    return <HostPermissionsPage />;
  }
  if (pathname === "/adaptive" || pathname === "/operating-profile") {
    return <AdaptiveRuntimePage />;
  }
  if (pathname === "/defaultspack" || pathname === "/pack/defaultspack" || pathname === "/chat" || pathname === "/calendar") {
    return <ChatApp />;
  }
  return <ChatApp />;
}
