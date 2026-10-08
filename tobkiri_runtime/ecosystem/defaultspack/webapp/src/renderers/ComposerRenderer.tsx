import { modelProfileConnectionId } from "../features/models/modelSelectionIdentity";
import type { ComposerEntityCandidate } from "../lib/composerEntityCandidates";
import { HISTORY_REFERENCE_DROP_MIME, HISTORY_REFERENCE_DROP_EVENT, type HistoryReferenceDropDetail } from "../lib/historyReferences";
import { anchorComposerMentionWidget, confirmedComposerMentionRange, updateConfirmedComposerWidgets } from "../lib/composerMentionAnchors";
import { ComposerRegisteredModelDropdown as ModelDropdown } from "./ComposerRegisteredModelDropdown";
export { ComposerRegisteredModelDropdown as ModelDropdown } from "./ComposerRegisteredModelDropdown";
import { mentionConfirmationKey } from "../features/search/searchQueryState";
import { isLocalTaskPetCommand, isTaskPetCommandInput } from "../lib/taskPetCommand";
import {
  Activity,
  ArrowUp,
  AtSign,
  BadgeDollarSign,
  Blocks,
  Bot,
  Box,
  Brain,
  BrainCircuit,
  Braces,
  Bug,
  ChartNoAxesCombined,
  Check,
  ChevronDown,
  CircleHelp,
  CircleAlert,
  Clock3,
  CloudUpload,
  Code2,
  CodeXml,
  CornerDownRight,
  Cpu,
  Database,
  Download,
  Eraser,
  File,
  FileDiff,
  FileText,
  Files,
  FlaskConical,
  Folder,
  GitBranch,
  GitCommitHorizontal,
  GitCompare,
  GitFork,
  KeyRound,
  Keyboard,
  ListChecks,
  Loader2,
  Maximize2,
  MessageSquare,
  MessageSquarePlus,
  MessagesSquare,
  Minimize2,
  MousePointerClick,
  Paintbrush,
  Palette,
  PanelRightOpen,
  Pencil,
  Play,
  Plug,
  Plus,
  Search,
  ScanSearch,
  ScrollText,
  Settings2,
  ShieldCheck,
  ShieldPlus,
  ShieldQuestion,
  SlidersHorizontal,
  Sparkles,
  Square,
  SquareTerminal,
  Stethoscope,
  Webhook,
  Wrench,
  X,
  Zap,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useId, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";

import type {
  AttachedFile,
  ComposerCommandItem,
  ComposerExtensionItem,
  ComposerModelStatusIndicator,
  ComposerRendererProps,
  ComposerSkillItem,
  DroppedWidget,
  AppMode,
  ToolGroup,
} from "./types";
import type { ModelCommandCandidate, ModelProfile, ModelSearchItem } from "../lib/api";
import { CodingWorkspaceBadge } from "../components/coding/CodingWorkspaceBadge";
import { CodingWorkspacePicker } from "../components/coding/CodingWorkspacePicker";
import { ErrorCopyAction, ErrorNotice } from "../components/ErrorNotice";
import { RuntimeCapabilityBanner } from "../components/RuntimeCapabilityBanner";
import { ViewportPopover } from "../ui/layers/ViewportPopover";
import { StructuredComposerPanel } from "../components/StructuredComposerPanel";
import { WarmActionIcon } from "../components/WarmActionIcon";
import {
  DEFAULT_MODEL_SELECTOR_SCHEMA,
  filterModelProfilesBySelector,
  modelSelectorSchemaForSurface,
} from "../features/models";
import { ActionApprovalControl } from "../features/tools/ActionApprovalControl";
import { ToolModeControl } from "../features/tools/ToolModeControl";
import { ProjectPicker } from "../features/projects/ProjectPicker";
import { ToolSelectionReviewCard } from "../features/tools/ToolSelectionReviewCard";
import { composerToolMentionGroups } from "../lib/composerToolMentions";
import {
  applyComposerVoiceTranscript,
  assertComposerMicrophoneAllowed,
  composerVoiceErrorMessage,
  composerVoiceLanguage,
  ComposerVoiceOperation,
  isAudioAttachment,
  readableTranscriptionError,
  requestComposerAudioTranscript,
  restoreComposerVoiceSelection,
  transcriptAttachmentFromAudio,
  type ComposerVoiceInsertMode,
  type ComposerVoicePhase,
} from "../features/voice/composerVoice";
import { fileToAttachment } from "../lib/attachments";
import { composerFileMentionWidget, composerKnownMentionValues, composerServiceMentionWidget, composerSkillMentionDisplay, composerSkillMentionWidget, composerToolMentionDisplay, composerToolMentionWidget, filterComposerSkillMentions, filterComposerToolMentions, resolveComposerWidgetDrop, skillMentionIdsFromText, toolMentionIdsFromText, widgetWithCurrentPresentation, composerMentionMetadataFromWidgets } from "../lib/composerWidgets";
import { WidgetAttentionIcon } from "../lib/widgetAttention";
import {
  COMPOSER_REFERENCE_MIME,
  composerReferencesAsMarkdown,
  insertComposerReferencePaste,
  mergeComposerReferences,
  restoreComposerMarkdownReferences,
  restoreComposerReferences,
  serializeComposerReferences,
  type ComposerEntityReference,
} from "../lib/composerReferences";
import { activeMentionAtCursor } from "../lib/mentionContract";
import { insertAtMentionText } from "../lib/composerMentionInsertion";
export { insertAtMentionText } from "../lib/composerMentionInsertion";
import { withSettingsAssistantSkill } from "../lib/settingsMode";
import { sortedToolGroups, toolGroupFor } from "../lib/toolUi";
import { declarativeIconForName } from "../lib/declarativeIcons";
import { startPinchAudioRecorder, type ActiveAudioRecorder } from "../ambient/ambientMedia";
import composerPaletteTemplateJson from "../templates/composerPalette.template.json" with { type: "json" };

export { composerSkillMentionDisplay, composerSkillMentionWidget, composerToolMentionDisplay, composerToolMentionWidget, filterComposerSkillMentions, filterComposerToolMentions, resolveComposerWidgetDrop, skillMentionIdsFromText, toolMentionIdsFromText } from "../lib/composerWidgets";

export type ComposerSubmissionLock = {
  signature: string;
  submittedAt: number;
};

export function composerSubmissionSignature(
  input: string,
  attachmentIds: string[],
): string {
  return JSON.stringify([input.trim(), [...attachmentIds].sort()]);
}

export function isDuplicateComposerSubmission(
  previous: ComposerSubmissionLock | null,
  signature: string,
  now = Date.now(),
  windowMs = 700,
): boolean {
  return Boolean(previous && previous.signature === signature && now - previous.submittedAt >= 0 && now - previous.submittedAt < windowMs);
}

export function isComposerImeEvent(event: {
  key?: string;
  keyCode?: number;
  nativeEvent?: { isComposing?: boolean; keyCode?: number };
}, composition: { active?: boolean; endedAt?: number; now?: number } = {}): boolean {
  const sinceEnd = (composition.now ?? Date.now()) - (composition.endedAt ?? -Infinity);
  return composition.active === true || event.keyCode === 229
    || event.nativeEvent?.keyCode === 229 || event.nativeEvent?.isComposing === true
    || (event.key === "Enter" && sinceEnd >= 0 && sinceEnd < 50);
}

const THINKING_LABELS: Record<string, string> = {
  none: "なし",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "最高",
};

const MODE_META: Record<AppMode, { label: string; icon: typeof MessageSquare; description: string }> = {
  chat: { label: "Chat", icon: MessageSquare, description: "通常チャット" },
  coding: { label: "Coding", icon: Code2, description: "コード編集・Git操作" },
  agent: { label: "Agent", icon: Bot, description: "自律エージェント" },
};

type ComposerChromeWidth = {
  basis: string;
  min?: string;
  max?: string;
  grow?: number;
  shrink?: number;
};

type ComposerChromeSlot = "leading" | "trailing";
type ComposerHomeSlot = "editor-leading" | "editor-trailing" | "toolbar-leading" | "toolbar-trailing";

type ComposerChromeWidgetSpec = {
  id: string;
  slot: ComposerChromeSlot;
  homeSlot?: ComposerHomeSlot;
  order: number;
  visible?: boolean;
  mobile?: "show" | "hide";
  width: ComposerChromeWidth;
  className?: string;
  render: () => ReactNode;
};

const COMPOSER_CHROME_WIDTHS = {
  icon: { basis: "44px", min: "44px", max: "44px" },
  mode: { basis: "auto", min: "2rem", max: "7rem", shrink: 1 },
  badge: { basis: "auto", min: "0", max: "11rem", shrink: 1 },
  thinking: { basis: "5.25rem", min: "5.25rem", max: "5.25rem", shrink: 0 },
  status: { basis: "auto", min: "2.5rem", shrink: 0 },
  send: { basis: "44px", min: "44px", max: "44px" },
  sendLarge: { basis: "44px", min: "44px", max: "44px" },
} satisfies Record<string, ComposerChromeWidth>;

const COMPOSER_CONTROL_SURFACE_CLASSNAME = "rumi-composer-control-surface flex h-[44px] min-h-[44px] min-w-0 items-center rounded-lg px-2.5";
export const COMPOSER_ATTACH_COMMAND_ID = "composer.attach_files";
export const COMPOSER_ATTACH_IMAGE_COMMAND_ID = "composer.attach_images";

export function composerMenuCommands(commands: ComposerCommandItem[], allowFiles: boolean, allowCommands: boolean): ComposerCommandItem[] {
  const availableCommands = allowCommands ? commands : [];
  if (!allowFiles) return availableCommands;
  return [
    {
      id: COMPOSER_ATTACH_IMAGE_COMMAND_ID,
      name: "image",
      aliases: ["photo", "画像", "写真"],
      label: "画像を追加",
      description: "画像を選択して会話に添付",
      category: "chat",
      visibility: "default",
      risk: "low",
      execution: { type: "frontend", action: "attach_images" },
    },
    {
      id: COMPOSER_ATTACH_COMMAND_ID,
      name: "attach",
      aliases: ["file", "添付"],
      label: "ファイルを添付",
      description: "写真やファイルを追加",
      category: "chat",
      visibility: "default",
      risk: "low",
      execution: { type: "frontend", action: "attach_files" },
    },
    ...availableCommands,
  ];
}

const AT_MENTION_LISTBOX_ID = "composer-at-mention-listbox";
const COMPOSER_MODEL_CONTROL_MIN_CH = 9;
const COMPOSER_MODEL_CONTROL_MAX_CH = 18;
const COMPOSER_MODEL_CONTROL_CHROME_CH = 6;
const NEW_CONVERSATION_TEXTAREA_MIN_HEIGHT = 22;
const NEW_CONVERSATION_TEXTAREA_MAX_HEIGHT = 240;
const CONVERSATION_TEXTAREA_MIN_HEIGHT = 56;
const CONVERSATION_TEXTAREA_MAX_HEIGHT = 240;
const COLLAPSED_TEXTAREA_MAX_HEIGHT = 72;
const TEXTAREA_COLLAPSE_THRESHOLD = 104;
const useIsomorphicLayoutEffect = typeof window === "undefined" ? useEffect : useLayoutEffect;
const MODEL_STATUS_POPOVER_WIDTH = 240;
const MODEL_STATUS_POPOVER_HEIGHT = 176;
const MODEL_STATUS_POPOVER_GAP = 10;
const MODEL_STATUS_POPOVER_VIEWPORT_MARGIN = 16;
const TEMPLATE_COMPOSER_TEXT_MAX = 180;
const TEMPLATE_COMPOSER_MODALITY_LABELS: Record<string, string> = {
  text: "Text",
  file: "Files",
  files: "Files",
  image: "Images",
  images: "Images",
  audio: "Audio",
  voice: "Voice",
  speech: "Voice",
};
const TEMPLATE_COMPOSER_FEATURE_LABELS: Record<string, string> = {
  slash_commands: "Slash",
  at_mentions: "Mentions",
  tool_mentions: "Tools",
  file_attachments: "Files",
  voice_input: "Voice",
  context_preview: "Context",
};

export function composerChromeWidgetStyle(width: ComposerChromeWidth): CSSProperties {
  return {
    flex: `${width.grow ?? 0} ${width.shrink ?? 0} ${width.basis}`,
    minWidth: width.min,
    maxWidth: width.max,
  };
}

function fitComposerTextareaHeight(textarea: HTMLTextAreaElement, minHeight: number, maxHeight: number, overlayHeight = 0) {
  textarea.style.height = "auto";
  const contentHeight = Math.max(textarea.scrollHeight, overlayHeight);
  const nextHeight = Math.min(Math.max(contentHeight, minHeight), maxHeight);
  textarea.style.height = `${nextHeight}px`;
  textarea.style.overflowY = contentHeight > maxHeight ? "auto" : "hidden";
}

function templateComposerText(value: unknown, maxLength = TEMPLATE_COMPOSER_TEXT_MAX): string {
  if (typeof value !== "string") return "";
  return value.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim().slice(0, maxLength);
}

function normalizedTemplateComposerList(value: unknown): string[] {
  const rawItems = Array.isArray(value) ? value : typeof value === "string" ? value.split(",") : [];
  const normalized = rawItems
    .map((item) => String(item ?? "").trim().toLowerCase())
    .filter(Boolean)
    .slice(0, 8);
  return [...new Set(normalized)];
}

function templateComposerFeatureFlags(value: unknown): Record<string, boolean> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const flags: Record<string, boolean> = {};
  Object.entries(value as Record<string, unknown>).forEach(([key, flag]) => {
    if (typeof flag === "boolean") flags[key] = flag;
  });
  return flags;
}

function looksLikeInternalComposerCopy(value: string): boolean {
  return /template-composed composer|context txt materialization|slash commands, mentions, files|context text|会話をtxt化/i.test(value);
}

export function composerPlaceholderCopy({
  isSteerMode,
  mode,
  placeholder,
  templatePlaceholder,
}: {
  isSteerMode: boolean;
  mode: AppMode;
  placeholder?: string;
  templatePlaceholder?: string;
}): string {
  if (isSteerMode) return "追加の指示を入力";
  const templateCopy = templateComposerText(templatePlaceholder);
  if (templateCopy && !looksLikeInternalComposerCopy(templateCopy)) return templateCopy;
  if (mode === "coding") return "変更したい内容を入力...";
  if (mode === "agent") return "タスクを入力...";
  return placeholder || "メッセージを入力...";
}

export function composerHelperCopy({
  isSteerMode,
  hasInput,
  slashCommands,
  atMentions,
  fileAttachments,
  templateHelp,
}: {
  isSteerMode: boolean;
  hasInput: boolean;
  slashCommands: boolean;
  atMentions: boolean;
  fileAttachments: boolean;
  templateHelp?: string;
}): string {
  if (isSteerMode) return hasInput ? "Enterで追加指示を送信" : "実行中の応答へ追加指示できます";
  const help = templateComposerText(templateHelp, 120);
  if (help && !looksLikeInternalComposerCopy(help)) return help;
  const hints = ["Enterで送信"];
  if (slashCommands) hints.push("/ でコマンド");
  if (atMentions) hints.push("@ で候補");
  else if (fileAttachments) hints.push("ファイル添付対応");
  return hints.join(" · ");
}

function SendButtonIcon({ className = "", size = 16 }: { className?: string; size?: number }) {
  return <ArrowUp aria-hidden="true" size={size} strokeWidth={2.4} className={className} />;
}

function ComposerTextareaResizeButton({
  collapsed,
  visible,
  onToggle,
}: {
  collapsed: boolean;
  visible: boolean;
  onToggle: () => void;
}) {
  if (!visible) return null;
  const Icon = collapsed ? Maximize2 : Minimize2;
  return (
    <button
      type="button"
      aria-label={collapsed ? "入力欄を広げる" : "入力欄を小さくする"}
      title={collapsed ? "入力欄を広げる" : "入力欄を小さくする"}
      onClick={onToggle}
      className="absolute right-1 top-1 rumi-layer-panel flex h-7 w-7 items-center justify-center rounded-lg text-zinc-400 transition-colors hover:bg-zinc-800 hover:text-zinc-100"
    >
      <Icon size={13} />
    </button>
  );
}

function composerIconForName(iconName: string | undefined, fallback: LucideIcon): LucideIcon {
  const declaredIcon = declarativeIconForName(iconName);
  if (declaredIcon) return declaredIcon;
  const normalized = String(iconName ?? "").trim().toLowerCase();
  if (/search|browser|web|globe/.test(normalized)) return Search;
  if (/file|document|pdf|text/.test(normalized)) return FileText;
  if (/folder|directory/.test(normalized)) return Folder;
  if (/git|branch|repo/.test(normalized)) return GitBranch;
  if (/code|terminal|shell|cli/.test(normalized)) return Code2;
  if (/think|brain|reason/.test(normalized)) return BrainCircuit;
  if (/model|cpu|provider|ai/.test(normalized)) return Cpu;
  if (/key|auth|credential/.test(normalized)) return KeyRound;
  if (/message|chat|conversation/.test(normalized)) return MessageSquare;
  if (/mention|at/.test(normalized)) return AtSign;
  if (/tool|wrench|mcp/.test(normalized)) return Wrench;
  return fallback;
}

const COMMAND_ICON_BY_ID: Partial<Record<string, LucideIcon>> = {
  help: CircleHelp,
  model: Cpu,
  think: Brain,
  fast: Zap,
  price: BadgeDollarSign,
  compact: Minimize2,
  new: MessageSquarePlus,
  clear: Eraser,
  coding: CodeXml,
  frontend: Palette,
  chat: MessagesSquare,
  agent: Bot,
  yolo: ShieldCheck,
  ultra_yolo: ShieldPlus,
  tools: Wrench,
  status: Activity,
  settings: Settings2,
  diff: GitCompare,
  review: ScanSearch,
  branch: GitBranch,
  test: FlaskConical,
  lint: ListChecks,
  files: Files,
  commit: GitCommitHorizontal,
  push: CloudUpload,
  terminal: SquareTerminal,
  patch: FileDiff,
  restore: Clock3,
  history: Clock3,
  export: Download,
  fork: GitFork,
  resume: Play,
  rename: Pencil,
  context: Braces,
  memory: Database,
  permissions: KeyRound,
  approvals: ShieldQuestion,
  usage: ChartNoAxesCombined,
  debug: Bug,
  doctor: Stethoscope,
  logs: ScrollText,
  raw: Braces,
  theme: Paintbrush,
  keymap: Keyboard,
  plugins: Blocks,
  mcp: Plug,
  skills: Sparkles,
  hooks: Webhook,
};

function commandIcon(command: ComposerCommandItem): LucideIcon {
  const protocolIcon = command.protocol_presentation?.icon;
  if (protocolIcon) {
    return COMMAND_ICON_BY_ID[protocolIcon]
      ?? composerIconForName(protocolIcon, Box);
  }
  return COMMAND_ICON_BY_ID[command.id]
    ?? COMMAND_ICON_BY_ID[command.name]
    ?? composerIconForName(`${command.id} ${command.name} ${command.category}`, Box);
}

function ComposerCommandIcon({ command, size = 14 }: { command: ComposerCommandItem; size?: number }) {
  const Icon = commandIcon(command);
  return <Icon aria-hidden="true" size={size} />;
}

const THINKING_LEVEL_STRENGTH: Record<string, number> = {
  none: 0,
  low: 1,
  medium: 2,
  high: 3,
  xhigh: 4,
};

function ThinkingLevelGlyph({ level }: { level: string }) {
  const strength = THINKING_LEVEL_STRENGTH[level] ?? 0;
  return (
    <svg aria-hidden="true" viewBox="0 0 18 18" className="h-4 w-4" fill="none">
      {[0, 1, 2, 3].map((index) => (
        <rect
          key={index}
          x={2.25 + index * 3.6}
          y={12.5 - index * 2.6}
          width="2.2"
          height={3 + index * 2.6}
          rx="1.1"
          fill="currentColor"
          opacity={index < strength ? 1 : 0.18}
        />
      ))}
    </svg>
  );
}

function RuntimeStateIcon({
  label,
  state,
  tone,
  focusable = true,
  children,
}: {
  label: string;
  state: string;
  tone: "neutral" | "sky" | "emerald" | "violet" | "amber" | "rose";
  focusable?: boolean;
  children: ReactNode;
}) {
  const toneClass = {
    neutral: "border-white/[0.06] bg-white/[0.025] text-zinc-600",
    sky: "border-sky-400/20 bg-sky-400/[0.08] text-sky-300",
    emerald: "border-emerald-400/20 bg-emerald-400/[0.08] text-emerald-300",
    violet: "border-violet-400/20 bg-violet-400/[0.08] text-violet-300",
    amber: "border-amber-400/20 bg-amber-400/[0.08] text-amber-300",
    rose: "border-rose-400/20 bg-rose-400/[0.08] text-rose-300",
  }[tone];
  return (
    <span
      role={focusable ? "img" : undefined}
      aria-label={focusable ? label : undefined}
      tabIndex={focusable ? 0 : undefined}
      data-state={state}
      className={`relative flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-lg border transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/70 ${focusable ? "group/runtime" : ""} ${toneClass}`}
    >
      {children}
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 rumi-layer-local-popover mb-2 w-max max-w-[220px] -translate-x-1/2 rounded-lg border border-white/[0.09] bg-[#16171b]/95 px-2.5 py-1.5 text-[11px] font-medium leading-none text-zinc-100 opacity-0 shadow-xl transition-[opacity,transform] duration-150 group-hover/runtime:opacity-100 group-focus/runtime:opacity-100 group-focus-within/runtime:opacity-100"
      >
        {label}
      </span>
    </span>
  );
}

function RuntimeStateButton({
  label,
  state,
  tone,
  onClick,
  children,
}: {
  label: string;
  state: string;
  tone: "neutral" | "sky" | "emerald" | "violet" | "amber" | "rose";
  onClick: () => void;
  children: ReactNode;
}) {
  const toneClass = {
    neutral: "border-white/[0.06] bg-white/[0.025] text-zinc-600",
    sky: "border-sky-400/20 bg-sky-400/[0.08] text-sky-300",
    emerald: "border-emerald-400/20 bg-emerald-400/[0.08] text-emerald-300",
    violet: "border-violet-400/20 bg-violet-400/[0.08] text-violet-300",
    amber: "border-amber-400/20 bg-amber-400/[0.08] text-amber-300",
    rose: "border-rose-400/20 bg-rose-400/[0.08] text-rose-300",
  }[tone];
  return (
    <button
      type="button"
      aria-label={label}
      data-state={state}
      onClick={onClick}
      className={`group/runtime relative flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-lg border transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/70 ${toneClass}`}
    >
      {children}
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 rumi-layer-local-popover mb-2 w-max max-w-[220px] -translate-x-1/2 rounded-lg border border-white/[0.09] bg-[#16171b]/95 px-2.5 py-1.5 text-[11px] font-medium leading-none text-zinc-100 opacity-0 shadow-xl transition-[opacity,transform] duration-150 group-hover/runtime:opacity-100 group-focus/runtime:opacity-100 group-focus-within/runtime:opacity-100"
      >
        {label}
      </span>
    </button>
  );
}

export function persistentComposerToggleCommands(commands: ComposerCommandItem[]): ComposerCommandItem[] {
  return commands.filter((command) => {
    if (command.protocol_presentation?.input.kind !== "toggle") return false;
    return command.protocol_presentation.mounts?.some((mount) => (
      mount.slot_ref === "tobkiri:composer.toolbar.leading"
      && mount.display === "persistent"
    )) === true;
  });
}

export function commandShowsToggleState(command: ComposerCommandItem): boolean {
  if (command.protocol_presentation) {
    return command.protocol_presentation.input.kind === "toggle";
  }
  // Legacy-only fallback. Protocol catalog entries never take this branch.
  return command.execution.type === "settings_patch";
}

export function shouldShowComposerCommandSuggestions({
  focused,
  slashCommandsEnabled,
  hasModelCandidates,
  matchCount,
}: {
  focused: boolean;
  slashCommandsEnabled: boolean;
  hasModelCandidates: boolean;
  matchCount: number;
}): boolean {
  return focused && slashCommandsEnabled && !hasModelCandidates && matchCount > 0;
}

/** A command surface owns the composer listbox while it is visible. */
export function shouldShowComposerAtMentionSuggestions({
  atMentionOpen,
  commandMenuOpen,
  commandSuggestionsOpen,
}: {
  atMentionOpen: boolean;
  commandMenuOpen: boolean;
  commandSuggestionsOpen: boolean;
}): boolean {
  return atMentionOpen && !commandMenuOpen && !commandSuggestionsOpen;
}

export function commandArgumentEntryPrefix(command: ComposerCommandItem | undefined): string | null {
  if (!command || command.protocol_presentation?.input.kind !== "form") return null;
  if (!(command.args ?? []).some((argument) => argument.type === "string")) return null;
  return `/${command.name} `;
}

export type CommandArgumentGuide = {
  command: string;
  arguments: string[];
  accessibleText: string;
};

export function commandArgumentGuideForInput(
  input: string,
  commands: ComposerCommandItem[],
): CommandArgumentGuide | null {
  for (const command of commands) {
    const prefix = commandArgumentEntryPrefix(command);
    if (!prefix || !input.startsWith(prefix)) continue;
    const protocolFields = command.protocol_presentation?.input.kind === "form"
      && Array.isArray(command.protocol_presentation.input.fields)
      ? command.protocol_presentation.input.fields as Array<Record<string, unknown>>
      : [];
    const labels = (command.args ?? [])
      .filter((argument) => argument.type === "string")
      .map((argument) => {
        const protocolField = protocolFields.find((field) => field.argument === argument.name);
        const protocolPlaceholder = protocolField?.placeholder;
        const fallback = protocolPlaceholder && typeof protocolPlaceholder === "object"
          ? String((protocolPlaceholder as { fallback?: unknown }).fallback ?? "").trim()
          : "";
        return String(argument.placeholder || argument.label || fallback || argument.name).trim();
      })
      .filter(Boolean);
    if (labels.length === 0) return null;
    const commandToken = `/${command.name}`;
    return {
      command: commandToken,
      arguments: labels,
      accessibleText: `${commandToken} ${labels.map((label) => `<${label}>`).join(" ")}`,
    };
  }
  return null;
}

function commandStateLabel(command: ComposerCommandItem): "オン" | "オフ" | null {
  if (!commandShowsToggleState(command)) return null;
  return command.active === true || command.enabled === true ? "オン" : "オフ";
}

function formatVoiceDuration(seconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(seconds));
  return `${Math.floor(safeSeconds / 60)}:${String(safeSeconds % 60).padStart(2, "0")}`;
}

const MODEL_STATUS_TONE_STYLES: Record<NonNullable<ComposerModelStatusIndicator["tone"]>, { icon: string; popover: string; button: string }> = {
  neutral: {
    icon: "text-zinc-300",
    popover: "border-zinc-700/80",
    button: "bg-zinc-100 text-zinc-950 hover:bg-white",
  },
  info: {
    icon: "text-sky-300",
    popover: "border-sky-500/30",
    button: "bg-sky-100 text-sky-950 hover:bg-white",
  },
  warning: {
    icon: "text-amber-300",
    popover: "border-amber-500/30",
    button: "bg-amber-100 text-amber-950 hover:bg-white",
  },
  danger: {
    icon: "text-orange-300",
    popover: "border-orange-500/35",
    button: "bg-orange-100 text-orange-950 hover:bg-white",
  },
};

function inlineSvgMarkup(markup: string): string {
  const sanitized = markup.replace(/\s(width|height)="[^"]*"/gi, "");
  return sanitized.replace(
    /<svg\b([^>]*)>/i,
    '<svg$1 width="100%" height="100%" aria-hidden="true" focusable="false" style="display:block;width:100%;height:100%;">',
  );
}

function clampPopoverOffset(value: number, min: number, max: number): number {
  if (max < min) return min;
  return Math.min(Math.max(value, min), max);
}

function ModelStatusIndicatorButton({
  indicator,
  open,
  onToggle,
  onClose,
}: {
  indicator: ComposerModelStatusIndicator;
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
}) {
  const tone = MODEL_STATUS_TONE_STYLES[indicator.tone ?? "warning"];
  const actionTone = MODEL_STATUS_TONE_STYLES[indicator.action?.tone ?? indicator.tone ?? "warning"];
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const [popoverStyle, setPopoverStyle] = useState<CSSProperties | null>(null);

  const updatePopoverStyle = useCallback(() => {
    if (typeof window === "undefined" || !triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    const minLeft = MODEL_STATUS_POPOVER_VIEWPORT_MARGIN;
    const maxLeft = window.innerWidth - MODEL_STATUS_POPOVER_WIDTH - MODEL_STATUS_POPOVER_VIEWPORT_MARGIN;
    const nextLeft = clampPopoverOffset(rect.right - MODEL_STATUS_POPOVER_WIDTH, minLeft, maxLeft);
    const spaceBelow = window.innerHeight - rect.bottom - MODEL_STATUS_POPOVER_VIEWPORT_MARGIN;
    const placeBelow = spaceBelow >= MODEL_STATUS_POPOVER_HEIGHT || rect.top < MODEL_STATUS_POPOVER_HEIGHT + MODEL_STATUS_POPOVER_GAP;
    const nextTop = placeBelow
      ? clampPopoverOffset(
        rect.bottom + MODEL_STATUS_POPOVER_GAP,
        MODEL_STATUS_POPOVER_VIEWPORT_MARGIN,
        window.innerHeight - MODEL_STATUS_POPOVER_HEIGHT - MODEL_STATUS_POPOVER_VIEWPORT_MARGIN,
      )
      : clampPopoverOffset(
        rect.top - MODEL_STATUS_POPOVER_HEIGHT - MODEL_STATUS_POPOVER_GAP,
        MODEL_STATUS_POPOVER_VIEWPORT_MARGIN,
        window.innerHeight - MODEL_STATUS_POPOVER_HEIGHT - MODEL_STATUS_POPOVER_VIEWPORT_MARGIN,
      );
    setPopoverStyle({ left: nextLeft, top: nextTop });
  }, []);

  useIsomorphicLayoutEffect(() => {
    if (!open) {
      setPopoverStyle(null);
      return;
    }
    updatePopoverStyle();
    if (typeof window === "undefined") return;
    window.addEventListener("resize", updatePopoverStyle);
    window.addEventListener("scroll", updatePopoverStyle, true);
    return () => {
      window.removeEventListener("resize", updatePopoverStyle);
      window.removeEventListener("scroll", updatePopoverStyle, true);
    };
  }, [open, updatePopoverStyle]);

  const openPopover = (
    <>
      <button
        type="button"
        aria-label="close status indicator"
        className="fixed inset-0 rumi-layer-global-overlay cursor-default bg-transparent"
        onClick={onClose}
      />
      <div
        className={`fixed rumi-layer-command-palette w-[240px] rounded-xl border bg-zinc-950 p-3 shadow-2xl ${tone.popover}`}
        style={popoverStyle ?? {
          right: MODEL_STATUS_POPOVER_VIEWPORT_MARGIN,
          top: MODEL_STATUS_POPOVER_VIEWPORT_MARGIN,
        }}
      >
        <div className="flex items-start gap-2">
          <span
            aria-hidden="true"
            className="mt-0.5 block h-5 w-5 flex-shrink-0"
            dangerouslySetInnerHTML={{ __html: inlineSvgMarkup(indicator.svgMarkup) }}
          />
          <div className="min-w-0">
            <p className="text-sm font-medium text-zinc-100">{indicator.name}</p>
            <p className="mt-1 text-[11px] leading-relaxed text-zinc-400">{indicator.description}</p>
          </div>
        </div>
        {indicator.action && (
          <button
            type="button"
            onClick={() => {
              indicator.action?.onSelect();
              onClose();
            }}
            className={`mt-3 flex h-8 w-full items-center justify-center rounded-lg px-3 text-xs font-semibold transition-colors ${actionTone.button}`}
          >
            {indicator.action.label}
          </button>
        )}
      </div>
    </>
  );

  return (
    <div className="group/status relative flex items-center">
      <button
        ref={triggerRef}
        type="button"
        aria-label={indicator.name}
        title={indicator.description}
        aria-expanded={open}
        onClick={onToggle}
        className={`relative flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-md transition-colors hover:bg-white/[0.06] ${tone.icon}`}
      >
        <span
          aria-hidden="true"
          className="block h-[14px] w-[14px]"
          dangerouslySetInnerHTML={{ __html: inlineSvgMarkup(indicator.svgMarkup) }}
        />
      </button>
      {!open && (
        <div className="pointer-events-none absolute bottom-full right-0 rumi-layer-local-popover mb-2 w-max max-w-[220px] rounded-lg border border-white/[0.08] bg-[#16171b]/95 px-2 py-1 text-[10px] leading-snug text-zinc-300 opacity-0 shadow-xl transition-opacity group-hover/status:opacity-100">
          <span className="block font-medium text-zinc-100">{indicator.name}</span>
          <span className="block text-zinc-400">{indicator.description}</span>
        </div>
      )}
      {open && (
        typeof document !== "undefined"
          ? createPortal(openPopover, document.body)
          : openPopover
      )}
    </div>
  );
}

function composerChromeWidgetsForSlot(
  widgets: ComposerChromeWidgetSpec[],
  slot: ComposerChromeSlot,
): ComposerChromeWidgetSpec[] {
  return widgets
    .filter((widget) => widget.slot === slot && widget.visible !== false)
    .sort((left, right) => left.order - right.order);
}

function composerChromeWidgetsForHomeSlot(
  widgets: ComposerChromeWidgetSpec[],
  slot: ComposerHomeSlot,
): ComposerChromeWidgetSpec[] {
  return widgets
    .filter((widget) => widget.visible !== false && widget.homeSlot === slot)
    .sort((left, right) => left.order - right.order);
}

function ComposerChromeWidget({
  widget,
  onNodeChange,
}: {
  widget: ComposerChromeWidgetSpec;
  onNodeChange?: (widgetId: string, node: HTMLDivElement | null) => void;
}) {
  const mobileClass = widget.mobile === "hide" ? "max-[640px]:hidden" : "";
  return (
    <div
      ref={(node) => onNodeChange?.(widget.id, node)}
      data-composer-widget={widget.id}
      data-composer-slot={widget.slot}
      className={`rumi-composer-widget flex min-w-0 items-center ${mobileClass} ${widget.className ?? ""}`}
      style={composerChromeWidgetStyle(widget.width)}
    >
      {widget.render()}
    </div>
  );
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

function profileProviderId(profile: ModelProfile | null | undefined): string {
  return String(profile?.provider_id ?? "").trim();
}

function profileProviderLabel(profile: ModelProfile | null | undefined): string {
  return String(
    profile?.provider_display_name
    ?? profile?.metadata?.provider_display_name
    ?? profile?.provider_id
    ?? "provider",
  );
}

function profileDisplayName(profile: ModelProfile | null | undefined): string {
  return String(
    profile?.disambiguated_name
    ?? profile?.metadata?.disambiguated_name
    ?? profile?.display_name
    ?? profile?.profile_id
    ?? "model",
  );
}

function profileIsConfigured(profile: ModelProfile | null | undefined): boolean {
  const availability = profile?.availability ?? {};
  return Boolean(
    availability.configured
    || availability.active
    || availability.status === "configured"
    || availability.status === "active",
  );
}

export function profileNeedsApiKey(profile: ModelProfile | null | undefined): boolean {
  const providerId = profileProviderId(profile);
  if (!providerId || providerId === "rumi" || LOCAL_MODEL_PROVIDER_IDS.has(providerId)) return false;
  const availability = profile?.availability ?? {};
  if (profile?.local || availability.local || availability.offline || profileIsConfigured(profile)) return false;
  return API_KEY_PROVIDER_IDS.has(providerId);
}

type ProtocolStaticSelectMatch = {
  command: ComposerCommandItem;
  query: string;
  options: Array<{ value: string; label: string }>;
};

export function protocolStaticSelectMatch(
  input: string,
  commands: ComposerCommandItem[],
): ProtocolStaticSelectMatch | null {
  const body = input.trimStart().replace(/^\//, "");
  for (const command of commands) {
    const presentation = command.protocol_presentation?.input;
    if (presentation?.kind !== "select" || !Array.isArray(presentation.options)) continue;
    const names = [command.name, command.id, ...(command.aliases ?? [])]
      .map((value) => String(value ?? "").trim())
      .filter(Boolean)
      .sort((left, right) => right.length - left.length);
    const matchedName = names.find((name) => (
      body.toLocaleLowerCase() === name.toLocaleLowerCase()
      || body.toLocaleLowerCase().startsWith(`${name.toLocaleLowerCase()} `)
    ));
    if (!matchedName) continue;
    const options = presentation.options.flatMap((option) => {
      if (!option || typeof option !== "object" || Array.isArray(option)) return [];
      const record = option as Record<string, unknown>;
      const value = String(record.value ?? "").trim();
      const labelRecord = record.label && typeof record.label === "object" && !Array.isArray(record.label)
        ? record.label as Record<string, unknown>
        : {};
      if (!value) return [];
      return [{ value, label: String(labelRecord.fallback ?? value) }];
    });
    return {
      command,
      query: body.slice(matchedName.length).trim().toLocaleLowerCase(),
      options,
    };
  }
  return null;
}

function compactProfileName(name: string): string {
  return name
    .replace(/^GPT[\s-]+/i, "")
    .replace(/^Claude\s+/i, "")
    .replace(/\s*\(.*?\)\s*/g, " ")
    .trim();
}

export function composerModelControlWidth(modelName: string): ComposerChromeWidth {
  const compactName = compactProfileName(modelName) || "model";
  const nameLength = Array.from(compactName).length;
  const basisCh = Math.min(
    COMPOSER_MODEL_CONTROL_MAX_CH,
    Math.max(COMPOSER_MODEL_CONTROL_MIN_CH, nameLength + COMPOSER_MODEL_CONTROL_CHROME_CH),
  );
  return {
    basis: `${basisCh}ch`,
    min: "5.5rem",
    max: "12rem",
    shrink: 1,
  };
}

function steerStatusLabel(status: string | undefined): string {
  switch (String(status || "").toLowerCase()) {
    case "queued":
      return "待機中";
    case "injected":
      return "反映済み";
    case "sending":
      return "送信中";
    case "sent":
      return "送信済み";
    default:
      return "入力";
  }
}

function capabilityBadges(profile: ModelProfile | null | undefined): string[] {
  if (!profile) return [];
  const badges: string[] = [];
  if (profile.supports_vision || profile.supports_image_input) badges.push("Vision");
  if (profile.supports_tool_calling) badges.push("Tools");
  if (profile.supports_thinking) badges.push("Thinking");
  if (profile.supports_fast || profile.speed_tier === "fast") badges.push("Fast");
  if ((profile.max_context_tokens ?? profile.max_context ?? 0) >= 100000) badges.push("Long Context");
  return badges;
}

function modelRouteReason(profile: ModelProfile | null | undefined): string {
  if (!profile) return "";
  const knowledge = typeof profile.knowledge_level === "number" ? `KL ${profile.knowledge_level}` : "";
  return [...capabilityBadges(profile), knowledge].filter(Boolean).join(" / ");
}

function normalizeProviderSearchToken(value: string): string {
  return value
    .trim()
    .replace(/^@+/, "")
    .toLowerCase()
    .replace(/[\s_-]+/g, "");
}

function modelProfileProviderAliases(profile: ModelProfile): string[] {
  return [
    profile.provider_id,
    profile.provider_display_name,
    profile.metadata?.provider_id,
    profile.metadata?.provider_display_name,
  ].map((value) => normalizeProviderSearchToken(String(value ?? ""))).filter(Boolean);
}

function modelProfileLegacySearchAliases(profile: ModelProfile): string[] {
  const providerId = profileProviderId(profile).toLowerCase();
  const modelId = String(profile.model_id ?? "").trim().toLowerCase();
  if (providerId !== "openrouter" || !/^tencent\/hy3(?:-preview)?(?::free)?$/.test(modelId)) return [];
  return [
    "hy3 free",
    "tencent hy3 free",
    modelId.includes("preview") ? "hy3 preview free" : "hy3 free current",
  ];
}

function modelProfileSearchText(profile: ModelProfile): string {
  return [
    profile.profile_id,
    profile.qualified_model_id,
    profile.model_id,
    profile.provider_id,
    profile.provider_display_name,
    profileDisplayName(profile),
    profile.display_name,
    profile.disambiguated_name,
    ...modelProfileLegacySearchAliases(profile),
    ...(profile.capability_tags ?? []),
    ...(profile.recommended_roles ?? []),
  ].filter(Boolean).join(" ").toLowerCase();
}


export function filterModelProfilesBySearch(profiles: ModelProfile[], search: string, providerTrigger = "@"): ModelProfile[] {
  const rawTokens = search.trim().split(/\s+/).filter(Boolean);
  if (rawTokens.length === 0) return profiles;
  const trigger = providerTrigger || "@";

  const providerTokens = rawTokens
    .filter((token) => token.startsWith(trigger))
    .map((token) => token.slice(trigger.length))
    .map(normalizeProviderSearchToken)
    .filter(Boolean);
  const textTokens = rawTokens
    .filter((token) => !token.startsWith(trigger))
    .map((token) => token.toLowerCase())
    .filter(Boolean);

  return profiles.filter((profile) => {
    const providerAliases = modelProfileProviderAliases(profile);
    const matchesProviders = providerTokens.every((token) => (
      providerAliases.some((alias) => alias.includes(token))
    ));
    if (!matchesProviders) return false;

    const searchText = modelProfileSearchText(profile);
    return textTokens.every((token) => searchText.includes(token));
  });
}

export type ModelProviderOption = {
  id: string;
  label: string;
  modelCount: number;
};

export type ModelProviderSearchState = {
  active: boolean;
  confirmedProviderId: string;
  highlightPrefix: string;
  providerQuery: string;
};

export function modelProviderSearchState(search: string, providerTrigger = "@"): ModelProviderSearchState {
  const trigger = providerTrigger || "@";
  const escapedTrigger = trigger.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = search.match(new RegExp(`^${escapedTrigger}([^\\s]*)(\\s+)?`));
  if (!match) {
    return { active: false, confirmedProviderId: "", highlightPrefix: "", providerQuery: "" };
  }
  const providerQuery = String(match[1] ?? "").trim().toLowerCase();
  const confirmed = Boolean(match[2]);
  return {
    active: !confirmed,
    confirmedProviderId: confirmed ? providerQuery : "",
    highlightPrefix: `${trigger}${providerQuery}`,
    providerQuery,
  };
}

export function modelProviderOptions(profiles: ModelProfile[]): ModelProviderOption[] {
  const byId = new Map<string, ModelProviderOption>();
  for (const profile of profiles) {
    const id = profileProviderId(profile);
    if (!id) continue;
    const current = byId.get(id);
    if (current) {
      current.modelCount += 1;
      continue;
    }
    byId.set(id, {
      id,
      label: profileProviderLabel(profile),
      modelCount: 1,
    });
  }
  return [...byId.values()].sort((left, right) => left.label.localeCompare(right.label, "ja"));
}

export type ModelSearchKeyAction =
  | { handled: false }
  | { handled: true; type: "close" }
  | { handled: true; type: "move_provider"; index: number }
  | { handled: true; type: "confirm_provider"; index: number }
  | { handled: true; type: "move_model"; index: number }
  | { handled: true; type: "confirm_model"; index: number };

export function modelSearchKeyAction({
  key,
  shiftKey,
  providerMode,
  providerCount,
  providerIndex,
  modelCount,
  modelIndex,
  providerConfirmKey = "Tab",
  modelConfirmKeys = ["Enter", "Tab"],
}: {
  key: string;
  shiftKey: boolean;
  providerMode: boolean;
  providerCount: number;
  providerIndex: number;
  modelCount: number;
  modelIndex: number;
  providerConfirmKey?: "Tab" | "Enter";
  modelConfirmKeys?: string[];
}): ModelSearchKeyAction {
  if (key === "Escape") return { handled: true, type: "close" };
  const direction = key === "ArrowUp" ? -1 : key === "ArrowDown" ? 1 : 0;
  if (providerMode) {
    if (direction && providerCount > 0) {
      return { handled: true, type: "move_provider", index: (providerIndex + direction + providerCount) % providerCount };
    }
    if (key === providerConfirmKey && !(key === "Tab" && shiftKey) && providerCount > 0) {
      return { handled: true, type: "confirm_provider", index: Math.min(Math.max(providerIndex, 0), providerCount - 1) };
    }
    return { handled: false };
  }
  if (direction && modelCount > 0) {
    return { handled: true, type: "move_model", index: (modelIndex + direction + modelCount) % modelCount };
  }
  if (modelConfirmKeys.includes(key) && !(key === "Tab" && shiftKey) && modelCount > 0) {
    return { handled: true, type: "confirm_model", index: Math.min(Math.max(modelIndex, 0), modelCount - 1) };
  }
  return { handled: false };
}

function groupToolItems(items: ComposerExtensionItem[]): ToolGroup[] {
  const groups = new Map<string, ToolGroup>();
  for (const item of items) {
    const meta = toolGroupFor(item);
    const current = groups.get(meta.id) ?? { ...meta, items: [] };
    current.items.push(item);
    groups.set(meta.id, current);
  }
  return sortedToolGroups([...groups.values()].filter((group) => group.items.length > 0));
}

function PendingFileChip({
  path,
  onRemove,
}: {
  path: string;
  onRemove?: (path: string) => void;
}) {
  const name = path.split("/").filter(Boolean).pop() || path;
  return (
    <span
      role="status"
      aria-label={`${name} を読み込み中`}
      className="inline-flex max-w-[220px] items-center gap-1.5 rounded-lg border border-sky-500/25 bg-sky-500/[0.08] py-1 pl-2 pr-1 text-[11px] text-sky-200"
    >
      <Loader2 size={12} className="flex-shrink-0 animate-spin" />
      <span className="truncate">{name} を読み込み中</span>
      {onRemove && (
        <button
          type="button"
          aria-label={`${name} の読み込みを取り消す`}
          onClick={() => onRemove(path)}
          className="flex h-[44px] min-h-[44px] w-[44px] min-w-[44px] flex-shrink-0 items-center justify-center rounded-full text-sky-200/60 transition-colors hover:bg-sky-400/10 hover:text-sky-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-300"
        >
          <X size={14} />
        </button>
      )}
    </span>
  );
}

function FilePreviewCard({
  file,
  onRemove,
  onTranscribe,
}: {
  file: AttachedFile;
  onRemove?: (id: string) => void;
  onTranscribe?: (file: AttachedFile) => Promise<void>;
}) {
  const [transcriptionState, setTranscriptionState] = useState<"idle" | "running" | "error">("idle");
  const [transcriptionError, setTranscriptionError] = useState("");
  const ext = file.name.split(".").pop()?.toUpperCase() || "FILE";
  const lineCount = file.content ? file.content.split(/\r\n|\r|\n/).length : null;
  const isImage = /^image\//.test(file.type ?? "");
  const isAudio = isAudioAttachment(file);
  const fileMeta = lineCount ? `${lineCount}行` : `${Math.max(1, Math.ceil(file.size / 1024))} KB`;
  const canTranscribe = isAudio && Boolean(file.dataUrl && onTranscribe);
  return (
    <div
      className="group/file relative h-24 w-24 aspect-square flex-shrink-0 overflow-hidden rounded-xl border border-white/[0.1] bg-[#1b1c20] shadow-sm outline-none focus-visible:ring-2 focus-visible:ring-sky-300/70"
      tabIndex={isAudio ? 0 : undefined}
      aria-label={isAudio ? `${file.name}。音声ファイル` : undefined}
    >
      {isImage ? (
        <>
          {file.dataUrl ? (
            <img src={file.dataUrl} alt={file.name} className="h-full w-full object-cover" />
          ) : (
            <div className="flex h-full w-full items-center justify-center bg-zinc-900 text-zinc-500">
              <File size={20} />
            </div>
          )}
          <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/90 to-transparent px-2 pb-1.5 pt-5">
            <p className="truncate text-[10px] font-medium text-white" title={file.name}>{file.name}</p>
          </div>
        </>
      ) : (
        <div className="flex h-full flex-col justify-between p-2.5">
          <span className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg border border-white/[0.08] bg-white/[0.04] text-[9px] font-semibold text-zinc-300">
            {ext.slice(0, 4)}
          </span>
          <span className="min-w-0">
            <span className="block truncate text-[11px] font-medium text-zinc-100" title={file.name}>{file.name}</span>
            <span className="mt-0.5 block text-[9px] text-zinc-500">{fileMeta}</span>
          </span>
        </div>
      )}
      {isAudio && (
        <div className="absolute inset-0 flex items-end bg-gradient-to-t from-black/95 via-black/60 to-transparent p-1.5 opacity-0 transition-opacity group-hover/file:opacity-100 group-focus-within/file:opacity-100">
          <button
            type="button"
            disabled={!canTranscribe || transcriptionState === "running"}
            aria-label={`${file.name} の文字起こしを作成`}
            title={canTranscribe ? "文字起こしを作成" : "この音声データは文字起こし用に読み込めません"}
            onClick={() => {
              if (!canTranscribe || !onTranscribe) return;
              setTranscriptionState("running");
              setTranscriptionError("");
              void onTranscribe(file).catch((error) => {
                setTranscriptionError(readableTranscriptionError(error));
                setTranscriptionState("error");
              });
            }}
            className="flex min-h-8 w-full items-center justify-center gap-1 rounded-lg border border-white/15 bg-zinc-950/90 px-1.5 text-[9px] font-semibold leading-tight text-zinc-100 shadow-lg hover:bg-zinc-900 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {transcriptionState === "running" ? <Loader2 size={11} className="animate-spin" /> : <FileText size={11} />}
            {transcriptionState === "running" ? "作成中..." : "文字起こしを作成"}
          </button>
        </div>
      )}
      {transcriptionState === "error" && (
        <ErrorNotice
          className="absolute inset-x-1 bottom-1 bg-rose-950/95 px-1.5 py-1 text-[8px] leading-tight"
          copyLabel={`${file.name} の文字起こしエラーをコピー`}
          message={transcriptionError}
        />
      )}
      {onRemove && (
        <button
          type="button"
          aria-label={`${file.name} を削除`}
          onClick={() => onRemove(file.id)}
          className="absolute right-0 top-0 flex h-[44px] min-h-[44px] w-[44px] min-w-[44px] items-center justify-center text-zinc-300 opacity-100 transition-opacity hover:text-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-3px] focus-visible:outline-sky-300"
          title="削除"
        >
          <span className="flex h-6 w-6 items-center justify-center rounded-full border border-white/[0.08] bg-black/65 shadow-sm">
            <X size={11} />
          </span>
        </button>
      )}
    </div>
  );
}

function ComposerAttachmentRegion({
  attachedFiles,
  pendingPaths,
  error,
  onFileRemove,
  onPendingRemove,
  onTranscribe,
}: {
  attachedFiles: AttachedFile[];
  pendingPaths: string[];
  error?: string | null;
  onFileRemove?: (id: string) => void;
  onPendingRemove?: (path: string) => void;
  onTranscribe?: (file: AttachedFile) => Promise<void>;
}) {
  const hasAttachments = attachedFiles.length > 0 || pendingPaths.length > 0;
  const isVisible = hasAttachments || Boolean(error);
  return (
    <div
      className="rumi-composer-attachment-reveal"
      data-composer-attachment-region
      data-attachment-state={isVisible ? "expanded" : "collapsed"}
      aria-hidden={!isVisible}
    >
      <div className="rumi-composer-attachment-reveal-inner">
        {hasAttachments && (
          <div
            className="rumi-composer-attachment-strip flex gap-2 overflow-x-auto"
            role="region"
            aria-label="添付ファイル"
          >
            {pendingPaths.map((path) => (
              <PendingFileChip key={path} path={path} onRemove={onPendingRemove} />
            ))}
            {attachedFiles.map((file) => (
              <FilePreviewCard
                key={file.id}
                file={file}
                onRemove={onFileRemove}
                onTranscribe={onTranscribe}
              />
            ))}
          </div>
        )}
        {error && (
          <ErrorNotice
            className="mt-1.5 rounded-lg px-2 py-1.5 text-[10px] leading-4"
            copyLabel="画像添付エラーをコピー"
            message={error}
          />
        )}
      </div>
    </div>
  );
}

export function composerClipboardFiles(
  clipboardData: Pick<DataTransfer, "files" | "items">,
): File[] {
  const direct = Array.from(clipboardData.files);
  if (direct.length > 0) return direct;
  return Array.from(clipboardData.items)
    .filter((item) => item.kind === "file")
    .map((item) => item.getAsFile())
    .filter((file): file is File => Boolean(file));
}

const INLINE_IMAGE_MIME_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
]);
const MAX_INLINE_IMAGE_BYTES = 1024 * 1024;
const MAX_INLINE_IMAGE_COUNT = 2;

function isInlineImageAttachment(file: Pick<AttachedFile, "type">): boolean {
  return INLINE_IMAGE_MIME_TYPES.has(String(file.type ?? "").trim().toLowerCase());
}

/**
 * Keep the picker aligned with the saved-turn image contract before a user
 * invests in a draft that the backend cannot safely persist or send.
 */
export function prepareComposerAttachments(
  files: File[],
  attachedFiles: AttachedFile[],
): { files: File[]; error: string | null } {
  let remainingImageSlots = Math.max(
    0,
    MAX_INLINE_IMAGE_COUNT - attachedFiles.filter(isInlineImageAttachment).length,
  );
  const accepted: File[] = [];
  const rejected: string[] = [];

  for (const file of files) {
    const mimeType = String(file.type ?? "").trim().toLowerCase();
    if (!mimeType.startsWith("image/")) {
      accepted.push(file);
      continue;
    }
    if (!INLINE_IMAGE_MIME_TYPES.has(mimeType)) {
      rejected.push(`${file.name}: PNG、JPEG、WebP、GIFのみ対応`);
      continue;
    }
    if (file.size > MAX_INLINE_IMAGE_BYTES) {
      rejected.push(`${file.name}: 1 MB以下にしてください`);
      continue;
    }
    if (remainingImageSlots <= 0) {
      rejected.push(`${file.name}: 画像は1メッセージに最大2枚です`);
      continue;
    }
    accepted.push(file);
    remainingImageSlots -= 1;
  }

  return {
    files: accepted,
    error: rejected.length > 0 ? `画像を追加できませんでした。${rejected.join(" / ")}` : null,
  };
}

function DroppedWidgetChip({
  widget,
  onAction,
  onToggle,
}: {
  widget: DroppedWidget;
  onAction?: (widget: DroppedWidget) => void;
  onToggle?: (id: string) => void;
}) {
  if (widget.type === "conversation") {
    const ConversationIcon = composerIconForName(widget.icon, MessageSquare);
    return (
      <button
        type="button"
        title={widget.description ?? widget.label}
        onClick={() => onToggle?.(widget.id)}
        className={`inline-flex max-w-[220px] items-center gap-1.5 rounded-lg border px-2 py-1 text-[11px] transition-colors ${
          widget.enabled === false
            ? "border-white/[0.07] bg-white/[0.04] text-zinc-500"
            : "border-amber-500/30 bg-amber-500/10 text-amber-200 hover:bg-amber-500/15"
        }`}
      >
        <WidgetAttentionIcon
          attention={widget.presentation?.icon_attention}
          widgetId={widget.id}
        >
          <ConversationIcon size={11} className="flex-shrink-0" />
        </WidgetAttentionIcon>
        <span className="truncate">{widget.label}</span>
      </button>
    );
  }

  if (widget.type === "tool" || widget.type === "service"
    || widget.widgetKind === "tool_toggle"
    || widget.widgetKind === "service_reference") return null;

  const fallbackIcon = widget.widgetKind === "button"
    ? MousePointerClick
    : widget.widgetKind === "selector"
      ? SlidersHorizontal
      : PanelRightOpen;
  const Icon = composerIconForName(widget.icon, fallbackIcon);
  return (
    <button
      type="button"
      title={widget.description ?? widget.label}
      onClick={() => onAction?.(widget)}
      className="inline-flex max-w-[160px] items-center gap-1.5 rounded-lg border border-white/[0.08] bg-white/[0.05] px-2 py-1 text-[11px] text-zinc-300 transition-colors hover:bg-white/[0.08] hover:text-zinc-100"
    >
      <WidgetAttentionIcon
        attention={widget.presentation?.icon_attention}
        widgetId={widget.id}
      >
        <Icon size={10} />
      </WidgetAttentionIcon>
      <span className="truncate">{widget.label}</span>
    </button>
  );
}

function ToolItemList({
  items,
  onSelect,
}: {
  items: ComposerExtensionItem[];
  onSelect: (item: ComposerExtensionItem) => void;
}) {
  if (items.length === 0) {
    return <div className="px-3 py-2 text-xs text-zinc-500">tool がありません</div>;
  }
  return (
    <div className="grid gap-0.5">
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          disabled={item.disabled}
          onClick={() => onSelect(item)}
          className="rounded-lg px-3 py-1.5 text-left transition-colors hover:bg-white/[0.06] disabled:opacity-50 group"
        >
          <span className="block truncate text-[13px] text-zinc-200 group-hover:text-zinc-50">{item.label}</span>
          {item.description && <span className="block truncate text-[10px] text-zinc-500">{item.description}</span>}
        </button>
      ))}
    </div>
  );
}

function ProviderApiKeyPrompt({
  profile,
  onCancel,
  onSave,
}: {
  profile: ModelProfile;
  onCancel: () => void;
  onSave: (providerId: string, value: string) => Promise<void> | void;
}) {
  const [draft, setDraft] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const providerId = profileProviderId(profile);
  const providerLabel = profileProviderLabel(profile);

  const save = async () => {
    const value = draft.trim();
    if (!value || isSaving) return;
    setIsSaving(true);
    setError(null);
    try {
      await onSave(providerId, value);
      setDraft("");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "API key の保存に失敗しました。");
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <>
      <button type="button" aria-label="close api key prompt" className="fixed inset-0 rumi-layer-global-overlay cursor-default" onClick={onCancel} />
      <div className="absolute bottom-full right-3 rumi-layer-global-overlay mb-2 w-[min(430px,calc(100vw-32px))] overflow-hidden rumi-popover">
        <div className="border-b border-white/[0.06] px-4 py-3">
          <div className="flex items-center gap-2 text-sm font-medium text-zinc-100">
            <KeyRound size={15} className="text-zinc-400" />
            {providerLabel} API key
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-zinc-500">
            {profile.display_name} を使うには API key が必要です。ここで保存すると、そのままモデルを選べます。
          </p>
        </div>
        <div className="space-y-2 p-3">
          <input
            type="password"
            autoComplete="off"
            value={draft}
            onChange={(event) => {
              setDraft(event.target.value);
              setError(null);
            }}
            onKeyDown={(event) => {
              if (event.key !== "Enter") return;
              event.preventDefault();
              void save();
            }}
            placeholder={providerId === "google" ? "Gemini API key" : `${providerLabel} API key`}
            className="w-full rounded-lg border border-white/[0.08] bg-black/25 px-3 py-2 text-sm text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-indigo-400/50"
            autoFocus
          />
          {error && (
            <ErrorNotice
              className="px-2 py-1 text-[11px]"
              copyLabel="API key 保存エラーをコピー"
              message={error}
            />
          )}
          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={onCancel}
              className="h-8 rounded-lg px-3 text-xs text-zinc-400 transition-colors hover:bg-white/[0.05] hover:text-zinc-100"
            >
              キャンセル
            </button>
            <button
              type="button"
              disabled={!draft.trim() || isSaving}
              onClick={() => void save()}
              className={`h-8 rounded-lg px-3 text-xs font-semibold transition-colors ${
                draft.trim() && !isSaving
                  ? "bg-zinc-100 text-zinc-950 hover:bg-white"
                  : "bg-zinc-900 text-zinc-600 cursor-not-allowed"
              }`}
            >
              {isSaving ? "保存中..." : "保存して使う"}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

export function modelDropdownPlacementClassName(placement: "above" | "below"): string {
  return placement === "below" ? "top-full -right-44 mt-2 max-[900px]:right-0" : "bottom-full right-0 mb-2";
}

export function nextModelPickerOpenState(
  currentOpen: boolean,
  action: string,
  rawHasArgs: boolean,
): boolean | null {
  if (action !== "open_model_picker" || rawHasArgs) return null;
  return !currentOpen;
}

export function isModelPickerToggleCommand(currentOpen: boolean, rawInput: string): boolean {
  return currentOpen && rawInput.trim().toLowerCase() === "/model";
}

export type ComposerModelSearchPayload = {
  query: string;
  provider_id?: string;
  max_results: number;
  offset?: number;
};

export function composerModelSearchPayload(
  search: string,
  providerState: ModelProviderSearchState,
  maxResults: number,
  offset = 0,
): ComposerModelSearchPayload {
  const providerId = providerState.confirmedProviderId;
  const query = providerId
    ? search.slice(providerState.highlightPrefix.length).trim()
    : search.trim();
  return {
    query,
    ...(providerId ? { provider_id: providerId } : {}),
    max_results: maxResults,
    offset,
  };
}

export function modelPickerPage(
  profiles: ModelProfile[],
  remoteProfiles: ModelProfile[],
  selectedProfile: ModelProfile | null,
  selectedFirst: boolean,
  limit: number,
): { visible: ModelProfile[]; total: number } {
  const byId = new Map<string, ModelProfile>();
  for (const profile of [...profiles, ...remoteProfiles]) {
    const key = profile.profile_id
      || profile.qualified_model_id
      || `${profile.provider_id ?? ""}/${profile.model_id ?? ""}`;
    if (!key || byId.has(key)) continue;
    byId.set(key, profile);
  }
  const values = [...byId.values()];
  if (selectedFirst && selectedProfile) {
    const selectedId = selectedProfile.profile_id || selectedProfile.qualified_model_id;
    const selectedIndex = values.findIndex((profile) => (
      profile.profile_id || profile.qualified_model_id
    ) === selectedId);
    if (selectedIndex > 0) values.unshift(...values.splice(selectedIndex, 1));
  }
  return { visible: values.slice(0, limit), total: values.length };
}


function ModeSelector({
  mode,
  onModeChange,
  onClose,
}: {
  mode: AppMode;
  onModeChange: (mode: AppMode) => void;
  onClose: () => void;
}) {
  return (
    <>
      <button type="button" aria-label="close mode selector" className="fixed inset-0 rumi-layer-local-popover cursor-default" onClick={onClose} />
      <div className="absolute bottom-full left-0 mb-2 rumi-layer-modal w-[220px] overflow-hidden rumi-popover">
        <div className="border-b border-white/[0.06] px-3 py-2">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">モード選択</p>
        </div>
        <div className="py-1">
          {(Object.entries(MODE_META) as [AppMode, (typeof MODE_META)[AppMode]][]).map(([id, meta]) => {
            const Icon = meta.icon;
            return (
              <button
                key={id}
                type="button"
                onClick={() => {
                  onModeChange(id);
                  onClose();
                }}
                className={`w-full flex items-center gap-2.5 px-3 py-2 text-left transition-colors ${
                  mode === id ? "bg-white/[0.08] text-zinc-100" : "text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-200"
                }`}
              >
                <Icon size={15} />
                <span className="min-w-0">
                  <span className="block text-[13px] font-medium">{meta.label}</span>
                  <span className="block text-[10px] text-zinc-500">{meta.description}</span>
                </span>
              </button>
            );
          })}
        </div>
      </div>
    </>
  );
}

export type ComposerMentionSection = {
  id: "plugin" | "builtin-tool" | "custom-tool" | "skill" | "service" | "file" | "chat" | "group" | "mcp";
  label: string;
  badge: string;
  tone: "sky" | "cyan" | "violet" | "blue" | "emerald" | "amber" | "rose" | "neutral";
};

const COMPOSER_MENTION_SECTIONS: Record<ComposerMentionSection["id"], ComposerMentionSection> = {
  chat: { id: "chat", label: "チャット", badge: "チャット", tone: "sky" },
  group: { id: "group", label: "グループ", badge: "グループ", tone: "blue" },
  mcp: { id: "mcp", label: "MCP サーバー", badge: "MCP", tone: "cyan" },
  plugin: { id: "plugin", label: "プラグイン・接続", badge: "接続", tone: "cyan" },
  "builtin-tool": { id: "builtin-tool", label: "内蔵ツール", badge: "内蔵", tone: "sky" },
  "custom-tool": { id: "custom-tool", label: "追加したツール", badge: "追加", tone: "emerald" },
  skill: { id: "skill", label: "スキル", badge: "スキル", tone: "violet" },
  service: { id: "service", label: "ツールのまとまり", badge: "まとまり", tone: "blue" },
  file: { id: "file", label: "ワークスペースのファイル", badge: "ファイル", tone: "neutral" },
};

const BUILTIN_TOOL_PACK_IDS = new Set([
  "defaultspack",
  "rumi_default_tools_pack",
]);

/**
 * Separate catalog-backed integrations from bundled tools without inferring a
 * provider from a tool name. A service id is an explicit catalog declaration;
 * a pack outside the known bundled packs is likewise a separately installed
 * capability.
 */
export function composerMentionSectionForTool(item: ComposerExtensionItem): ComposerMentionSection {
  const sourcePackId = String(item.sourcePackId ?? "").trim().toLowerCase();
  if (sourcePackId === "user_dynamic") return COMPOSER_MENTION_SECTIONS["custom-tool"];
  if (String(item.serviceId ?? "").trim() || (sourcePackId && !BUILTIN_TOOL_PACK_IDS.has(sourcePackId))) {
    return COMPOSER_MENTION_SECTIONS.plugin;
  }
  return COMPOSER_MENTION_SECTIONS["builtin-tool"];
}

/**
 * Keep a service group beside the catalog entries it contains. Groups that
 * span catalog origins intentionally remain a separate catch-all, since a
 * label such as a provider name cannot reliably describe a mixed bundle.
 */
export function composerMentionSectionForToolGroup(
  items: readonly ComposerExtensionItem[],
): ComposerMentionSection {
  let section: ComposerMentionSection | null = null;
  for (const item of items) {
    if (item.disabled) continue;
    const itemSection = composerMentionSectionForTool(item);
    if (section && section.id !== itemSection.id) {
      return COMPOSER_MENTION_SECTIONS.service;
    }
    section = itemSection;
  }
  return section ?? COMPOSER_MENTION_SECTIONS.service;
}

const COMPOSER_MENTION_SECTION_ORDER: readonly ComposerMentionSection["id"][] = [
  "plugin",
  "builtin-tool",
  "custom-tool",
  "skill",
  "service",
  "file",
  "chat",
  "group",
  "mcp",
];

export type ComposerAtMentionCandidate =
  | { kind: "tool"; id: string; label: string; displayLabel?: string; description?: string; item: ComposerExtensionItem; section: ComposerMentionSection }
  | { kind: "service"; id: string; label: string; displayLabel?: string; description?: string; service: ToolGroup; section: ComposerMentionSection }
  | { kind: "skill"; id: string; label: string; displayLabel?: string; description?: string; skill: ComposerSkillItem; section: ComposerMentionSection }
  | { kind: "entity"; id: string; label: string; displayLabel?: string; description?: string; entity: ComposerEntityCandidate; section: ComposerMentionSection }
  | { kind: "file"; id: string; label: string; displayLabel?: string; description?: string; file: string; section: ComposerMentionSection };

/** Build a semantic candidate while retaining exclusion intent for tools. */
export function composerAtMentionCandidateWidget(
  candidate: Exclude<ComposerAtMentionCandidate, { kind: "entity" }>,
  exclude = false,
): DroppedWidget {
  const negative = exclude && (candidate.kind === "tool" || candidate.kind === "service");
  const syntax = `@${negative ? "-" : ""}${candidate.label}`;
  const widget = candidate.kind === "tool"
    ? composerToolMentionWidget(candidate.item, syntax)
    : candidate.kind === "service"
      ? composerServiceMentionWidget({
        id: candidate.service.id,
        label: candidate.service.label,
        description: candidate.service.description,
        toolIds: candidate.service.items.filter((item) => !item.disabled).map((item) => item.id),
      })
      : candidate.kind === "skill"
        ? composerSkillMentionWidget(candidate.skill)
        : composerFileMentionWidget(candidate.file);
  if (!negative) return widget;
  return {
    ...widget,
    id: `exclude:${widget.id}`,
    metadata: {
      ...widget.metadata,
      mention: {
        ...(widget.metadata?.mention as Record<string, unknown>),
        syntax,
        intent: "exclude",
      },
    },
  };
}

/** Ensure Settings Mode stays available when a host catalog omits skills. */
export function composerMentionSkills(skills: ComposerSkillItem[]): ComposerSkillItem[] {
  return withSettingsAssistantSkill(skills);
}

/** Keep palette section headings contiguous while preserving each section's source order. */
export function orderComposerAtMentionCandidates(
  candidates: readonly ComposerAtMentionCandidate[],
): ComposerAtMentionCandidate[] {
  return COMPOSER_MENTION_SECTION_ORDER.flatMap((sectionId) => (
    candidates.filter((candidate) => candidate.section.id === sectionId)
  ));
}

export type JsonListPanelItem = {
  id: string;
  title: string;
  description?: string;
  section?: {
    id: string;
    label: string;
  };
  icon?: string;
  fallbackIcon: "tool" | "service" | "skill" | "file" | "command";
  badges?: Array<{
    label: string;
    tone: "sky" | "cyan" | "violet" | "blue" | "emerald" | "amber" | "rose" | "neutral";
  }>;
  disabled?: boolean;
  disabledReason?: string;
};

export type JsonListPanelTemplate = {
  version: 1;
  maxHeightRem?: number;
  header: {
    showCount?: boolean;
  };
  item: {
    showDescription?: boolean;
  };
};

/**
 * Complete JSON-serializable palette input.  Triggers such as `@` and `/`
 * only produce this data; the panel renderer has no trigger-specific branch.
 */
export type JsonListPanelPayload = {
  version: 1;
  id: string;
  listboxId: string;
  ariaLabel: string;
  testId?: string;
  maxHeightRem?: number;
  header: {
    label: string;
    icon?: string;
    showCount?: boolean;
  };
  empty: {
    message: string;
  };
  item: {
    prefix?: string;
    showDescription?: boolean;
  };
  items: JsonListPanelItem[];
};

const COMPOSER_PALETTE_TEMPLATE = composerPaletteTemplateJson as JsonListPanelTemplate;

const JSON_LIST_BADGE_TONE_CLASS: Record<NonNullable<JsonListPanelItem["badges"]>[number]["tone"], string> = {
  sky: "border-sky-400/25 text-sky-200",
  cyan: "border-cyan-500/25 text-cyan-300",
  violet: "border-violet-500/25 text-violet-300",
  blue: "border-sky-500/25 text-sky-300",
  emerald: "border-emerald-500/20 text-emerald-300",
  amber: "border-amber-500/25 text-amber-300",
  rose: "border-rose-500/30 text-rose-300",
  neutral: "border-zinc-500/25 text-zinc-300",
};

const JSON_LIST_FALLBACK_ICON: Record<JsonListPanelItem["fallbackIcon"], LucideIcon> = {
  tool: Wrench,
  service: Wrench,
  skill: BrainCircuit,
  file: FileText,
  command: SlidersHorizontal,
};

/** Merge the shared visual template with a trigger-neutral JSON payload. */
export function jsonListPanelPayload(
  payload: Omit<JsonListPanelPayload, "version" | "maxHeightRem"> & Partial<Pick<JsonListPanelPayload, "maxHeightRem">>,
): JsonListPanelPayload {
  return {
    version: COMPOSER_PALETTE_TEMPLATE.version,
    maxHeightRem: payload.maxHeightRem ?? COMPOSER_PALETTE_TEMPLATE.maxHeightRem,
    ...payload,
    header: {
      showCount: COMPOSER_PALETTE_TEMPLATE.header.showCount,
      ...payload.header,
    },
    item: {
      showDescription: COMPOSER_PALETTE_TEMPLATE.item.showDescription,
      ...payload.item,
    },
  };
}

/** Render a picker using only JSON-serializable panel and item data. */
export function JsonListPanel({
  payload,
  activeIndex,
  onActiveIndexChange,
  onSelect,
  footer,
}: {
  payload: JsonListPanelPayload;
  footer?: ReactNode;
  activeIndex: number;
  onActiveIndexChange: (index: number) => void;
  onSelect: (index: number) => void;
}) {
  const HeaderIcon = composerIconForName(payload.header.icon, Wrench);
  const maxHeightRem = Math.min(Math.max(payload.maxHeightRem ?? 24, 12), 32);
  const style = { "--rumi-json-list-max-height": `${maxHeightRem}rem` } as CSSProperties;

  return (
    <div
      id={payload.listboxId}
      role="listbox"
      aria-label={payload.ariaLabel}
      data-testid={payload.testId}
      data-json-list-template={payload.id}
      data-composer-mention-menu
      style={style}
      className="rumi-composer-mention-menu absolute bottom-full left-0 mb-2 rumi-layer-modal w-full overflow-hidden rumi-popover"
    >
      <div className="border-b border-white/[0.06] px-3 py-2 flex items-center justify-between gap-2">
        <span className="inline-flex min-w-0 items-center gap-2">
          <HeaderIcon size={13} className="text-zinc-500" />
          <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">{payload.header.label}</span>
        </span>
        {payload.header.showCount !== false && <span className="text-[10px] text-zinc-600">{payload.items.length}</span>}
      </div>
      <div className="overflow-y-auto py-1">
        {payload.items.length === 0 && (
          <div role="status" aria-live="polite" data-testid={`${payload.id}-empty`} className="px-3 py-4 text-sm leading-relaxed text-zinc-400">
            {payload.empty.message}
          </div>
        )}
        {payload.items.map((item, index) => {
          const Icon = composerIconForName(item.icon, JSON_LIST_FALLBACK_ICON[item.fallbackIcon]);
          const section = item.section;
          const previousSectionId = payload.items[index - 1]?.section?.id;
          const startsSection = section && section.id !== previousSectionId;
          return (
            <div key={item.id}>
              {startsSection && (
                <div aria-hidden="true" className="px-3 pb-1 pt-2 text-[10px] font-medium text-zinc-500">
                  {section?.label}
                </div>
              )}
              <button
              id={`${payload.id}-option-${index}`}
              type="button"
              role="option"
              aria-selected={index === activeIndex}
              aria-disabled={item.disabled || undefined}
              disabled={item.disabled}
              title={item.disabledReason}
              tabIndex={-1}
              onMouseEnter={() => onActiveIndexChange(index)}
              onClick={() => onSelect(index)}
              className={`flex min-h-11 w-full items-center justify-between gap-3 px-3 py-2 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
                index === activeIndex ? "bg-white/[0.08] text-zinc-100" : "hover:bg-white/[0.05]"
              }`}
            >
              <span className="flex min-w-0 items-center gap-2">
                <span className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-lg border border-white/[0.07] bg-white/[0.04] text-zinc-300">
                  <Icon size={14} />
                </span>
                <span className="min-w-0">
                  <span className="block truncate text-[13px] text-zinc-200">{payload.item.prefix ?? ""}{item.title}</span>
                  {payload.item.showDescription !== false && item.description && <span className="block truncate text-[10px] text-zinc-500">{item.description}</span>}
                </span>
              </span>
              {item.badges && item.badges.length > 0 && (
                <span className="flex flex-shrink-0 items-center gap-1">
                  {item.badges.map((badge, badgeIndex) => (
                    <span key={`${badge.label}:${badgeIndex}`} className={`rounded-full border px-1.5 py-0.5 text-[9px] leading-none ${JSON_LIST_BADGE_TONE_CLASS[badge.tone]}`}>
                      {badge.label}
                    </span>
                  ))}
                </span>
              )}
              </button>
            </div>
          );
        })}
      </div>
      {footer && <div className="border-t border-white/[0.06]">{footer}</div>}
    </div>
  );
}

export function atMentionPalettePayload(candidates: ComposerAtMentionCandidate[]): JsonListPanelPayload {
  const items: JsonListPanelItem[] = candidates.map((candidate) => {
    const icon = candidate.kind === "tool"
      ? candidate.item.ui?.composer_icon ?? candidate.item.ui?.item_icon ?? candidate.item.ui?.group_icon
      : candidate.kind === "service"
        ? candidate.service.id
        : candidate.kind === "skill"
          ? String(candidate.skill.metadata?.icon ?? candidate.skill.id)
          : candidate.kind === "entity" ? candidate.entity.kind : candidate.file;
    return {
      id: candidate.id,
      title: candidate.displayLabel ?? candidate.label,
      description: candidate.description,
      section: { id: candidate.section.id, label: candidate.section.label },
      icon,
      fallbackIcon: candidate.kind === "entity" ? "service" : candidate.kind,
      disabled: candidate.kind === "entity" && !candidate.entity.available,
      badges: [{ label: candidate.section.badge, tone: candidate.section.tone }],
    };
  });

  return jsonListPanelPayload({
    id: "composer-at-mention",
    listboxId: AT_MENTION_LISTBOX_ID,
    ariaLabel: "メンション候補",
    testId: "composer-at-mention-candidates",
    header: { label: "メンション", icon: "wrench" },
    empty: { message: "一致する候補はありません。Enterで本文を送信、Tabで次の操作へ移動できます。" },
    item: { prefix: "@" },
    items,
  });
}

const COMMAND_LISTBOX_ID = "composer-slash-command-listbox";
const COMMAND_ARGUMENT_LISTBOX_ID = "composer-slash-command-argument-listbox";

export function commandPalettePayload(commands: ComposerCommandItem[]): JsonListPanelPayload {
  const riskTone: Record<ComposerCommandItem["risk"], NonNullable<JsonListPanelItem["badges"]>[number]["tone"]> = {
    low: "emerald",
    medium: "amber",
    high: "rose",
  };

  return jsonListPanelPayload({
    id: "composer-slash-command",
    listboxId: COMMAND_LISTBOX_ID,
    ariaLabel: "Composer commands",
    testId: "composer-slash-command-candidates",
    header: { label: "Commands", icon: "wrench" },
    empty: { message: "一致するコマンドはありません。" },
    item: { prefix: "/" },
    items: commands.map((command) => {
      const state = commandStateLabel(command);
      return {
        id: command.id,
        title: command.name ?? command.id,
        description: command.availability?.status === "unavailable"
          ? command.availability.reason ?? command.description
          : command.description,
        icon: `${command.id} ${command.name} ${command.category}`,
        fallbackIcon: "command" as const,
        badges: [
          ...(command.risk !== "low" ? [{ label: command.risk, tone: riskTone[command.risk] }] : []),
          ...(command.availability?.status === "unavailable"
            ? [{ label: "unavailable", tone: "neutral" as const }]
            : []),
          ...(state ? [{ label: state, tone: state === "オン" ? "sky" as const : "neutral" as const }] : []),
        ],
        disabled: command.availability?.status === "unavailable",
        ...(command.availability?.reason
          ? { disabledReason: command.availability.reason }
          : {}),
      };
    }),
  });
}

export function commandArgumentPalettePayload(
  guide: CommandArgumentGuide,
): JsonListPanelPayload {
  return jsonListPanelPayload({
    id: "composer-slash-command-argument",
    listboxId: COMMAND_ARGUMENT_LISTBOX_ID,
    ariaLabel: guide.accessibleText,
    testId: "composer-command-argument-guide",
    header: { label: "Commands", icon: "wrench" },
    empty: { message: "引数を入力してください。" },
    item: { prefix: "/" },
    items: [{
      id: guide.command.slice(1),
      title: `${guide.command.slice(1)} ${guide.arguments.map((argument) => `<${argument}>`).join(" ")}`,
      description: "入力欄に値を続けて入力し、Enterで実行します。",
      icon: "command form input",
      fallbackIcon: "command",
      badges: [{ label: "入力中", tone: "sky" }],
    }],
  });
}

export function filterAtMentionFiles(files: string[], query: string): string[] {
  if (!query) return files.slice(0, 20);
  const q = query.toLowerCase();
  return files.filter((file) => file.toLowerCase().includes(q)).slice(0, 20);
}

export type ComposerInlineMentionPart = {
  mention: boolean;
  text: string;
};

/** Search only supplied authenticated entity candidates, retaining their identities. */
export function composerEntityMentionCandidates(candidates: ComposerEntityCandidate[], query: string): ComposerAtMentionCandidate[] {
  const normalized = query.trim().toLocaleLowerCase();
  const prefix = normalized.match(/^(chat|group|mcp)(?::|\s|$)/);
  const text = prefix ? normalized.slice(prefix[0].length).trim() : normalized;
  return candidates.filter((candidate) => (!prefix || candidate.kind === prefix[1])
    && (!text || `${candidate.id} ${candidate.label}`.toLocaleLowerCase().includes(text)))
    .map((entity) => ({ kind: "entity", id: `${entity.kind}:${entity.id}`, label: entity.syntax.slice(1),
      displayLabel: entity.label, description: entity.syntax, entity,
      section: COMPOSER_MENTION_SECTIONS[entity.kind] }));
}

/** Copy only anchored occurrences wholly inside the current native selection. */
export function composerReferencesForSelection(input: string, start: number, end: number, widgets: DroppedWidget[], references: ComposerEntityReference[]): ComposerEntityReference[] {
  const value = input.slice(start, end);
  return widgets.flatMap((widget) => {
    const range = confirmedComposerMentionRange(widget, input);
    if (!range || range.start < start || range.end > end) return [];
    const mention = composerMentionMetadataFromWidgets([widget])[0];
    if (!mention) return [];
    const reference = [...references].reverse().find((item) => item.kind === mention.kind && item.id === mention.id && item.syntax === range.syntax && item.profileId === mention.profileId);
    if (!reference) return [];
    return [{ ...reference, confirmedRange: { value, start: range.start - start, end: range.end - start } }];
  });
}

/** Saved route freshness includes its execution connection, independent of object identity. */
export function composerReferenceModelBinding(profile: ModelProfile | null): string {
  return JSON.stringify(profile ? [profile.profile_id, profile.provider_id, profile.model_id, modelProfileConnectionId(profile)] : null);
}

export type ComposerReferenceInsertionSnapshot = {
  input: string; value: string; start: number; end: number;
  droppedWidgets: DroppedWidget[]; entityReferences: ComposerEntityReference[];
  profileId?: string; modelProfile: ModelProfile | null; modelBinding?: string; imeGeneration: number;
  blocked: boolean;
};

/** Async confirmation may update only the exact draft and caret that requested it. */
export function composerReferenceInsertionIsCurrent(captured: ComposerReferenceInsertionSnapshot, live: ComposerReferenceInsertionSnapshot): boolean {
  return !live.blocked && captured.input === live.input && captured.value === live.value
    && captured.start === live.start && captured.end === live.end
    && captured.droppedWidgets === live.droppedWidgets && captured.entityReferences === live.entityReferences
    && captured.profileId === live.profileId && (captured.modelBinding ?? composerReferenceModelBinding(captured.modelProfile)) === (live.modelBinding ?? composerReferenceModelBinding(live.modelProfile))
    && captured.imeGeneration === live.imeGeneration;
}

/** Only a visible, topmost composer at the release point may accept a history drop. */
export function validComposerHistoryDropTarget(target: HTMLElement, point: { x: number; y: number }, targetId: string, top: Element | null): boolean {
  const rect = target.getBoundingClientRect();
  const view = target.ownerDocument?.defaultView;
  if (view) {
    for (let node: HTMLElement | null = target; node; node = node.parentElement) {
      const style = view.getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse" || style.opacity === "0") return false;
    }
  }
  return target.id === targetId && target.isConnected && rect.width > 0 && rect.height > 0
    && Number.isFinite(point.x) && Number.isFinite(point.y)
    && point.x >= rect.left && point.x <= rect.right && point.y >= rect.top && point.y <= rect.bottom
    && top !== null && target.contains(top);
}

/** Reconfirmation replaces the same occurrence while retaining other anchors. */
export function replaceConfirmedComposerWidget(widgets: DroppedWidget[], widget: DroppedWidget): DroppedWidget[] {
  const occurrenceKey = (item: DroppedWidget) => {
    const mention = composerMentionMetadataFromWidgets([item])[0];
    const entity = mention ? JSON.stringify([mention.kind, mention.id, mention.profileId ?? null])
      : `${item.type}:${item.sourceItemId || item.id.replace(/^exclude:/, "")}`;
    const confirmation = item.metadata?.composer_confirmation as { value?: unknown } | undefined;
    const range = typeof confirmation?.value === "string"
      ? confirmedComposerMentionRange(item, confirmation.value) : null;
    return range ? JSON.stringify([entity, range.start, range.end, range.syntax]) : entity;
  };
  return [...widgets.filter((prior) => occurrenceKey(prior) !== occurrenceKey(widget)), widget];
}

/** Split composer text into ordinary and selected semantic-mention runs. */
export function composerInlineMentionParts(
  input: string,
  widgets: DroppedWidget[],
): ComposerInlineMentionPart[] {
  if (!input) return [];
  const ranges = widgets.filter((widget) => widget.metadata?.source === "composer_at_mention")
    .map((widget) => confirmedComposerMentionRange(widget, input))
    .filter((range): range is NonNullable<typeof range> => range !== null)
    .sort((left, right) => left.start - right.start || right.end - left.end);
  const matches: Array<{ start: number; end: number }> = [];
  let cursor = 0;
  for (const range of ranges) {
    if (range.start < cursor) continue;
    matches.push(range);
    cursor = range.end;
  }
  if (matches.length === 0) return [{ mention: false, text: input }];

  const parts: ComposerInlineMentionPart[] = [];
  cursor = 0;
  for (const match of matches) {
    if (match.start > cursor) parts.push({ mention: false, text: input.slice(cursor, match.start) });
    parts.push({ mention: true, text: input.slice(match.start, match.end) });
    cursor = match.end;
  }
  if (cursor < input.length) parts.push({ mention: false, text: input.slice(cursor) });
  return parts;
}

export function atomicComposerMentionEdit(
  input: string,
  selectionStart: number,
  selectionEnd: number,
  key: "Backspace" | "Delete",
  widgets: DroppedWidget[],
): { value: string; cursor: number } | null {
  let cursor = 0;
  const ranges: Array<{ start: number; end: number }> = [];
  for (const part of composerInlineMentionParts(input, widgets)) {
    const start = cursor;
    cursor += part.text.length;
    if (part.mention) ranges.push({ start, end: cursor });
  }
  if (ranges.length === 0) return null;

  let removeStart = Math.min(selectionStart, selectionEnd);
  let removeEnd = Math.max(selectionStart, selectionEnd);
  const collapsed = removeStart === removeEnd;
  const affected = ranges.filter((range) => (
    collapsed
      ? key === "Backspace"
        ? range.start < removeStart && removeStart <= range.end
        : range.start <= removeStart && removeStart < range.end
      : range.start < removeEnd && range.end > removeStart
  ));
  if (affected.length === 0) return null;
  removeStart = Math.min(removeStart, ...affected.map((range) => range.start));
  removeEnd = Math.max(removeEnd, ...affected.map((range) => range.end));
  return {
    value: `${input.slice(0, removeStart)}${input.slice(removeEnd)}`,
    cursor: removeStart,
  };
}

/** Remove the unfinished mention that currently owns the textarea cursor. */
export function dismissActiveAtMentionText(
  input: string,
  cursorPos: number,
  knownValues?: Iterable<string>,
): { value: string; cursor: number } {
  const cursor = Math.min(Math.max(cursorPos, 0), input.length);
  const activeMention = activeMentionAtCursor(input, cursor, knownValues);
  if (!activeMention) return { value: input, cursor };
  return {
    value: `${input.slice(0, activeMention.start)}${input.slice(cursor)}`,
    cursor: activeMention.start,
  };
}

export type ModelCandidateMenuKeyAction =
  | { handled: false }
  | { handled: true; type: "move"; nextIndex: number }
  | { handled: true; type: "select"; index: number }
  | { handled: true; type: "close" };

export type AtMentionMenuKeyAction =
  | { handled: false }
  | { handled: true; type: "move"; nextIndex: number }
  | { handled: true; type: "select"; index: number }
  | { handled: true; type: "close" };

export function nextModelCandidateIndex(currentIndex: number, candidateCount: number, direction: 1 | -1): number {
  if (candidateCount <= 0) return 0;
  return (currentIndex + direction + candidateCount) % candidateCount;
}

export function modelCandidateMenuKeyAction(
  key: string,
  shiftKey: boolean,
  currentIndex: number,
  candidateCount: number,
): ModelCandidateMenuKeyAction {
  if (candidateCount <= 0) return { handled: false };
  if (key === "Tab" || key === "ArrowDown" || key === "ArrowUp") {
    const direction = key === "ArrowUp" || (key === "Tab" && shiftKey) ? -1 : 1;
    return {
      handled: true,
      type: "move",
      nextIndex: nextModelCandidateIndex(currentIndex, candidateCount, direction),
    };
  }
  if (key === "Enter") {
    return { handled: true, type: "select", index: Math.min(Math.max(currentIndex, 0), candidateCount - 1) };
  }
  if (key === "Escape") {
    return { handled: true, type: "close" };
  }
  return { handled: false };
}

export function atMentionMenuKeyAction(
  key: string,
  shiftKey: boolean,
  currentIndex: number,
  candidateCount: number,
  composition: { isComposing?: boolean; keyCode?: number; repeat?: boolean } = {},
): AtMentionMenuKeyAction {
  if (composition.isComposing || composition.keyCode === 229) return { handled: false };
  if (key === "Escape") return { handled: true, type: "close" };
  if (candidateCount <= 0) return { handled: false };
  if (!shiftKey && mentionConfirmationKey({ key, shiftKey, ...composition })) {
    return { handled: true, type: "select", index: Math.min(Math.max(currentIndex, 0), candidateCount - 1) };
  }
  if (key === "ArrowDown" || key === "ArrowUp") {
    const direction = key === "ArrowUp" ? -1 : 1;
    return {
      handled: true,
      type: "move",
      nextIndex: nextModelCandidateIndex(currentIndex, candidateCount, direction),
    };
  }
  return { handled: false };
}

export function shouldFocusComposerForSlashKey(
  event: Pick<KeyboardEvent, "key" | "metaKey" | "ctrlKey" | "altKey" | "defaultPrevented" | "isComposing">,
  target: EventTarget | null,
): boolean {
  if (event.defaultPrevented || event.isComposing) return false;
  if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return false;
  if (typeof Element === "undefined") return true;
  if (!(target instanceof Element)) return true;
  const tagName = target.tagName.toLowerCase();
  return tagName !== "input" && tagName !== "textarea" && tagName !== "select" && !target.closest("[contenteditable='true']");
}

function modelCandidateTitle(candidate: ModelCommandCandidate): string {
  return String(candidate.display_name ?? candidate.profile_id ?? "model");
}

function modelCandidateSubtitle(candidate: ModelCommandCandidate): string {
  const explicit = String(candidate.subtitle ?? "").trim();
  if (explicit) return explicit;
  const provider = String(candidate.provider_display_name ?? candidate.provider_id ?? "").trim();
  const model = String(candidate.model_id ?? candidate.qualified_model_id ?? candidate.profile_id ?? "").trim();
  return [provider, model].filter(Boolean).join(" / ");
}

function modelCandidateApiKeyBadge(candidate: ModelCommandCandidate): string | null {
  if (candidate.requires_api_key === true || candidate.api_key_required === true) return "API key";
  if (candidate.api_key_configured === true || candidate.configured === true) return "key set";
  const availability = candidate.availability ?? {};
  if (availability.configured === true || availability.status === "configured" || availability.status === "active") return "key set";
  return null;
}

type PopupAnchorRect = Pick<DOMRect, "left" | "right" | "top">;

export function modelCandidatePopupStyleForAnchor(
  anchorRect: PopupAnchorRect | null,
  viewportWidth: number,
  preferredWidth = 460,
): CSSProperties | undefined {
  if (!anchorRect || viewportWidth <= 0) return undefined;
  const width = Math.min(preferredWidth, Math.max(260, viewportWidth - 16));
  const left = Math.max(8, Math.min(anchorRect.right - width, viewportWidth - width - 8));
  const top = Math.max(8, anchorRect.top - 8);
  return {
    left,
    top,
    width,
    transform: "translateY(-100%)",
  };
}

function ModelCommandCandidatePopup({
  candidates,
  activeIndex,
  onActiveIndexChange,
  onSelect,
  onClose,
  style,
}: {
  candidates: ModelCommandCandidate[];
  activeIndex: number;
  onActiveIndexChange: (index: number) => void;
  onSelect: (candidate: ModelCommandCandidate) => void;
  onClose?: () => void;
  style?: CSSProperties;
}) {
  if (candidates.length === 0) return null;

  return (
    <div
      role="listbox"
      aria-label="Model candidates"
      style={style}
      className="fixed rumi-layer-modal w-[min(460px,calc(100vw-32px))] overflow-hidden rumi-popover"
    >
      <div className="flex items-center justify-between gap-2 border-b border-white/[0.06] px-3 py-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Models</span>
        {onClose && (
          <button
            type="button"
            aria-label="close model candidates"
            onClick={onClose}
            className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-white/[0.05] hover:text-zinc-200"
          >
            <X size={13} />
          </button>
        )}
      </div>
      <div className="max-h-64 overflow-y-auto py-1">
        {candidates.map((candidate, index) => {
          const badge = modelCandidateApiKeyBadge(candidate);
          return (
            <button
              key={candidate.profile_id}
              type="button"
              role="option"
              aria-selected={index === activeIndex}
              onMouseEnter={() => onActiveIndexChange(index)}
              onClick={() => onSelect(candidate)}
              className={`flex w-full items-center justify-between gap-3 px-3 py-2 text-left transition-colors ${
                index === activeIndex ? "bg-white/[0.08] text-zinc-100" : "hover:bg-white/[0.05]"
              }`}
            >
              <span className="min-w-0">
                <span className="block truncate text-sm font-medium text-zinc-100">{modelCandidateTitle(candidate)}</span>
                <span className="block truncate text-[11px] text-zinc-500">{modelCandidateSubtitle(candidate)}</span>
              </span>
              {badge && (
                <span
                  className={`flex-shrink-0 rounded-full border px-2 py-0.5 text-[10px] ${
                    badge === "API key"
                      ? "border-amber-500/30 text-amber-300"
                      : "border-emerald-500/25 text-emerald-300"
                  }`}
                >
                  {badge}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function ComposerRenderer({
  surfaceMode = "standard",
  submissionDisabled = false,
  input,
  placeholder,
  isNewConversation = false,
  isGenerating,
  selectedProfile,
  widgetContext,
  favoriteProfiles,
  modelProfiles = [],
  modelSelectorSchema = DEFAULT_MODEL_SELECTOR_SCHEMA,
  thinkingLevel,
  contextUsage,
  inlineExtensions,
  belowExtensions,
  skillExtensions = [],
  commands = [],
  composerInput = null,
  structuredInputValues = {},
  modelCommandCandidates = [],
  modelPickerRequestId = 0,
  modelStatusIndicators = [],
  voiceInputEnabled = true,
  voiceInputUseAi = false,
  voiceScopeKey,
  manualRuntimeModeSelectionEnabled = false,
  mode = "chat",
  codingContext = null,
  codingWorkspaces = [],
  selectedCodingWorkspaceId = null,
  projects = [],
  selectedProjectId = null,
  attachedFiles = [],
  pendingMentionAttachmentPaths = [],
  droppedWidgets = [],
  entityReferences = [],
  entityCandidates = [], entityCandidateStatus, entityCandidatesHaveMore = false, entityCandidatesLoadMore,
  onEntityCandidateConfirm, onHistoryReferenceDrop, onReferenceConfirmationPendingChange, historyReferenceTargetProfileId, historyReferences = [],
  selectedToolIds = [],
  actionApprovalMode = "ask",
  actionApprovalModes,
  showActionApprovalControl = true,
  showToolSelectionControl = false,
  toolSelectionMode = "auto",
  toolSelectionReview = null,
  keyboardButtonNavigation = true,
  steerStatus = null,
  steerBusy = false,
  steerControlsReady = true,
  pendingRecovery,
  steerPreviewItems = [],
  suppressPopovers = false,
  onOpenModelManager,
  onOpenToolSettings,
  onActionApprovalModeChange,
  onToolSelectionModeChange,
  onToolSelectionReviewApprove,
  onToolSelectionReviewEdit,
  onToolSelectionReviewNoTools,
  onToolSelectionReviewCancel,
  onSwitchToVisionModel,
  onExtensionSelect,
  onLocalCommandSubmit,
  onCommandSelect,
  onModelCommandCandidateSelect,
  onModelCommandCandidatesClose,
  onModelProfileSelect,
  onProviderApiKeySave,
  onThinkingLevelChange,
  onInputChange,
  onStructuredInputChange,
  onSubmit,
  onStopGenerating,
  onSteerSubmit,
  onModeChange,
  onFileAttach,
  onAtFileAttach,
  onPendingMentionAttachmentRemove,
  onFileRemove,
  onDropWidget,
  onDroppedWidgetsChange,
  onEntityReferencesChange,
  onWidgetAction,
  onWidgetToggle,
  onCodingBranchSwitch,
  onCodingDirectoryChange,
  onCodingWorkspaceSelect,
  onCodingWorkspaceTrust,
  onCodingWorkspaceCreate,
  onCodingWorkspacesRefresh,
  onCodingContextRefresh,
  onProjectSelect,
  onProjectDirectorySelect,
  onProjectWorkspaceCreate,
  projectProfileId,
  onProjectStoragePrepare,
}: ComposerRendererProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [commandMenuOpen, setCommandMenuOpen] = useState(false);
  const previousTextareaCollapsed = useRef(false);
  const textareaResizeAnimation = useRef<Animation | null>(null);
  const [openFolder, setOpenFolder] = useState<"tools" | "models" | "commands">("tools");
  const [openToolGroup, setOpenToolGroup] = useState<string | null>(null);
  const [modelDropdownOpen, setModelDropdownOpen] = useState(false);
  const modelDropdownTriggerRef = useRef<HTMLButtonElement | null>(null);
  const [openModelStatusId, setOpenModelStatusId] = useState<string | null>(null);
  const [apiKeyPromptProfile, setApiKeyPromptProfile] = useState<ModelProfile | null>(null);
  const [locallyConfiguredProviders, setLocallyConfiguredProviders] = useState<Set<string>>(() => new Set());
  const [modeSelectorOpen, setModeSelectorOpen] = useState(false);
  const [atMentionOpen, setAtMentionOpen] = useState(false);
  const [atMentionQuery, setAtMentionQuery] = useState("");
  const [atMentionStart, setAtMentionStart] = useState<number | null>(null);
  const [selectedAtMentionIndex, setSelectedAtMentionIndex] = useState(0);
  const [selectedCommandIndex, setSelectedCommandIndex] = useState(0);
  const [selectedModelCandidateIndex, setSelectedModelCandidateIndex] = useState(0);
  const [composerPopoverStyle, setComposerPopoverStyle] = useState<CSSProperties | undefined>(undefined);
  const [voiceStatus, setVoiceStatus] = useState<ComposerVoicePhase>("idle");
  const [voiceElapsedSeconds, setVoiceElapsedSeconds] = useState(0);
  const [voiceError, setVoiceError] = useState("");
  const [voiceTranscript, setVoiceTranscript] = useState("");
  const [voiceLanguage, setVoiceLanguage] = useState(() => composerVoiceLanguage(
    typeof document === "undefined" ? undefined : document.documentElement.lang,
    typeof navigator === "undefined" ? undefined : navigator.language,
  ));

  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const [textareaCollapsed, setTextareaCollapsed] = useState(false);
  const [textareaCanCollapse, setTextareaCanCollapse] = useState(false);
  const [textareaFocused, setTextareaFocused] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuButtonRef = useRef<HTMLButtonElement | null>(null);
  const commandMenuRef = useRef<HTMLDivElement | null>(null);
  const commandMenuButtonRef = useRef<HTMLButtonElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const imeActiveRef = useRef(false);
  const imeGenerationRef = useRef(0);
  const imeEndedAtRef = useRef(-Infinity);
  const historyDropTargetId = useId();
  const historyDropTargetRef = useRef<HTMLDivElement | null>(null);
  const [entityConfirmationError, setEntityConfirmationError] = useState("");
  const entityConfirmationPendingRef = useRef(false);
  useEffect(() => () => onReferenceConfirmationPendingChange?.(false), [onReferenceConfirmationPendingChange]);
  const liveReferenceDraftRef = useRef({ input, droppedWidgets, entityReferences, profileId: historyReferenceTargetProfileId, modelProfile: selectedProfile, modelBinding: composerReferenceModelBinding(selectedProfile), blocked: submissionDisabled || isGenerating || voiceStatus !== "idle" });
  liveReferenceDraftRef.current = { input, droppedWidgets, entityReferences, profileId: historyReferenceTargetProfileId, modelProfile: selectedProfile, modelBinding: composerReferenceModelBinding(selectedProfile), blocked: submissionDisabled || isGenerating || voiceStatus !== "idle" };
  const nativeMentionEditRef = useRef<{ start: number; end: number } | undefined>(undefined);
  const inlineMentionLayerRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    const captureEdit = (event: Event) => {
      const native = event as InputEvent;
      if (!native.inputType.startsWith("insert") && !["deleteContentBackward", "deleteContentForward", "deleteByCut"].includes(native.inputType)) {
        nativeMentionEditRef.current = undefined;
        return;
      }
      let start = textarea.selectionStart;
      let end = textarea.selectionEnd;
      if (start === end && native.inputType === "deleteContentBackward") start -= [...textarea.value.slice(0, start)].at(-1)?.length ?? 0;
      if (start === end && native.inputType === "deleteContentForward") end += [...textarea.value.slice(end)][0]?.length ?? 0;
      nativeMentionEditRef.current = { start, end };
    };
    textarea.addEventListener("beforeinput", captureEdit);
    return () => textarea.removeEventListener("beforeinput", captureEdit);
  });
  const voiceRecorderRef = useRef<ActiveAudioRecorder | null>(null);
  const voiceGenerationRef = useRef(new ComposerVoiceOperation());
  const attachmentTranscriptionRef = useRef(new ComposerVoiceOperation());
  const voiceStartedAtRef = useRef(0);
  const voiceOriginalDraftRef = useRef({ value: input, selection: { start: input.length, end: input.length } });
  const chromeWidgetNodeMapRef = useRef<Map<string, HTMLDivElement>>(new Map());
  const submissionLockRef = useRef<ComposerSubmissionLock | null>(null);
  const lastModelPickerRequestIdRef = useRef(modelPickerRequestId);
  const chromeButtonTabIndex = keyboardButtonNavigation ? undefined : -1;
  const isVoiceListening = voiceStatus === "listening";
  const profileName = profileDisplayName(selectedProfile);
  const compactSelectedProfileName = compactProfileName(profileName);
  const selectedProviderLabel = profileProviderLabel(selectedProfile);
  const selectedModelRouteLabel = modelRouteReason(selectedProfile) || selectedProviderLabel;
  const modelControlWidth = composerModelControlWidth(profileName);
  const visibleModelStatusIndicators = modelStatusIndicators.filter(Boolean);
  const levels = selectedProfile?.supports_thinking
    ? selectedProfile.thinking_levels?.length
      ? selectedProfile.thinking_levels
      : ["low", "medium", "high"]
    : [];
  const contextDegrees = Math.round(contextUsage.ratio * 360);
  const contextTitle =
    contextUsage.maxContext < 0
      ? `${contextUsage.usedTokens} tokens / unlimited · ${selectedModelRouteLabel}`
      : `${contextUsage.usedTokens} / ${contextUsage.maxContext || "unknown"} tokens · ${selectedModelRouteLabel}`;
  const templateComposerInputId = templateComposerText(composerInput?.id, 80);
  const templateComposerPlaceholder = templateComposerText(composerInput?.placeholder);
  const templateComposerHelp = templateComposerText(composerInput?.help || composerInput?.description, 220);
  const templateAcceptedModalities = useMemo(
    () => normalizedTemplateComposerList(composerInput?.accepted_modalities),
    [composerInput?.accepted_modalities],
  );
  const templateFeatureFlags = useMemo(
    () => templateComposerFeatureFlags(composerInput?.feature_flags),
    [composerInput?.feature_flags],
  );
  const templateAllowsSlashCommands = surfaceMode !== "thread" && templateFeatureFlags.slash_commands !== false;
  const templateComposerInfoItems = useMemo(() => {
    const items = [
      ...templateAcceptedModalities.map((modality) => TEMPLATE_COMPOSER_MODALITY_LABELS[modality] ?? modality),
      ...Object.entries(templateFeatureFlags)
        .filter(([key, value]) => value === true && TEMPLATE_COMPOSER_FEATURE_LABELS[key])
        .map(([key]) => TEMPLATE_COMPOSER_FEATURE_LABELS[key]),
    ];
    return [...new Set(items)].slice(0, 6);
  }, [templateAcceptedModalities, templateFeatureFlags]);
  const templateHasModalityLimit = templateAcceptedModalities.length > 0;
  const templateAllowsFileAttachments = surfaceMode !== "thread" && templateFeatureFlags.file_attachments !== false
    && templateFeatureFlags.attachments !== false
    && (!templateHasModalityLimit || templateAcceptedModalities.some((item) => (
      item === "file" || item === "files" || item === "image" || item === "images" || item === "audio" || item === "video"
    )));
  const templateAllowsVoiceInput = surfaceMode !== "thread" && templateFeatureFlags.voice_input !== false
    && templateFeatureFlags.voice !== false
    && (!templateHasModalityLimit || templateAcceptedModalities.some((item) => (
      item === "voice" || item === "speech" || item === "audio"
    )));
  const templateAllowsAtMentions = surfaceMode !== "thread" && templateFeatureFlags.at_mentions !== false
    && templateFeatureFlags.mentions !== false;
	  const toolItems = useMemo(() => [...inlineExtensions, ...belowExtensions], [inlineExtensions, belowExtensions]);
  const mentionSkills = useMemo(
    () => composerMentionSkills(skillExtensions),
    [skillExtensions],
  );
  const resolvedModelSelectorSchema = useMemo(
    () => modelSelectorSchemaForSurface(modelSelectorSchema, "composer"),
    [modelSelectorSchema],
  );
	  const selectableProfiles = useMemo(
    () => filterModelProfilesBySelector(
      modelProfiles.length > 0 ? modelProfiles : favoriteProfiles,
      resolvedModelSelectorSchema,
      "composer",
    ),
    [favoriteProfiles, modelProfiles, resolvedModelSelectorSchema],
  );
	  const selectedToolIdSet = useMemo(() => new Set(selectedToolIds), [selectedToolIds]);
  const visibleDroppedWidgets = useMemo(
    () => droppedWidgets
      .filter((widget) => widget.metadata?.source !== "composer_at_mention"
        && widget.type !== "tool" && widget.type !== "service"
        && widget.widgetKind !== "tool_toggle" && widget.widgetKind !== "service_reference")
      .map((widget) => widgetWithCurrentPresentation(widget, toolItems)),
    [droppedWidgets, toolItems],
  );
  const inlineMentionParts = useMemo(
    () => composerInlineMentionParts(input, droppedWidgets),
    [droppedWidgets, input],
  );
  const hasInlineMentions = inlineMentionParts.some((part) => part.mention);
  const syncInlineMentionScroll = useCallback((textarea: HTMLTextAreaElement) => {
    if (!inlineMentionLayerRef.current) return;
    inlineMentionLayerRef.current.scrollTop = textarea.scrollTop;
    inlineMentionLayerRef.current.scrollLeft = textarea.scrollLeft;
  }, []);
  const toolGroups = useMemo(() => groupToolItems(toolItems), [toolItems]);
  const mentionToolGroups = useMemo(() => composerToolMentionGroups(toolItems), [toolItems]);
  const serviceLabelById = useMemo(() => new Map(toolGroups.map((group) => [group.id, group.label])), [toolGroups]);
  const labelForServiceId = useCallback((serviceId: string) => serviceLabelById.get(serviceId) ?? serviceId, [serviceLabelById]);
  const computerUseSelected = selectedToolIds.some((toolId) => (
    toolId === "computer_use"
    || toolId === "browser_computer"
    || toolId === "browser_use"
    || toolId === "browser_companion"
  ));
  const hasAttachedImages = attachedFiles.some((file) => String(file.type ?? "").startsWith("image/"));
  const imageRequiresVisionModel = hasAttachedImages && !selectedProfile?.supports_vision && !selectedProfile?.supports_image_input;
  const activeToolGroup = toolGroups.find((group) => group.id === openToolGroup) ?? toolGroups[0] ?? null;
  const showToolGroups = toolItems.length > 4;
  const isEscapedSlash = input.startsWith("//");
  const localPetInput = Boolean(onLocalCommandSubmit && isTaskPetCommandInput(input));
  const steeringControlsPending = surfaceMode !== "thread" && isGenerating && !steerControlsReady;
  const isPendingRecovery = steeringControlsPending && Boolean(pendingRecovery?.onDetach);
  const composerInputBlocked = steeringControlsPending && !isPendingRecovery;
  const isSteerMode = surfaceMode !== "thread" && isGenerating && steerControlsReady && !isNewConversation;
  const effectiveComposerPlaceholder = composerPlaceholderCopy({
    isSteerMode,
    mode,
    placeholder,
    templatePlaceholder: templateComposerPlaceholder,
  });
  const effectiveComposerHelp = !isSteerMode && (!templateComposerHelp || looksLikeInternalComposerCopy(templateComposerHelp)) ? "" : composerHelperCopy({
    isSteerMode,
    hasInput: Boolean(input.trim()),
    slashCommands: templateAllowsSlashCommands,
    atMentions: templateAllowsAtMentions,
    fileAttachments: templateAllowsFileAttachments,
    templateHelp: templateComposerHelp,
  });
  const hasSlashCommandPrefix = templateAllowsSlashCommands && input.startsWith("/") && !isEscapedSlash;
  const slashText = hasSlashCommandPrefix ? input.slice(1) : "";
  const slashCommandName = slashText.trimStart().split(/\s+/, 1)[0] ?? "";
  const slashQuery = slashCommandName.toLowerCase();
  const staticSelectMatch = hasSlashCommandPrefix ? protocolStaticSelectMatch(input, commands) : null;
  const menuCommands = composerMenuCommands(commands, templateAllowsFileAttachments, templateAllowsSlashCommands);
  const matchedCommands = hasSlashCommandPrefix || commandMenuOpen
    ? staticSelectMatch && staticSelectMatch.options.length > 0
      ? staticSelectMatch.options
          .filter((option) => !staticSelectMatch.query || `${option.value} ${option.label}`.toLocaleLowerCase().includes(staticSelectMatch.query))
          .map((option) => ({
            ...staticSelectMatch.command,
            id: `${staticSelectMatch.command.id}::${option.value}`,
            name: `${staticSelectMatch.command.name} ${option.value}`,
            label: option.label,
            description: `${staticSelectMatch.command.label}を「${option.label}」に変更`,
            protocol_source_command_id: staticSelectMatch.command.id,
            protocol_option_value: option.value,
          }))
      : menuCommands.filter((command) => {
          const haystack = `${command.id} ${command.name} ${(command.aliases ?? []).join(" ")} ${command.label} ${command.description ?? ""}`.toLowerCase();
          return !slashQuery || haystack.includes(slashQuery);
        })
    : [];
  const activeCommandArgumentGuide = commandArgumentGuideForInput(input, commands);
  const hasModelCommandCandidates = textareaFocused && modelCommandCandidates.length > 0;
  const showCommandSuggestions = shouldShowComposerCommandSuggestions({
    focused: textareaFocused || commandMenuOpen,
    slashCommandsEnabled: templateAllowsSlashCommands || commandMenuOpen,
    hasModelCandidates: hasModelCommandCandidates,
    matchCount: activeCommandArgumentGuide && !commandMenuOpen ? 0 : matchedCommands.length,
  });
  const showAtMentionSuggestions = shouldShowComposerAtMentionSuggestions({
    atMentionOpen,
    commandMenuOpen,
    commandSuggestionsOpen: showCommandSuggestions,
  });
  const persistentToggleCommands = persistentComposerToggleCommands(commands).slice(0, 3);
  const visibleSteerPreviewItems = steerPreviewItems.filter((item) => (
    item.visible !== false && String(item.prompt ?? "").trim()
  ));
  const steerError = steerStatus?.kind === "error" ? steerStatus.message : null;
  const steerSuccessStatus = steerStatus?.kind === "success" ? steerStatus.message : null;
  const currentModeMeta = MODE_META[mode];
  const ModeIcon = currentModeMeta.icon;
  const directoryEntries = (codingContext?.entries ?? []).filter((entry) => entry.is_dir);
  const branchOptions = codingContext?.branches?.length ? codingContext.branches : codingContext?.branch ? [codingContext.branch] : [];
  const currentDirectory = codingContext?.directory || ".";
  const selectedCodingWorkspace = codingWorkspaces.find((workspace) => workspace.workspace_id === (selectedCodingWorkspaceId || codingContext?.workspaceId)) ?? codingWorkspaces[0] ?? null;
  const atMentionKnownValues = useMemo(() => [
    ...entityCandidates.map((candidate) => candidate.syntax.slice(1)),
    ...composerKnownMentionValues(toolItems),
    ...composerKnownMentionValues(mentionSkills),
    ...mentionToolGroups.flatMap((service) => [service.id, service.label]),
    ...(mode === "coding" ? codingContext?.files ?? [] : []),
  ], [entityCandidates, codingContext?.files, mentionSkills, mode, mentionToolGroups, toolItems]);
  const atMentionCandidates = useMemo<ComposerAtMentionCandidate[]>(() => {
    const exclude = atMentionQuery.startsWith("-");
    const rawQuery = exclude ? atMentionQuery.slice(1) : atMentionQuery;
    const scope = rawQuery.trim().toLowerCase().match(/^(tool|skill|file|chat|group|mcp)(?::|\s|$)/)?.[1];
    const query = scope ? rawQuery.trim().replace(/^(tool|skill|file|chat|group|mcp)(?::|\s|$)/i, "").trim() : rawQuery;
    const toolCandidates = filterComposerToolMentions(toolItems, query, 32).map((item) => {
      const display = composerToolMentionDisplay(item);
      return {
        kind: "tool" as const,
        id: `tool:${item.id}`,
        label: display.label,
        description: exclude ? ["このメッセージで除外", display.description].filter(Boolean).join(" · ") : display.description,
        item,
        section: composerMentionSectionForTool(item),
      };
    });
    const toolsInSection = (sectionId: ComposerMentionSection["id"]) => toolCandidates
      .filter((candidate) => candidate.section.id === sectionId)
      .slice(0, atMentionQuery.trim() ? 8 : 6);
    const skillCandidates = filterComposerSkillMentions(mentionSkills, query, 8).map((skill) => {
      const display = composerSkillMentionDisplay(skill);
      return {
        kind: "skill" as const,
        id: `skill:${skill.id}`,
        label: display.label,
        description: display.description,
        skill,
        section: COMPOSER_MENTION_SECTIONS.skill,
      };
    });
    const normalizedServiceQuery = query.trim().toLowerCase();
    const serviceCandidates = mentionToolGroups
      .filter((service) => service.items.some((item) => !item.disabled))
      .filter((service) => (
        !normalizedServiceQuery
        || `${service.id} ${service.label} ${service.description}`
          .toLowerCase()
          .includes(normalizedServiceQuery)
      ))
      .slice(0, 8)
      .map((service) => {
        const availableItemCount = service.items.filter((item) => !item.disabled).length;
        return {
          kind: "service" as const,
          id: `service:${service.id}`,
          label: service.label,
          displayLabel: `${service.label}（${exclude ? "除外" : "まとめ"}）`,
          description: [
            service.description,
            `${availableItemCount}件のツールをまとめて${exclude ? "除外" : "選択"}`,
          ].filter(Boolean).join(" · "),
          service,
          section: composerMentionSectionForToolGroup(service.items),
        };
      });
    const fileCandidates = mode === "coding"
      ? filterAtMentionFiles(codingContext?.files ?? [], query).slice(0, 8).map((file) => ({
          kind: "file" as const,
          id: `file:${file}`,
          label: file,
          description: "workspace file",
          file,
          section: COMPOSER_MENTION_SECTIONS.file,
        }))
      : [];
    const candidates = orderComposerAtMentionCandidates([
      ...toolsInSection("plugin"),
      ...toolsInSection("builtin-tool"),
      ...toolsInSection("custom-tool"),
      ...(exclude ? [] : skillCandidates),
      ...serviceCandidates,
      ...(exclude ? [] : fileCandidates),
      ...(exclude ? [] : composerEntityMentionCandidates(entityCandidates, rawQuery)),
    ]);
    return scope ? candidates.filter((candidate) => candidate.kind === "entity" ? candidate.entity.kind === scope : candidate.kind === scope) : candidates;
  }, [entityCandidates, atMentionQuery, codingContext?.files, mentionSkills, mode, mentionToolGroups, toolItems]);

  const atMentionPalette = useMemo(() => atMentionPalettePayload(atMentionCandidates), [atMentionCandidates]);
  const commandPalette = useMemo(() => commandPalettePayload(matchedCommands), [matchedCommands]);
  const commandArgumentPalette = useMemo(
    () => activeCommandArgumentGuide
      ? commandArgumentPalettePayload(activeCommandArgumentGuide)
      : null,
    [activeCommandArgumentGuide],
  );
  const activeComposerListboxId = showAtMentionSuggestions
    ? AT_MENTION_LISTBOX_ID
    : showCommandSuggestions
      ? COMMAND_LISTBOX_ID
      : commandArgumentPalette
        ? COMMAND_ARGUMENT_LISTBOX_ID
        : undefined;
  const activeComposerOptionId = showAtMentionSuggestions && atMentionCandidates.length > 0
    ? `composer-at-mention-option-${selectedAtMentionIndex}`
    : showCommandSuggestions && matchedCommands.length > 0
      ? `composer-slash-command-option-${selectedCommandIndex}`
      : commandArgumentPalette
        ? `${commandArgumentPalette.id}-option-0`
        : undefined;

	  const needsApiKey = useCallback(
    (profile: ModelProfile | null | undefined) => (
      profileNeedsApiKey(profile) && !locallyConfiguredProviders.has(profileProviderId(profile))
    ),
    [locallyConfiguredProviders],
  );

  const updateComposerPopoverAnchor = useCallback(() => {
    if (typeof window === "undefined") return;
    const anchorRect = textareaRef.current?.getBoundingClientRect() ?? null;
    setComposerPopoverStyle(modelCandidatePopupStyleForAnchor(anchorRect, window.innerWidth));
  }, []);

  const resizeComposerTextarea = useCallback(
    (textarea: HTMLTextAreaElement | null = textareaRef.current) => {
      if (!textarea) return;
      const previousHeight = textarea.getBoundingClientRect().height;
      textareaResizeAnimation.current?.cancel();
      const animateResize = previousTextareaCollapsed.current !== textareaCollapsed;
      previousTextareaCollapsed.current = textareaCollapsed;
      textarea.style.height = "auto";
      const naturalHeight = Math.max(textarea.scrollHeight, 0);
      const minHeight = isNewConversation ? NEW_CONVERSATION_TEXTAREA_MIN_HEIGHT : CONVERSATION_TEXTAREA_MIN_HEIGHT;
      const expandedMaxHeight = isNewConversation ? NEW_CONVERSATION_TEXTAREA_MAX_HEIGHT : CONVERSATION_TEXTAREA_MAX_HEIGHT;
      const maxHeight = textareaCollapsed ? COLLAPSED_TEXTAREA_MAX_HEIGHT : expandedMaxHeight;
      setTextareaCanCollapse(naturalHeight > TEXTAREA_COLLAPSE_THRESHOLD);
      fitComposerTextareaHeight(
        textarea,
        minHeight,
        maxHeight,
      );
      if (animateResize && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        textareaResizeAnimation.current = textarea.animate(
          [{ height: `${previousHeight}px` }, { height: textarea.style.height }],
          { duration: 200, easing: "ease-out" },
        );
      }
    },
    [isNewConversation, textareaCollapsed],
  );

  const requestModelProfileSelect = useCallback(
    (profileId: string) => {
      const profile = selectableProfiles.find((item) => (
        item.profile_id === profileId
        || item.qualified_model_id === profileId
        || `${item.provider_id}/${item.model_id}` === profileId
      ));
      if (profile && needsApiKey(profile)) {
        setApiKeyPromptProfile(profile);
        setModelDropdownOpen(false);
        setMenuOpen(false);
        return;
      }
      onModelProfileSelect(profileId);
    },
    [needsApiKey, onModelProfileSelect, selectableProfiles],
  );

  const registerChromeWidgetNode = useCallback((widgetId: string, node: HTMLDivElement | null) => {
    const nodeMap = chromeWidgetNodeMapRef.current;
    if (node) nodeMap.set(widgetId, node);
    else nodeMap.delete(widgetId);
  }, []);

  const saveProviderApiKey = useCallback(
    async (providerId: string, value: string) => {
      if (!apiKeyPromptProfile) return;
      if (!onProviderApiKeySave) {
        throw new Error("この provider の API key 保存に対応していません。");
      }
      await onProviderApiKeySave(providerId, value);
      setLocallyConfiguredProviders((current) => new Set(current).add(providerId));
      const selectedId = apiKeyPromptProfile.profile_id || apiKeyPromptProfile.qualified_model_id || `${apiKeyPromptProfile.provider_id}/${apiKeyPromptProfile.model_id}`;
      setApiKeyPromptProfile(null);
      onModelProfileSelect(selectedId);
    },
    [apiKeyPromptProfile, onModelProfileSelect, onProviderApiKeySave],
  );

  useEffect(() => {
    if (!menuOpen) return;

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (menuRef.current?.contains(target) || menuButtonRef.current?.contains(target)) return;
      setMenuOpen(false);
    };

    const handleDocumentKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
      }
    };

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleDocumentKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleDocumentKeyDown);
    };
  }, [menuOpen]);

  useEffect(() => {
    if (!commandMenuOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (commandMenuRef.current?.contains(target) || commandMenuButtonRef.current?.contains(target)) return;
      setCommandMenuOpen(false);
    };
    const handleDocumentKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setCommandMenuOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleDocumentKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleDocumentKeyDown);
    };
  }, [commandMenuOpen]);

  useIsomorphicLayoutEffect(() => {
    if (!commandMenuOpen) return;
    setAtMentionOpen(false);
    setAtMentionQuery("");
    setAtMentionStart(null);
  }, [commandMenuOpen]);

  useEffect(() => {
    setSelectedCommandIndex((current) => {
      if (matchedCommands.length === 0) return 0;
      return Math.min(current, matchedCommands.length - 1);
    });
  }, [matchedCommands.length]);

  useEffect(() => {
    setSelectedAtMentionIndex((current) => {
      if (atMentionCandidates.length === 0) return 0;
      return Math.min(current, atMentionCandidates.length - 1);
    });
  }, [atMentionCandidates.length]);

  useIsomorphicLayoutEffect(() => {
    resizeComposerTextarea();
    if (!input) setTextareaCollapsed(false);
  }, [input, resizeComposerTextarea]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const handleResize = () => resizeComposerTextarea();
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [resizeComposerTextarea]);

  useEffect(() => {
    setSelectedModelCandidateIndex((current) => {
      if (modelCommandCandidates.length === 0) return 0;
      return Math.min(current, modelCommandCandidates.length - 1);
    });
    if (modelCommandCandidates.length > 0) {
      setModelDropdownOpen(false);
      setMenuOpen(false);
    }
  }, [modelCommandCandidates.length]);

  useEffect(() => {
    if (modelPickerRequestId === lastModelPickerRequestIdRef.current) return;
    lastModelPickerRequestIdRef.current = modelPickerRequestId;
    if (modelPickerRequestId <= 0) return;
    setMenuOpen(false);
    setModelDropdownOpen(true);
    window.setTimeout(() => textareaRef.current?.focus({ preventScroll: true }), 0);
  }, [modelPickerRequestId]);

  useEffect(() => {
    if (!suppressPopovers) return;
    setCommandMenuOpen(false);
    setMenuOpen(false);
    setAtMentionOpen(false);
    setModelDropdownOpen(false);
    setModeSelectorOpen(false);
    onModelCommandCandidatesClose?.();
  }, [onModelCommandCandidatesClose, suppressPopovers]);

  useEffect(() => {
    if (templateAllowsSlashCommands || openFolder !== "commands") return;
    setOpenFolder("tools");
  }, [openFolder, templateAllowsSlashCommands]);

  useEffect(() => {
    if (!hasModelCommandCandidates) return;
    updateComposerPopoverAnchor();
    window.addEventListener("resize", updateComposerPopoverAnchor);
    window.addEventListener("scroll", updateComposerPopoverAnchor, true);
    return () => {
      window.removeEventListener("resize", updateComposerPopoverAnchor);
      window.removeEventListener("scroll", updateComposerPopoverAnchor, true);
    };
  }, [hasModelCommandCandidates, updateComposerPopoverAnchor]);

  useEffect(() => {
    textareaRef.current?.focus({ preventScroll: true });
    const focusTimer = window.setTimeout(() => {
      textareaRef.current?.focus({ preventScroll: true });
    }, 80);
    return () => window.clearTimeout(focusTimer);
  }, []);

  useEffect(() => {
    const handleDocumentSlashFocus = (event: KeyboardEvent) => {
      if (!templateAllowsSlashCommands || !shouldFocusComposerForSlashKey(event, event.target)) return;
      event.preventDefault();
      const textarea = textareaRef.current;
      if (!textarea) return;
      if (!input.trim()) {
        onInputChange("/");
        window.setTimeout(() => {
          textarea.focus({ preventScroll: true });
          textarea.setSelectionRange(1, 1);
        }, 0);
        return;
      }
      textarea.focus({ preventScroll: true });
    };

    document.addEventListener("keydown", handleDocumentSlashFocus);
    return () => document.removeEventListener("keydown", handleDocumentSlashFocus);
  }, [input, onInputChange, templateAllowsSlashCommands]);

  const chooseCommand = (
    commandId: string,
    rawInput = input,
    intent: "execute" | "complete" = "execute",
  ) => {
    if (commandId === COMPOSER_ATTACH_COMMAND_ID || commandId === COMPOSER_ATTACH_IMAGE_COMMAND_ID) {
      if (!templateAllowsFileAttachments) return;
      setCommandMenuOpen(false);
      if (hasSlashCommandPrefix) onInputChange("");
      const inputElement = commandId === COMPOSER_ATTACH_IMAGE_COMMAND_ID
        ? imageInputRef.current
        : fileInputRef.current;
      inputElement?.click();
      return;
    }
    if (!templateAllowsSlashCommands) return;
    setCommandMenuOpen(false);
    if (commandMenuOpen && !hasSlashCommandPrefix) {
      const selected = commands.find((command) => command.id === commandId);
      rawInput = `/${selected?.name ?? commandId}`;
    }
    const protocolOption = matchedCommands.find((command) => command.id === commandId) as (
      ComposerCommandItem & { protocol_source_command_id?: string; protocol_option_value?: string }
    ) | undefined;
    if (protocolOption?.protocol_source_command_id && protocolOption.protocol_option_value) {
      const source = commands.find((command) => command.id === protocolOption.protocol_source_command_id);
      if (!source || source.availability?.status === "unavailable") return;
      onCommandSelect?.(source.id, `/${source.name} ${protocolOption.protocol_option_value}`);
      onInputChange("");
      return;
    }

    const command = commands.find((item) => item.id === commandId);
    if (command?.availability?.status === "unavailable") return;
    const argumentEntryPrefix = commandArgumentEntryPrefix(command);
    if (intent === "complete" && argumentEntryPrefix) {
      onInputChange(argumentEntryPrefix);
      window.setTimeout(() => {
        textareaRef.current?.focus({ preventScroll: true });
        textareaRef.current?.setSelectionRange(argumentEntryPrefix.length, argumentEntryPrefix.length);
      }, 0);
      return;
    }
    const action = command?.execution.type === "frontend" ? command.execution.action : "";
    const rawHasArgs = rawInput.trim().includes(" ");
    if (command?.protocol_presentation?.input.kind === "select") {
      onInputChange(`/${command.name} `);
      window.setTimeout(() => textareaRef.current?.focus({ preventScroll: true }), 0);
      return;
    }
    const nextModelPickerState = nextModelPickerOpenState(modelDropdownOpen, action, rawHasArgs);
    if (nextModelPickerState !== null) {
      setModelDropdownOpen(nextModelPickerState);
      setMenuOpen(false);
      onInputChange("");
      return;
    } else if (action === "open_tool_picker" && !rawHasArgs) {
      setOpenFolder("tools");
      setMenuOpen(true);
    } else if (action === "open_command_help") {
      setOpenFolder("commands");
      setMenuOpen(true);
    }
    const localCommandInput = command && isLocalTaskPetCommand(command) ? "/pet" : rawInput;
    if (onLocalCommandSubmit?.(localCommandInput)) return;
    onCommandSelect?.(commandId, rawInput);
    if (hasSlashCommandPrefix && !(command?.protocol_presentation?.input.kind === "search_select" && rawHasArgs)) {
      onInputChange("");
    }
  };

  const chooseModelCommandCandidate = useCallback(
    (candidate: ModelCommandCandidate | undefined) => {
      if (!candidate) return;
      onModelCommandCandidateSelect?.(candidate);
    },
    [onModelCommandCandidateSelect],
  );

  const updateAtMentionStateFromInput = useCallback(
    (value: string) => {
      const textarea = textareaRef.current;
      const textareaOwnsFocus = typeof document === "undefined" || document.activeElement === textarea;
      if (!textarea || !textareaOwnsFocus || commandMenuOpen || suppressPopovers || !templateAllowsAtMentions) {
        setAtMentionOpen(false);
        setAtMentionQuery("");
        setAtMentionStart(null);
        return;
      }
      const cursorPos = textarea.selectionStart ?? value.length;
      const activeMention = activeMentionAtCursor(value, cursorPos, atMentionKnownValues);

      if (activeMention) {
        setAtMentionOpen(true);
        setAtMentionQuery(activeMention.query);
        setAtMentionStart(activeMention.start);
      } else {
        setAtMentionOpen(false);
        setAtMentionQuery("");
        setAtMentionStart(null);
      }
    },
    [atMentionKnownValues, commandMenuOpen, suppressPopovers, templateAllowsAtMentions],
  );

  useEffect(() => {
    updateAtMentionStateFromInput(textareaRef.current?.value ?? input);
  }, [input, textareaFocused, updateAtMentionStateFromInput]);

  useIsomorphicLayoutEffect(() => {
    if (!hasModelCommandCandidates) return;
    updateComposerPopoverAnchor();
  }, [hasModelCommandCandidates, updateComposerPopoverAnchor]);

  const handleInputChange = useCallback(
    (value: string) => {
      const edit = nativeMentionEditRef.current;
      nativeMentionEditRef.current = undefined;
      const updatedWidgets = updateConfirmedComposerWidgets(input, value, droppedWidgets, edit);
      onDroppedWidgetsChange?.(updatedWidgets);
      onInputChange(value);
      onEntityReferencesChange?.(composerReferencesForSelection(value, 0, value.length, updatedWidgets, entityReferences));
      updateAtMentionStateFromInput(value);

      if (!templateAllowsSlashCommands || !value.startsWith("/") || value.startsWith("//")) {
        setSelectedCommandIndex(0);
      }
    },
    [droppedWidgets, input, onDroppedWidgetsChange, entityReferences, onEntityReferencesChange, onInputChange, templateAllowsSlashCommands, updateAtMentionStateFromInput],
  );

  const confirmEntityInsertion = useCallback(async (
    resolve: () => Promise<{ widget: DroppedWidget; reference: ComposerEntityReference; syntax: string } | null>,
    replaceMention: boolean,
  ) => {
    const textarea = textareaRef.current;
    if (!textarea || imeActiveRef.current || entityConfirmationPendingRef.current || submissionDisabled || isGenerating) return;
    const value = textarea.value;
    const selection = { start: textarea.selectionStart, end: textarea.selectionEnd };
    const captured = { ...liveReferenceDraftRef.current, value, ...selection, imeGeneration: imeGenerationRef.current };
    entityConfirmationPendingRef.current = true;
    onReferenceConfirmationPendingChange?.(true);
    setEntityConfirmationError("");
    try {
      const admitted = await resolve();
      const live = { ...liveReferenceDraftRef.current, value: textarea.value, start: textarea.selectionStart, end: textarea.selectionEnd, imeGeneration: imeGenerationRef.current };
      if (!admitted || imeActiveRef.current || textareaRef.current !== textarea || !textarea.isConnected
        || !composerReferenceInsertionIsCurrent(captured, live)) return;
      const start = replaceMention ? activeMentionAtCursor(value, selection.start, atMentionKnownValues)?.start ?? selection.start : selection.start;
      const next = replaceMention
        ? insertAtMentionText(value, selection.start, admitted.syntax.slice(1), atMentionKnownValues, selection.end)
        : insertComposerReferencePaste(value, selection.start, selection.end, { text: `${admitted.syntax} `, references: [admitted.reference] });
      const widget = anchorComposerMentionWidget(admitted.widget, next.value, start);
      if (!widget.metadata?.composer_confirmation) return;
      const end = replaceMention ? value.length - (next.value.length - next.cursor) : selection.end;
      const updated = replaceConfirmedComposerWidget(updateConfirmedComposerWidgets(value, next.value, captured.droppedWidgets, { start, end }), widget);
      if (onDroppedWidgetsChange) onDroppedWidgetsChange(updated); else onDropWidget?.(widget);
      nativeMentionEditRef.current = undefined;
      onInputChange(next.value);
      onEntityReferencesChange?.(mergeComposerReferences(captured.entityReferences, [{ ...admitted.reference, confirmedRange: { value: next.value, start, end: start + admitted.syntax.length } }], next.value));
      setAtMentionOpen(false); setAtMentionQuery(""); setAtMentionStart(null);
      setTimeout(() => {
        if (textareaRef.current !== textarea || textarea.value !== next.value || imeActiveRef.current) return;
        textarea.setSelectionRange(next.cursor, next.cursor); textarea.focus();
      }, 0);
    } catch (error) {
      setEntityConfirmationError(error instanceof Error ? error.message : "参照を確認できませんでした。");
    } finally { entityConfirmationPendingRef.current = false; onReferenceConfirmationPendingChange?.(false); }
  }, [onReferenceConfirmationPendingChange, atMentionKnownValues, submissionDisabled, isGenerating, onDroppedWidgetsChange, onDropWidget, onInputChange, onEntityReferencesChange]);

  const handleAtMentionSelect = useCallback(
    (candidate: ComposerAtMentionCandidate) => {
      if (candidate.kind === "entity") {
        if (candidate.entity.available && onEntityCandidateConfirm) void confirmEntityInsertion(() => onEntityCandidateConfirm(candidate.entity), true);
        return;
      }
      const textarea = textareaRef.current;
      if (!textarea || imeActiveRef.current) return;

      const currentInput = textarea.value;
      const cursorPos = textarea.selectionStart;
      const exclude = atMentionQuery.startsWith("-")
        && (candidate.kind === "tool" || candidate.kind === "service");
      const label = `${exclude ? "-" : ""}${candidate.label}`;
      const next = insertAtMentionText(currentInput, cursorPos, label, atMentionKnownValues, textarea.selectionEnd);
      const start = activeMentionAtCursor(currentInput, cursorPos, atMentionKnownValues)?.start ?? cursorPos;
      const updated = updateConfirmedComposerWidgets(currentInput, next.value, droppedWidgets, {
        start, end: currentInput.length - (next.value.length - next.cursor),
      });
      const widget = anchorComposerMentionWidget(composerAtMentionCandidateWidget(candidate, exclude), next.value, start);
      if (onDroppedWidgetsChange) onDroppedWidgetsChange(replaceConfirmedComposerWidget(updated, widget));
      else onDropWidget?.(widget);
      nativeMentionEditRef.current = undefined;
	      onInputChange(next.value);
	      if (candidate.kind !== "service" && !exclude) {
	        const reference: ComposerEntityReference = {
	          kind: candidate.kind,
	          id: candidate.kind === "tool" ? candidate.item.id : candidate.kind === "skill" ? candidate.skill.id : candidate.file,
	          syntax: `@${candidate.label}`,
	        };
	        onEntityReferencesChange?.(mergeComposerReferences(entityReferences, [reference], next.value));
	      }
	      if (candidate.kind === "file" && mode === "coding") {
	        onAtFileAttach?.(candidate.file);
	      }
      setAtMentionOpen(false);
      setAtMentionQuery("");
      setAtMentionStart(null);

      setTimeout(() => {
        if (textareaRef.current !== textarea || textarea.value !== next.value || imeActiveRef.current) return;
        textarea.setSelectionRange(next.cursor, next.cursor);
        textarea.focus();
      }, 0);
    },
		    [onEntityCandidateConfirm, confirmEntityInsertion, droppedWidgets, onDroppedWidgetsChange, atMentionKnownValues, atMentionQuery, atMentionStart, entityReferences, input, mode, onAtFileAttach, onDropWidget, onEntityReferencesChange, onInputChange],
		  );

  const attachFiles = useCallback(async (files: FileList | File[] | null) => {
    if (!files?.length) return;
    if (!templateAllowsFileAttachments) return;
    const prepared = prepareComposerAttachments(Array.from(files), attachedFiles);
    setAttachmentError(prepared.error);
    if (prepared.files.length === 0) return;
    const results = await Promise.allSettled(prepared.files.map(fileToAttachment));
    const newFiles = results
      .filter((result): result is PromiseFulfilledResult<AttachedFile> => result.status === "fulfilled")
      .map((result) => result.value);
    const readError = results.find((result): result is PromiseRejectedResult => result.status === "rejected")?.reason;
    if (readError) {
      const message = readError instanceof Error ? readError.message : "ファイルを読み込めませんでした。";
      setAttachmentError(prepared.error ? `${prepared.error} / ${message}` : message);
    }
    if (newFiles.length > 0) onFileAttach?.(newFiles);
  }, [attachedFiles, onFileAttach, templateAllowsFileAttachments]);

  const handleCopy = useCallback((event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const textarea = event.currentTarget;
    const value = textarea.value;
    const selectedText = value.slice(textarea.selectionStart, textarea.selectionEnd);
    const references = composerReferencesForSelection(value, textarea.selectionStart, textarea.selectionEnd, droppedWidgets, entityReferences);
    const serialized = serializeComposerReferences(selectedText, references);
    if (!serialized) return;
    event.preventDefault();
    event.clipboardData.setData("text/plain", composerReferencesAsMarkdown(selectedText, references));
    event.clipboardData.setData(COMPOSER_REFERENCE_MIME, serialized);
  }, [droppedWidgets, entityReferences, input]);

  const handlePaste = useCallback(async (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const files = composerClipboardFiles(event.clipboardData);
    if (files.length > 0) {
      event.preventDefault();
      void attachFiles(files);
      return;
    }
    const raw = event.clipboardData.getData(COMPOSER_REFERENCE_MIME);
    const catalog = {
      tools: toolItems,
      skills: mentionSkills,
      files: mode === "coding" ? codingContext?.files ?? [] : [],
      profileId: historyReferenceTargetProfileId, historyReferences, preserveConfirmationRanges: true,
      mcpReferences: entityCandidates.filter((candidate) => candidate.kind === "mcp" && candidate.available).map((candidate) => ({ kind: "mcp" as const, id: candidate.id, syntax: candidate.syntax, label: candidate.label })),
    };
    const restored = raw
      ? restoreComposerReferences(raw, catalog)
      : restoreComposerMarkdownReferences(event.clipboardData.getData("text/plain"), catalog);
    if (!restored) return;
    event.preventDefault();
    const textarea = event.currentTarget;
    const currentInput = textarea.value;
    const edit = { start: textarea.selectionStart, end: textarea.selectionEnd };
    const captured = { ...liveReferenceDraftRef.current, value: currentInput, ...edit, imeGeneration: imeGenerationRef.current };
    if (imeActiveRef.current || entityConfirmationPendingRef.current) return;
    const resolvedEntities = new Map<string, { widget: DroppedWidget; reference: ComposerEntityReference; syntax: string }>();
    const entityReferencesToRestore = restored.references.filter((reference) => ["chat", "group", "mcp"].includes(reference.kind));
    if (entityReferencesToRestore.length) {
      if (!onEntityCandidateConfirm) return;
      entityConfirmationPendingRef.current = true;
      onReferenceConfirmationPendingChange?.(true);
      try {
        for (const reference of entityReferencesToRestore) {
          const candidate = entityCandidates.find((item) => item.kind === reference.kind && item.id === reference.id && item.syntax === reference.syntax);
          if (!candidate?.available) return;
          const admitted = await onEntityCandidateConfirm(candidate);
          if (!admitted || admitted.syntax !== reference.syntax) return;
          resolvedEntities.set(`${reference.kind}:${reference.id}`, admitted);
        }
        const live = { ...liveReferenceDraftRef.current, value: textarea.value, start: textarea.selectionStart, end: textarea.selectionEnd, imeGeneration: imeGenerationRef.current };
        if (imeActiveRef.current || textareaRef.current !== textarea || !textarea.isConnected
          || !composerReferenceInsertionIsCurrent(captured, live)) return;
      } catch (error) {
        setEntityConfirmationError(error instanceof Error ? error.message : "参照を確認できませんでした。");
        return;
      } finally { entityConfirmationPendingRef.current = false; onReferenceConfirmationPendingChange?.(false); }
    }
    const next = insertComposerReferencePaste(currentInput, edit.start, edit.end, restored);
    let updatedWidgets = updateConfirmedComposerWidgets(currentInput, next.value, droppedWidgets, edit);
    nativeMentionEditRef.current = undefined;
    const admitWidget = (widget: DroppedWidget, syntax: string, reference: ComposerEntityReference) => {
      let anchored: DroppedWidget | null = null;
      if (reference.confirmedRange) {
        const candidate = anchorComposerMentionWidget(widget, next.value, edit.start + reference.confirmedRange.start);
        if (candidate.metadata?.composer_confirmation) anchored = candidate;
      }
      for (let offset = reference.confirmedRange ? -1 : restored.text.indexOf(syntax); offset >= 0; offset = restored.text.indexOf(syntax, offset + 1)) {
        const candidate = anchorComposerMentionWidget(widget, next.value, edit.start + offset);
        if (candidate.metadata?.composer_confirmation) { anchored = candidate; break; }
      }
      if (!anchored) return;
      updatedWidgets = replaceConfirmedComposerWidget(updatedWidgets, anchored);
      if (!onDroppedWidgetsChange) onDropWidget?.(anchored);
    };
    onInputChange(next.value);
    for (const reference of next.references) {
      if (reference.kind === "tool") {
        const item = toolItems.find((candidate) => candidate.id === reference.id);
        if (item) admitWidget(composerToolMentionWidget(item, reference.syntax), reference.syntax, reference);
      } else if (reference.kind === "skill") {
        const skill = mentionSkills.find((candidate) => candidate.id === reference.id);
        if (skill) admitWidget(composerSkillMentionWidget(skill, reference.syntax), reference.syntax, reference);
      } else if (["chat", "group", "mcp"].includes(reference.kind)) {
        const admitted = resolvedEntities.get(`${reference.kind}:${reference.id}`);
        if (admitted) admitWidget(admitted.widget, reference.syntax, reference);
      } else if (reference.kind === "file" && mode === "coding") {
        admitWidget(composerFileMentionWidget(reference.id, reference.syntax), reference.syntax, reference);
        onAtFileAttach?.(reference.id);
      }
    }
    onDroppedWidgetsChange?.(updatedWidgets);
    const pastedReferences = next.references.map((reference) => {
      const resolved = resolvedEntities.get(`${reference.kind}:${reference.id}`);
      return resolved ? { ...resolved.reference, confirmedRange: reference.confirmedRange } : reference;
    });
    const currentReferences = composerReferencesForSelection(next.value, 0, next.value.length, updatedWidgets, [...entityReferences, ...pastedReferences]);
    onEntityReferencesChange?.(mergeComposerReferences([], currentReferences, next.value));
    setTimeout(() => {
      if (textareaRef.current !== textarea || textarea.value !== next.value || imeActiveRef.current) return;
      textarea.setSelectionRange(next.cursor, next.cursor);
      textarea.focus();
    }, 0);
  }, [onReferenceConfirmationPendingChange, onEntityCandidateConfirm, entityCandidates, historyReferences, historyReferenceTargetProfileId, droppedWidgets, onDroppedWidgetsChange, attachFiles, codingContext?.files, entityReferences, input, mentionSkills, mode, onAtFileAttach, onDropWidget, onEntityReferencesChange, onInputChange, toolItems]);

  const requestAudioTranscript = useCallback(async (
    file: AttachedFile,
    metadata: Record<string, unknown>,
    language = "ja",
  ): Promise<string> => {
    return requestComposerAudioTranscript(file, {
      profile: selectedProfile,
      language,
      metadata,
    });
  }, [selectedProfile]);

  const transcribeAttachedAudio = useCallback(async (file: AttachedFile) => {
    const generation = attachmentTranscriptionRef.current.token();
    const transcript = await requestAudioTranscript(file, {
      action: "replace_audio_attachment_with_transcript",
      source_attachment_id: file.id,
    });
    if (!attachmentTranscriptionRef.current.isCurrent(generation)) return;
    const transcriptFile = transcriptAttachmentFromAudio(file, transcript);
    onFileRemove?.(file.id);
    onFileAttach?.([transcriptFile]);
  }, [onFileAttach, onFileRemove, requestAudioTranscript]);

  const acceptHistoryDrop = useCallback((rawPayload: string, point: { x: number; y: number }, targetId: string) => {
    const target = historyDropTargetRef.current;
    if (!target || !onHistoryReferenceDrop || !historyReferenceTargetProfileId || typeof document === "undefined"
      || !validComposerHistoryDropTarget(target, point, targetId, document.elementFromPoint(point.x, point.y))) return;
    void confirmEntityInsertion(() => onHistoryReferenceDrop(rawPayload), false);
  }, [confirmEntityInsertion, onHistoryReferenceDrop, historyReferenceTargetProfileId]);
  useEffect(() => {
    const receive = (event: Event) => {
      const detail = (event as CustomEvent<HistoryReferenceDropDetail>).detail;
      if (!detail || typeof detail.rawPayload !== "string" || !detail.point || detail.targetId !== historyDropTargetId) return;
      acceptHistoryDrop(detail.rawPayload, detail.point, detail.targetId);
    };
    window.addEventListener(HISTORY_REFERENCE_DROP_EVENT, receive);
    return () => window.removeEventListener(HISTORY_REFERENCE_DROP_EVENT, receive);
  }, [acceptHistoryDrop, historyDropTargetId]);

  const handleDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      if (event.dataTransfer.files.length > 0) {
        void attachFiles(event.dataTransfer.files);
        return;
      }

      const historyData = event.dataTransfer.getData(HISTORY_REFERENCE_DROP_MIME);
      if (historyData) {
        acceptHistoryDrop(historyData, { x: event.clientX, y: event.clientY }, historyDropTargetId);
        return;
      }

      const data = event.dataTransfer.getData("application/rumi-widget");
      if (data) {
        try {
          const widget: DroppedWidget = JSON.parse(data);
          const action = resolveComposerWidgetDrop(widget, toolItems);
          if (action.type === "drop_widget") {
            onDropWidget?.(action.widget);
          } else if (action.type === "select_model") {
            requestModelProfileSelect(action.profileId);
            setModelDropdownOpen(false);
            setMenuOpen(false);
          }
        } catch {
          // invalid drop data
        }
      }
    },
    [acceptHistoryDrop, historyDropTargetId, attachFiles, onDropWidget, requestModelProfileSelect, toolItems],
  );

  const handleDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  }, []);

  const handleSubmitWithApiKeyGuard = useCallback(
    (event: React.SyntheticEvent) => {
      event.preventDefault();
      if (entityConfirmationPendingRef.current) return;
      if (onLocalCommandSubmit?.(input)) return;
      if (voiceStatus !== "idle" && !isGenerating) return;
      if (isGenerating) {
        if (surfaceMode === "thread") { if (event.type === "click") onStopGenerating?.(); return; }
        if (!steerControlsReady) return;
        const prompt = input.trim();
        if (prompt && !steerBusy) {
          onSteerSubmit?.(prompt);
        } else if (!prompt) {
          onStopGenerating?.();
        }
        return;
      }
      if (submissionDisabled || pendingMentionAttachmentPaths.length > 0) return;
      if (needsApiKey(selectedProfile)) {
        if (selectedProfile) setApiKeyPromptProfile(selectedProfile);
        return;
      }
      const signature = composerSubmissionSignature(input, attachedFiles.map((file) => file.id));
      const now = Date.now();
      if (isDuplicateComposerSubmission(submissionLockRef.current, signature, now)) return;
      submissionLockRef.current = { signature, submittedAt: now };
      onSubmit(event);
    },
    [surfaceMode, submissionDisabled, voiceStatus, attachedFiles, input, isGenerating, needsApiKey, onLocalCommandSubmit, onStopGenerating, onSteerSubmit, onSubmit, pendingMentionAttachmentPaths.length, selectedProfile, steerBusy, steerControlsReady],
  );

  const handleSendButtonClick = useCallback(
    (event: React.MouseEvent<HTMLButtonElement>) => {
      if (!isGenerating && surfaceMode !== "scheduled") return;
      handleSubmitWithApiKeyGuard(event);
    },
    [handleSubmitWithApiKeyGuard, isGenerating, surfaceMode],
  );

  useEffect(() => {
    if (voiceStatus !== "listening") return;
    const timer = window.setInterval(() => {
      setVoiceElapsedSeconds(Math.max(0, Math.floor((performance.now() - voiceStartedAtRef.current) / 1000)));
    }, 250);
    return () => window.clearInterval(timer);
  }, [voiceStatus]);

  useEffect(() => () => {
    voiceGenerationRef.current.invalidate();
    attachmentTranscriptionRef.current.invalidate();
    voiceRecorderRef.current?.cancel();
    voiceRecorderRef.current = null;
  }, []);

  const cancelVoiceInput = useCallback(() => {
    voiceGenerationRef.current.invalidate();
    voiceRecorderRef.current?.cancel();
    voiceRecorderRef.current = null;
    setVoiceElapsedSeconds(0);
    setVoiceError("");
    setVoiceTranscript("");
    setVoiceStatus("idle");
    const selection = voiceOriginalDraftRef.current.selection;
    window.setTimeout(() => {
      textareaRef.current?.focus({ preventScroll: true });
      const restored = restoreComposerVoiceSelection(
        voiceOriginalDraftRef.current.value, textareaRef.current?.value ?? "", selection,
      );
      if (restored) textareaRef.current?.setSelectionRange(restored.start, restored.end);
    }, 0);
  }, []);

  useEffect(() => {
    const scope = JSON.stringify([voiceScopeKey, widgetContext?.activeConversationId, selectedProfile?.profile_id, isGenerating]);
    voiceGenerationRef.current.bindScope(scope);
    attachmentTranscriptionRef.current.bindScope(scope);
    voiceRecorderRef.current?.cancel();
    voiceRecorderRef.current = null;
    setVoiceElapsedSeconds(0);
    setVoiceTranscript("");
    setVoiceError("");
    setVoiceStatus("idle");
  }, [voiceScopeKey, widgetContext?.activeConversationId, selectedProfile?.profile_id, isGenerating]);

  const stopAndTranscribeVoice = useCallback(async () => {
    const recorder = voiceRecorderRef.current;
    if (!recorder) return;
    const generation = voiceGenerationRef.current.next();
    voiceRecorderRef.current = null;
    setVoiceStatus("transcribing");
    setVoiceError("");
    try {
      const recording = await recorder.stop();
      if (!voiceGenerationRef.current.isCurrent(generation)) return;
      const audioFile: AttachedFile = {
        id: `voice-${Date.now()}`,
        name: `voice-${new Date().toISOString().replace(/[:.]/g, "-")}.${recording.extension}`,
        size: recording.size,
        type: recording.mimeType,
        dataUrl: recording.dataUrl,
      };
      const transcript = await requestAudioTranscript(audioFile, {
        duration_ms: recording.durationMs,
        action: "reviewable_voice_input",
        voice_input_use_ai: voiceInputUseAi,
      }, voiceLanguage);
      if (!voiceGenerationRef.current.isCurrent(generation)) return;
      if (!transcript.trim()) {
        setVoiceError(composerVoiceErrorMessage("no-speech"));
        setVoiceStatus("error");
        return;
      }
      setVoiceTranscript(transcript.trim());
      setVoiceElapsedSeconds(0);
      setVoiceStatus("review");
    } catch (error) {
      if (!voiceGenerationRef.current.isCurrent(generation)) return;
      setVoiceError(error instanceof Error && error.message.trim()
        ? error.message
        : composerVoiceErrorMessage("unknown", typeof navigator === "undefined" ? true : navigator.onLine));
      setVoiceStatus("error");
    }
  }, [requestAudioTranscript, voiceInputUseAi, voiceLanguage]);

  const startVoiceRecording = useCallback(async () => {
    if (!voiceInputEnabled || !templateAllowsVoiceInput || isGenerating) return;
    const generation = voiceGenerationRef.current.next();
    const selectionStart = textareaRef.current?.selectionStart ?? input.length;
    const selectionEnd = textareaRef.current?.selectionEnd ?? selectionStart;
    voiceOriginalDraftRef.current = {
      value: input,
      selection: { start: selectionStart, end: selectionEnd },
    };
    setVoiceStatus("starting");
    setVoiceError("");
    setVoiceTranscript("");
    try {
      await assertComposerMicrophoneAllowed();
      if (!voiceGenerationRef.current.isCurrent(generation)) return;
      const recorder = await voiceGenerationRef.current.settle(
        generation, startPinchAudioRecorder(), (lateRecorder) => lateRecorder.cancel(),
      );
      if (!recorder) return;
      voiceRecorderRef.current = recorder;
      voiceStartedAtRef.current = performance.now();
      setVoiceElapsedSeconds(0);
      setVoiceStatus("listening");
    } catch (error) {
      if (!voiceGenerationRef.current.isCurrent(generation)) return;
      const errorName = error instanceof Error ? error.name.toLowerCase() : "";
      const code = errorName === "notallowederror" || errorName === "securityerror"
        ? "not-allowed"
        : errorName === "notfounderror" || errorName === "devicesnotfounderror"
          ? "not-found"
          : "unknown";
      setVoiceError(errorName === "composermicrophonepermissionerror"
        ? "Tobkiri のマイク利用許可が必要です。設定の Host Permissions で状態を確認してください。"
        : composerVoiceErrorMessage(code, typeof navigator === "undefined" ? true : navigator.onLine));
      setVoiceStatus("error");
    }
  }, [input, isGenerating, templateAllowsVoiceInput, voiceInputEnabled]);

  const applyVoiceTranscript = useCallback((mode: ComposerVoiceInsertMode) => {
    if (input !== voiceOriginalDraftRef.current.value) {
      setVoiceError("下書きが変更されました。音声を再確認してください。現在の下書きは保持されています。");
      setVoiceStatus("error");
      return;
    }
    const result = applyComposerVoiceTranscript(
      voiceOriginalDraftRef.current.value,
      voiceTranscript,
      mode,
      voiceOriginalDraftRef.current.selection,
    );
    onInputChange(result.value);
    setVoiceStatus("idle");
    setVoiceTranscript("");
    setVoiceError("");
    window.setTimeout(() => {
      textareaRef.current?.focus({ preventScroll: true });
      textareaRef.current?.setSelectionRange(result.cursor, result.cursor);
    }, 0);
  }, [input, onInputChange, voiceTranscript]);

  const toggleVoiceInput = useCallback(async () => {
    if (!voiceInputEnabled || !templateAllowsVoiceInput || isGenerating) return;
    if (voiceStatus === "listening") {
      await stopAndTranscribeVoice();
      return;
    }
    if (voiceStatus === "starting" || voiceStatus === "transcribing") return;
    if (voiceStatus !== "review") {
      const start = textareaRef.current?.selectionStart ?? input.length;
      voiceOriginalDraftRef.current = {
        value: input,
        selection: { start, end: textareaRef.current?.selectionEnd ?? start },
      };
    }
    setVoiceError("");
    setVoiceStatus(voiceStatus === "review" ? "review" : "consent");
  }, [input, isGenerating, stopAndTranscribeVoice, templateAllowsVoiceInput, voiceInputEnabled, voiceStatus]);

  useEffect(() => {
    if (!["starting", "listening", "transcribing"].includes(voiceStatus)) return undefined;
    const failCapture = (code: string) => {
      voiceGenerationRef.current.invalidate();
      voiceRecorderRef.current?.cancel();
      voiceRecorderRef.current = null;
      setVoiceError(composerVoiceErrorMessage(code));
      setVoiceStatus("error");
    };
    const handleVisibilityChange = () => {
      if (document.visibilityState === "hidden") failCapture("aborted");
    };
    const handleDeviceChange = () => failCapture("audio-capture");
    document.addEventListener("visibilitychange", handleVisibilityChange);
    navigator.mediaDevices?.addEventListener?.("devicechange", handleDeviceChange);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      navigator.mediaDevices?.removeEventListener?.("devicechange", handleDeviceChange);
    };
  }, [voiceStatus]);

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
        event.stopPropagation();
        return;
      }

      if (isComposerImeEvent(event, { active: imeActiveRef.current, endedAt: imeEndedAtRef.current })) return;
      if (event.key === "Enter" && !event.shiftKey && event.repeat) {
        event.preventDefault();
        return;
      }

      const currentKeyInput = textareaRef.current?.value ?? input;
      if (event.key === "Backspace" || event.key === "Delete") {
        const textarea = event.currentTarget;
        const atomicEdit = atomicComposerMentionEdit(
          currentKeyInput,
          textarea.selectionStart,
          textarea.selectionEnd,
          event.key,
          droppedWidgets,
        );
        if (atomicEdit) {
          event.preventDefault();
          nativeMentionEditRef.current = { start: atomicEdit.cursor, end: currentKeyInput.length - (atomicEdit.value.length - atomicEdit.cursor) };
          handleInputChange(atomicEdit.value);
          window.setTimeout(() => {
            textarea.setSelectionRange(atomicEdit.cursor, atomicEdit.cursor);
            textarea.focus();
          }, 0);
          return;
        }
      }
      if (
        (event.key === "Enter" || event.key === "Tab")
        && isModelPickerToggleCommand(modelDropdownOpen, currentKeyInput)
      ) {
        event.preventDefault();
        event.stopPropagation();
        setModelDropdownOpen(false);
        onInputChange("");
        return;
      }

      if (event.key === "Escape") {
        const composerOwnsEscape = showAtMentionSuggestions
          || hasSlashCommandPrefix
          || hasModelCommandCandidates
          || menuOpen
          || modelDropdownOpen
          || modeSelectorOpen
          || commandMenuOpen
          || openModelStatusId !== null;
        if (composerOwnsEscape) {
          event.preventDefault();
          event.stopPropagation();
          if (showAtMentionSuggestions) {
            // Read from the textarea as the source of truth here. A keydown can
            // arrive before the controlled `input` prop has caught up with the
            // browser's latest input event (notably immediately after typing @).
            const currentInput = textareaRef.current?.value ?? input;
            const cursorPos = textareaRef.current?.selectionStart
              ?? (atMentionStart === null ? currentInput.length : atMentionStart + atMentionQuery.length + 1);
            const next = dismissActiveAtMentionText(currentInput, cursorPos, atMentionKnownValues);
            if (next.value !== currentInput) {
              handleInputChange(next.value);
              window.setTimeout(() => textareaRef.current?.setSelectionRange(next.cursor, next.cursor), 0);
            }
          }
          setAtMentionOpen(false);
          setCommandMenuOpen(false);
          setMenuOpen(false);
          setModelDropdownOpen(false);
          setModeSelectorOpen(false);
          setOpenModelStatusId(null);
          onModelCommandCandidatesClose?.();
          if (hasSlashCommandPrefix) onInputChange("");
          return;
        }
      }

      if (showAtMentionSuggestions) {
        const action = atMentionMenuKeyAction(
          event.key,
          event.shiftKey,
          selectedAtMentionIndex,
          atMentionCandidates.length,
          { isComposing: event.nativeEvent.isComposing, keyCode: event.nativeEvent.keyCode, repeat: event.repeat },
        );
        if (action.handled) {
          event.preventDefault();
          if (action.type === "move") {
            setSelectedAtMentionIndex(action.nextIndex);
          } else if (action.type === "select") {
            handleAtMentionSelect(atMentionCandidates[action.index]);
          } else if (action.type === "close") {
            setAtMentionOpen(false);
          }
          return;
        }
      }

      const modelCandidateAction = modelCandidateMenuKeyAction(
        event.key,
        event.shiftKey,
        selectedModelCandidateIndex,
        modelCommandCandidates.length,
      );
      if (modelCandidateAction.handled) {
        event.preventDefault();
        if (modelCandidateAction.type === "move") {
          setSelectedModelCandidateIndex(modelCandidateAction.nextIndex);
        } else if (modelCandidateAction.type === "select") {
          chooseModelCommandCandidate(modelCommandCandidates[modelCandidateAction.index]);
        } else if (modelCandidateAction.type === "close") {
          onModelCommandCandidatesClose?.();
        }
        return;
      }

      if (matchedCommands.length > 0) {
        if (event.key === "ArrowDown") {
          event.preventDefault();
          setSelectedCommandIndex((current) => (current + 1) % matchedCommands.length);
          return;
        }
        if (event.key === "ArrowUp") {
          event.preventDefault();
          setSelectedCommandIndex((current) => (current - 1 + matchedCommands.length) % matchedCommands.length);
          return;
        }
        if (event.key === "Tab" || event.key === "Enter") {
          event.preventDefault();
          chooseCommand(
            matchedCommands[selectedCommandIndex]?.id ?? matchedCommands[0].id,
            currentKeyInput,
            event.key === "Tab" ? "complete" : "execute",
          );
          return;
        }
      }

      if (event.key === "Enter" && !event.shiftKey && isSteerMode) {
        event.preventDefault();
        handleSubmitWithApiKeyGuard(event);
        return;
      }

      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        handleSubmitWithApiKeyGuard(event);
      }
    },
    [
      commandMenuOpen,
      atMentionCandidates,
      atMentionKnownValues,
      showAtMentionSuggestions,
      atMentionQuery.length,
      atMentionStart,
      chooseModelCommandCandidate,
      droppedWidgets,
      hasModelCommandCandidates,
      hasSlashCommandPrefix,
      handleAtMentionSelect,
      handleInputChange,
      handleSubmitWithApiKeyGuard,
      input,
      isSteerMode,
      matchedCommands,
      menuOpen,
      modelCommandCandidates,
      modelDropdownOpen,
      modeSelectorOpen,
      onInputChange,
      onModelCommandCandidatesClose,
      openModelStatusId,
      selectedAtMentionIndex,
      selectedCommandIndex,
      selectedModelCandidateIndex,
    ],
  );

  useEffect(() => {
    if (!openModelStatusId) return;
    if (!visibleModelStatusIndicators.some((indicator) => indicator.id === openModelStatusId)) {
      setOpenModelStatusId(null);
    }
  }, [openModelStatusId, visibleModelStatusIndicators]);

  const chromeWidgets: ComposerChromeWidgetSpec[] = [
    {
      id: "structured-options",
      slot: "leading",
      homeSlot: "toolbar-leading",
      order: 10,
      visible: Array.isArray(composerInput?.fields) && composerInput.fields.length > 0 && !isSteerMode,
      width: { basis: "auto", min: "2.25rem", max: "5rem", shrink: 0 },
      className: "rumi-composer-dock-control",
      render: () => (
        <StructuredComposerPanel
          composerInput={composerInput}
          values={structuredInputValues}
          onApply={(values) => onStructuredInputChange?.(values)}
          compact
        />
      ),
    },
    {
      id: "file-attach",
      slot: "leading",
      homeSlot: "editor-leading",
      order: 20,
      visible: templateAllowsFileAttachments || templateAllowsSlashCommands,
      width: { basis: "32px", min: "32px", max: "32px" },
      className: "relative overflow-visible",
      render: () => (
        <>
          <button
            ref={commandMenuButtonRef}
            type="button"
            tabIndex={chromeButtonTabIndex}
            aria-label="添付とコマンド"
            aria-controls={COMMAND_LISTBOX_ID}
            aria-expanded={showCommandSuggestions}
            title="添付とコマンド"
            onClick={() => {
              setCommandMenuOpen(!showCommandSuggestions);
              if (showCommandSuggestions && hasSlashCommandPrefix) onInputChange("");
              setAtMentionOpen(false);
              textareaRef.current?.focus({ preventScroll: true });
            }}
            className="rumi-icon-button rumi-attachment-button text-zinc-300"
          >
            <Plus aria-hidden="true" size={18} strokeWidth={1.8} />
          </button>

        </>
      ),
    },
    {
      id: "runtime-option-states",
      slot: "leading",
      homeSlot: "toolbar-leading",
      order: 15,
      visible: manualRuntimeModeSelectionEnabled,
      width: { basis: "auto", min: "0", max: "11rem", shrink: 1 },
      render: () => (
        <span
          role="status"
          aria-label="現在の実行オプション"
          className="inline-flex h-[44px] min-h-[44px] max-w-full items-center gap-0.5 rounded-xl border border-white/[0.07] bg-white/[0.025] p-1"
        >
          <span className="relative">
            <RuntimeStateButton
              label={`実行モード: ${currentModeMeta.description}`}
              state={mode}
              tone={mode === "coding" ? "sky" : mode === "agent" ? "emerald" : "neutral"}
              onClick={() => setModeSelectorOpen((open) => !open)}
            >
              <ModeIcon aria-hidden="true" size={14} />
            </RuntimeStateButton>
            {modeSelectorOpen && (
              <ModeSelector
                mode={mode}
                onModeChange={(nextMode) => onModeChange?.(nextMode)}
                onClose={() => setModeSelectorOpen(false)}
              />
            )}
          </span>
          {thinkingLevel && (
            <RuntimeStateIcon
              label={`思考レベル: ${THINKING_LABELS[thinkingLevel] ?? thinkingLevel}`}
              state={thinkingLevel}
              tone={thinkingLevel === "xhigh" ? "rose" : thinkingLevel === "high" ? "amber" : thinkingLevel === "medium" ? "violet" : thinkingLevel === "low" ? "sky" : "neutral"}
            >
              <ThinkingLevelGlyph level={thinkingLevel} />
            </RuntimeStateIcon>
          )}
          {persistentToggleCommands.filter((command) => (
            command.active === true || command.enabled === true
          )).map((command) => {
            const active = command.active === true || command.enabled === true;
            const Icon = commandIcon(command);
            const unavailable = command.availability?.status === "unavailable";
            return (
              <button
                key={command.id}
                type="button"
                aria-label={`${command.label || command.name}: ${active ? "オン" : "オフ"}`}
                aria-pressed={active}
                disabled={unavailable}
                title={unavailable ? command.availability?.reason : undefined}
                onClick={() => chooseCommand(command.id, `/${command.name}`)}
                className="group/runtime relative rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/70 disabled:cursor-not-allowed disabled:opacity-45"
              >
                <RuntimeStateIcon
                  label={`${command.label || command.name}: ${active ? "オン" : "オフ"}`}
                  state={active ? "on" : "off"}
                  tone={active ? "sky" : "neutral"}
                  focusable={false}
                >
                  <Icon aria-hidden="true" size={14} className={active ? "drop-shadow-[0_0_5px_rgba(125,211,252,0.45)]" : ""} />
                  <span className={`absolute -bottom-0.5 -right-0.5 flex h-3.5 w-3.5 items-center justify-center rounded-full border border-[#17181d] ${active ? "bg-sky-300 text-sky-950" : "bg-zinc-700 text-zinc-300"}`}>
                    {active ? <Check aria-hidden="true" size={9} strokeWidth={3} /> : <X aria-hidden="true" size={8} strokeWidth={2.6} />}
                  </span>
                </RuntimeStateIcon>
              </button>
            );
          })}
        </span>
      ),
    },
    {
      id: "voice-input",
      slot: "leading",
      homeSlot: "toolbar-trailing",
      order: 30,
      visible: templateAllowsVoiceInput,
      mobile: "hide",
      width: COMPOSER_CHROME_WIDTHS.icon,
      render: () => (
	        <button
	          type="button"
	          tabIndex={chromeButtonTabIndex}
	          aria-label={isVoiceListening ? "Stop voice input and review transcript" : voiceStatus === "review" ? "Review captured voice transcript" : "Start reviewable voice input"}
	          aria-pressed={isVoiceListening}
	          aria-expanded={voiceStatus !== "idle"}
	          aria-controls="composer-voice-panel"
		          disabled={!voiceInputEnabled || !templateAllowsVoiceInput || isGenerating || voiceStatus === "starting" || voiceStatus === "transcribing"}
		          title={isVoiceListening ? "Stop and review voice input" : "Reviewable voice input"}
		          onClick={() => void toggleVoiceInput()}
	          className={isVoiceListening ? "rumi-icon-button is-live" : "rumi-icon-button"}
	        >
          <WarmActionIcon kind="mic" size="md" />
        </button>
      ),
    },
    {
      id: "mode",
      slot: "leading",
      homeSlot: "toolbar-leading",
      order: 40,
      visible: false,
      width: COMPOSER_CHROME_WIDTHS.mode,
      render: () => (
        <div className="group/mode relative flex min-w-0 max-w-full">
          <button
            type="button"
            tabIndex={chromeButtonTabIndex}
            aria-label={`モード: ${currentModeMeta.label}`}
            disabled={isGenerating}
            title={`モード: ${currentModeMeta.label}`}
            onClick={() => setModeSelectorOpen((v) => !v)}
            className={`h-8 flex min-w-0 flex-shrink-0 items-center gap-1.5 rounded-lg px-2.5 transition-colors disabled:opacity-50 ${
              mode === "coding"
                ? "text-emerald-400 bg-emerald-500/10 hover:bg-emerald-500/20 border border-emerald-500/30"
                : mode === "agent"
                  ? "text-violet-400 bg-violet-500/10 hover:bg-violet-500/20 border border-violet-500/30"
                  : "text-zinc-400 hover:text-zinc-100 hover:bg-zinc-700/60"
            }`}
          >
            <ModeIcon size={14} className="flex-shrink-0" />
            <span className="truncate text-[11px] font-medium max-[640px]:hidden">{currentModeMeta.label}</span>
          </button>
          {mode !== "chat" && (
            <button
              type="button"
              tabIndex={chromeButtonTabIndex}
              aria-label="モードを閉じる"
              title="Chat に戻す"
              onClick={(event) => {
                event.stopPropagation();
                setModeSelectorOpen(false);
                onModeChange?.("chat");
              }}
              className="absolute -right-1 -top-1 hidden h-4 w-4 items-center justify-center rounded-full border border-zinc-700 bg-zinc-900 text-zinc-400 shadow-sm hover:bg-zinc-800 hover:text-zinc-100 group-hover/mode:flex"
            >
              <X size={10} />
            </button>
          )}
          {modeSelectorOpen && (
            <ModeSelector
              mode={mode}
              onModeChange={(m) => onModeChange?.(m)}
              onClose={() => setModeSelectorOpen(false)}
            />
          )}
        </div>
      ),
    },
    {
      id: "action-approval-control",
      slot: "leading",
      homeSlot: "toolbar-leading",
      order: 50,
      visible: showActionApprovalControl,
      width: { basis: "auto", min: "4rem", max: "8.5rem", shrink: 1 },
      className: "rumi-composer-dock-control",
      render: () => (
        <ActionApprovalControl
          mode={actionApprovalMode}
          availableModes={actionApprovalModes}
          disabled={isGenerating || !onActionApprovalModeChange}
          disabledReason={!onActionApprovalModeChange ? "この会話の承認は設定された権限に従います。ここで代理承認やフルアクセスに変更する機能は未対応です。" : undefined}
          surfaceClassName={COMPOSER_CONTROL_SURFACE_CLASSNAME}
          tabIndex={chromeButtonTabIndex}
          onModeChange={(nextMode) => onActionApprovalModeChange?.(nextMode)}
          onOpenSettings={onOpenToolSettings}
        />
      ),
    },
    {
      id: "tool-selection-control",
      slot: "leading",
      homeSlot: "toolbar-leading",
      order: 52,
      visible: showToolSelectionControl && Boolean(onToolSelectionModeChange),
      width: { basis: "auto", min: "4rem", max: "8.5rem", shrink: 1 },
      className: "rumi-composer-dock-control",
      render: () => (
        <ToolModeControl
          mode={toolSelectionMode}
          availableModes={["auto", "manual", "none"]}
          manualCount={selectedToolIds.length}
          disabled={isGenerating}
          surfaceClassName={COMPOSER_CONTROL_SURFACE_CLASSNAME}
          tabIndex={chromeButtonTabIndex}
          onModeChange={(nextMode) => onToolSelectionModeChange?.(nextMode)}
          onOpenPicker={onOpenToolSettings}
        />
      ),
    },
    {
      id: "project-picker",
      slot: "leading",
      homeSlot: "toolbar-leading",
      order: 55,
      width: { basis: "auto", min: "5.5rem", max: "13rem", shrink: 1 },
      className: "rumi-composer-dock-control overflow-visible",
      render: () => (
        <ProjectPicker
          projects={projects}
          profileId={projectProfileId}
          selectedProjectId={selectedProjectId}
          disabled={isGenerating}
          codingWorkspaces={codingWorkspaces}
          onSelect={(project) => onProjectSelect?.(project)}
          onDirectorySelect={onProjectDirectorySelect}
          onCodingWorkspaceCreate={onProjectWorkspaceCreate}
          onProjectStoragePrepare={onProjectStoragePrepare}
        />
      ),
    },
    {
      id: "computer-use-status",
      slot: "leading",
      homeSlot: "toolbar-leading",
      order: 60,
      visible: computerUseSelected,
      width: COMPOSER_CHROME_WIDTHS.badge,
      className: "overflow-hidden",
      render: () => (
        <span aria-label="PC操作" title="PC操作" className="inline-flex items-center gap-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-300 max-[430px]:px-1.5">
          <MousePointerClick size={12} className="flex-shrink-0" />
          <span className="truncate max-[430px]:hidden">PC操作</span>
        </span>
      ),
    },
    {
      id: "vision-model-required-status",
      slot: "leading",
      homeSlot: "toolbar-leading",
      order: 70,
      visible: imageRequiresVisionModel,
      width: COMPOSER_CHROME_WIDTHS.badge,
      className: "overflow-hidden",
      render: () => (
        <span aria-label="画像対応モデルが必要" title="画像対応モデルが必要" className="inline-flex items-center gap-1 rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-300 max-[430px]:px-1.5">
          <CircleAlert size={12} className="flex-shrink-0" />
          <span className="truncate max-[430px]:hidden">画像対応モデルが必要</span>
        </span>
      ),
    },
    {
      id: "model-picker",
      slot: "trailing",
      homeSlot: "toolbar-trailing",
      order: 10,
      mobile: "hide",
      width: modelControlWidth,
      className: "rumi-composer-dock-control",
      render: () => (
        <div className={`${COMPOSER_CONTROL_SURFACE_CLASSNAME} rumi-model-control w-full gap-2`}>
          <div
            title={contextTitle}
            className="h-3.5 w-3.5 flex-shrink-0 rounded-full p-[2px]"
            style={{
              background: `conic-gradient(#a1a1aa ${contextDegrees}deg, #52525b ${contextDegrees}deg)`,
            }}
          >
            <div className="h-full w-full rounded-full bg-zinc-800" />
          </div>
          <div className="relative h-full min-w-0 max-w-full flex-1">
            <button
              type="button"
              tabIndex={chromeButtonTabIndex}
              ref={modelDropdownTriggerRef}
              aria-label={`モデル: ${profileName}`}
              disabled={isGenerating}
              onClick={() => setModelDropdownOpen((v) => !v)}
              className="flex h-[44px] min-h-[44px] w-full min-w-0 items-center gap-1 text-[12px] font-medium text-zinc-300 hover:text-zinc-100 transition-colors disabled:opacity-50"
            >
              <span className="min-w-0 flex-1 truncate" title={profileName}>モデル: {compactSelectedProfileName}</span>
              <ChevronDown size={12} className={`flex-shrink-0 transition-transform ${modelDropdownOpen ? "rotate-180" : ""}`} />
            </button>
            {modelDropdownOpen && (
              <ModelDropdown
                anchorRef={modelDropdownTriggerRef}
                profiles={selectableProfiles}
                onOpenModelManager={onOpenModelManager}
                selectedProfile={selectedProfile}
                isGenerating={isGenerating}
                placement={resolvedModelSelectorSchema.layout.placement === "auto"
                  ? (isNewConversation ? "below" : "above")
                  : resolvedModelSelectorSchema.layout.placement}
                selectorSchema={resolvedModelSelectorSchema}
                onSelect={(profile) => {
                  requestModelProfileSelect(profile.profile_id);
                  setModelDropdownOpen(false);
                }}
                onClose={() => setModelDropdownOpen(false)}
              />
            )}
          </div>
        </div>
      ),
    },
    {
      id: "thinking-control",
      slot: "trailing",
      homeSlot: "toolbar-trailing",
      order: 20,
      visible: levels.length > 0,
      mobile: "hide",
      width: COMPOSER_CHROME_WIDTHS.thinking,
      className: "rumi-composer-dock-control",
      render: () => (
        <label className={`${COMPOSER_CONTROL_SURFACE_CLASSNAME} cursor-pointer justify-between gap-1.5 text-[11px] font-medium text-zinc-500`}>
          <select
            value={thinkingLevel ?? levels[0]}
            onChange={(event) => onThinkingLevelChange(event.target.value)}
            disabled={isGenerating}
            tabIndex={chromeButtonTabIndex}
            className="h-full w-full cursor-pointer appearance-none bg-transparent text-right text-[11px] font-medium text-zinc-300 outline-none transition-colors hover:text-zinc-100 disabled:opacity-50"
            aria-label="Thinking level"
            title="Thinking level"
          >
            {levels.map((level) => (
              <option key={level} value={level} className="bg-zinc-900 text-zinc-100">
                {THINKING_LABELS[level] ?? level}
              </option>
            ))}
          </select>
          <ChevronDown size={12} className="pointer-events-none flex-shrink-0 text-zinc-500" />
        </label>
      ),
    },
    {
      id: "model-status",
      slot: "trailing",
      homeSlot: "toolbar-trailing",
      order: 30,
      visible: visibleModelStatusIndicators.length > 0,
      mobile: "hide",
      width: COMPOSER_CHROME_WIDTHS.status,
      className: "rumi-composer-dock-control",
      render: () => (
        <div className={`${COMPOSER_CONTROL_SURFACE_CLASSNAME} justify-center px-2`}>
          <div className="flex items-center gap-1">
            {visibleModelStatusIndicators.map((indicator) => (
              <ModelStatusIndicatorButton
                key={indicator.id}
                indicator={indicator}
                open={openModelStatusId === indicator.id}
                onToggle={() => setOpenModelStatusId((current) => current === indicator.id ? null : indicator.id)}
                onClose={() => setOpenModelStatusId(null)}
              />
            ))}
          </div>
        </div>
      ),
    },
    {
      id: "send",
      slot: "trailing",
      homeSlot: "toolbar-trailing",
      order: 40,
      width: isNewConversation ? COMPOSER_CHROME_WIDTHS.sendLarge : COMPOSER_CHROME_WIDTHS.send,
      render: () => (
        <button
          type={isGenerating || surfaceMode === "scheduled" ? "button" : "submit"}
          onClick={handleSendButtonClick}
          tabIndex={chromeButtonTabIndex}
          aria-label={surfaceMode === "scheduled" ? "Agentタスクを保存" : localPetInput ? "/pet を実行" : isGenerating
            ? (steeringControlsPending
              ? (isPendingRecovery ? "送信結果が未確認" : "会話を準備中")
              : (surfaceMode !== "thread" && input.trim()) ? "追加指示を送る" : "生成を停止")
            : pendingMentionAttachmentPaths.length > 0
              ? "ファイルを読み込み中"
              : "メッセージを送信"}
          disabled={!localPetInput && (steeringControlsPending || (isGenerating ? surfaceMode === "thread" && !onStopGenerating : (
            submissionDisabled || pendingMentionAttachmentPaths.length > 0
            || (!input.trim() && attachedFiles.length === 0)
          )))}
          title={surfaceMode === "scheduled" ? "Agentタスクを保存" : localPetInput ? "ペットを表示" : isGenerating
            ? (steeringControlsPending
              ? (isPendingRecovery ? "送信結果が未確認" : "会話を準備中")
              : (surfaceMode !== "thread" && input.trim()) ? "追加指示を送る" : "停止")
            : pendingMentionAttachmentPaths.length > 0
              ? "ファイルを読み込み中"
              : "送信"}
          className={`rumi-send-button flex flex-shrink-0 items-center justify-center rounded-full transition-all duration-150 disabled:cursor-not-allowed ${
            "h-8 min-h-8 w-8 min-w-8"
          } ${
            isGenerating && !steeringControlsPending
              ? (surfaceMode !== "thread" && input.trim())
                ? "bg-zinc-100 text-zinc-950 hover:bg-white"
                : "bg-zinc-100 text-zinc-900 hover:bg-white"
              : pendingMentionAttachmentPaths.length > 0 || (!input.trim() && attachedFiles.length === 0)
                ? "bg-white/[0.06] text-zinc-500"
                : "bg-zinc-100 text-zinc-950 shadow-[0_6px_18px_rgba(0,0,0,0.28)] hover:bg-white"
          }`}
        >
          {localPetInput ? <SendButtonIcon size={18} /> : steeringControlsPending ? (
            isPendingRecovery
              ? <Clock3 size={14} aria-hidden="true" />
              : <Loader2 size={14} className="animate-spin" aria-hidden="true" />
          ) : isGenerating && !(surfaceMode !== "thread" && input.trim()) ? (
            <Square size={11} strokeWidth={2.4} fill="currentColor" aria-hidden="true" />
          ) : isGenerating ? (
            <CornerDownRight size={15} strokeWidth={2.4} />
          ) : (
            <SendButtonIcon size={18} />
          )}
        </button>
      ),
    },
  ];

  const availableChromeWidgets = surfaceMode === "thread"
    ? chromeWidgets.filter((widget) => widget.id === "send")
    : surfaceMode === "scheduled"
      ? chromeWidgets.filter((widget) => ["model-picker", "tool-selection-control", "action-approval-control", "send"].includes(widget.id))
      : chromeWidgets;
  const conversationFileAttachWidget = availableChromeWidgets.find((widget) => widget.id === "file-attach" && widget.visible !== false);
  const leadingChromeWidgets = composerChromeWidgetsForSlot(availableChromeWidgets, "leading")
    .filter((widget) => widget.id !== "file-attach");
  const trailingChromeWidgets = composerChromeWidgetsForSlot(availableChromeWidgets, "trailing");
  const newConversationInlineLeadingWidgets = composerChromeWidgetsForHomeSlot(availableChromeWidgets, "editor-leading");
  const newConversationTopRightWidgets = composerChromeWidgetsForHomeSlot(availableChromeWidgets, "editor-trailing");
  const newConversationInlineActionWidgets = composerChromeWidgetsForHomeSlot(availableChromeWidgets, "toolbar-leading");
  const newConversationTrailingWidgets = composerChromeWidgetsForHomeSlot(availableChromeWidgets, "toolbar-trailing");
  const menuFolders = templateAllowsSlashCommands
    ? ([
        ["tools", "Tools", Wrench],
        ["models", "Models", SlidersHorizontal],
        ["commands", "Commands", Folder],
      ] as const)
    : ([
        ["tools", "Tools", Wrench],
        ["models", "Models", SlidersHorizontal],
      ] as const);

  const ComposerFrame = surfaceMode === "scheduled" ? "div" : "form";
  return (
    <div
      id={historyDropTargetId}
      ref={historyDropTargetRef}
      data-history-reference-drop-target="composer"
      className={`${surfaceMode === "scheduled" ? "rumi-calendar-agent-editor w-full min-w-0" : isNewConversation ? "w-full px-4" : "rumi-composer-dock px-4 pb-3 pt-2 bg-[#09090b] flex-shrink-0 max-[640px]:px-2 max-[640px]:pb-2"}`}
      onDrop={handleDrop}
      onDragOver={handleDragOver}
    >
      <div className={surfaceMode === "scheduled" ? "w-full min-w-0" : `rumi-composer-shell ${isNewConversation ? "rumi-composer-shell-new mx-auto" : "mx-auto"}`}>
        <RuntimeCapabilityBanner
          visible={imageRequiresVisionModel}
          onSwitchToVisionModel={onSwitchToVisionModel}
          onOpenModelManager={onOpenModelManager}
          onOpenToolSettings={onOpenToolSettings}
        />
        <ComposerFrame
          onSubmit={handleSubmitWithApiKeyGuard}
          className={`rumi-composer-frame ${
            isNewConversation
              ? "rumi-composer-new border-transparent bg-transparent"
              : "rounded-2xl max-[640px]:rounded-2xl"
          } relative flex flex-col border overflow-visible`}
        >
          {entityConfirmationError && !showAtMentionSuggestions && <ErrorNotice message={entityConfirmationError} title="参照を確認できませんでした" />}
          {apiKeyPromptProfile && (
            <ProviderApiKeyPrompt
              profile={apiKeyPromptProfile}
              onCancel={() => setApiKeyPromptProfile(null)}
              onSave={saveProviderApiKey}
            />
          )}
          {hasModelCommandCandidates && (
            <ModelCommandCandidatePopup
              candidates={modelCommandCandidates}
              activeIndex={selectedModelCandidateIndex}
              onActiveIndexChange={setSelectedModelCandidateIndex}
              onSelect={chooseModelCommandCandidate}
              onClose={onModelCommandCandidatesClose}
              style={composerPopoverStyle}
            />
          )}
          {showCommandSuggestions && (
            <div ref={commandMenuRef}>
            <JsonListPanel
              payload={commandPalette}
              activeIndex={selectedCommandIndex}
              onActiveIndexChange={setSelectedCommandIndex}
              onSelect={(index) => chooseCommand(matchedCommands[index].id)}
            />
            </div>
          )}

          {commandArgumentPalette && (
            <JsonListPanel
              payload={commandArgumentPalette}
              activeIndex={0}
              onActiveIndexChange={() => undefined}
              onSelect={() => textareaRef.current?.focus({ preventScroll: true })}
            />
          )}

          {showAtMentionSuggestions && (
            <>
              <button
                type="button"
                tabIndex={-1}
                aria-label="close mention menu"
                className="fixed inset-0 rumi-layer-local-popover cursor-default"
                onClick={() => setAtMentionOpen(false)}
              />
              <JsonListPanel
                payload={atMentionPalette}
              activeIndex={selectedAtMentionIndex}
              onActiveIndexChange={setSelectedAtMentionIndex}
                onSelect={(index) => handleAtMentionSelect(atMentionCandidates[index])}
                footer={(entityCandidateStatus || entityConfirmationError || entityCandidatesHaveMore) ? <>
                  {(entityCandidateStatus || entityConfirmationError) && <p role="status" className="px-3 py-1 text-xs text-zinc-400">{entityConfirmationError || entityCandidateStatus}</p>}
                  {entityCandidatesHaveMore && entityCandidatesLoadMore && <button type="button" onClick={entityCandidatesLoadMore} className="px-3 py-2 text-xs text-sky-300">さらに参照を表示</button>}
                </> : undefined}
              />
            </>
          )}

          {menuOpen && (
            <>
                      <button
                        type="button"
                        aria-label="close composer menu"
                        tabIndex={chromeButtonTabIndex}
                        className="fixed inset-0 rumi-layer-local-popover cursor-default"
                        onClick={() => setMenuOpen(false)}
                      />
              <div ref={menuRef} className="absolute bottom-full left-4 rumi-layer-global-overlay mb-2 grid w-[min(480px,calc(100vw-32px))] grid-cols-[120px_minmax(0,1fr)] overflow-hidden rumi-popover max-[640px]:left-2 max-[640px]:grid-cols-1">
                <div className="border-r border-white/[0.06] bg-black/25 p-1.5 max-[640px]:flex max-[640px]:border-b max-[640px]:border-r-0">
                  {menuFolders.map(([id, label, Icon]) => (
                            <button
                              key={id}
                              type="button"
                              tabIndex={chromeButtonTabIndex}
                              onClick={() => setOpenFolder(id)}
                      className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-xs transition-colors ${
                        openFolder === id
                          ? "bg-white/[0.08] text-zinc-100"
                          : "text-zinc-400 hover:bg-white/[0.05] hover:text-zinc-200"
                      }`}
                    >
                      <Icon size={13} />
                      <span>{label}</span>
                    </button>
                  ))}
                </div>
                <div className="max-h-72 overflow-y-auto p-2">
                  {openFolder === "tools" && !showToolGroups && (
                    <ToolItemList
                      items={toolItems}
                      onSelect={(item) => {
                        onExtensionSelect?.(item);
                        setMenuOpen(false);
                      }}
                    />
                  )}
                  {openFolder === "tools" && showToolGroups && (
                    <div className="grid grid-cols-[130px_minmax(0,1fr)] gap-2 max-[640px]:grid-cols-1">
                      <div className="grid content-start gap-0.5">
                        {toolGroups.map((group) => (
                                  <button
                                    key={group.id}
                                    type="button"
                                    tabIndex={chromeButtonTabIndex}
                                    onClick={() => setOpenToolGroup(group.id)}
                            className={`rounded-lg px-2.5 py-1.5 text-left transition-colors ${
                              activeToolGroup?.id === group.id
                                ? "bg-white/[0.08] text-zinc-100"
                                : "text-zinc-400 hover:bg-white/[0.05] hover:text-zinc-200"
                            }`}
                          >
                            <span className="block truncate text-[13px]">{group.label}</span>
                            <span className="block truncate text-[10px] text-zinc-500">
                              {group.path?.length && group.path.length > 1 ? group.path.join(" / ") : `${group.items.length} tools`}
                            </span>
                          </button>
                        ))}
                      </div>
                      <div className="min-w-0">
                        {activeToolGroup && (
                          <>
                            <div className="mb-1 px-2 text-[10px] text-zinc-500">
                              {activeToolGroup.path?.length && activeToolGroup.path.length > 1
                                ? activeToolGroup.path.join(" / ")
                                : activeToolGroup.description}
                            </div>
                            <ToolItemList
                              items={activeToolGroup.items}
                              onSelect={(item) => {
                                onExtensionSelect?.(item);
                                setMenuOpen(false);
                              }}
                            />
                          </>
                        )}
                      </div>
                    </div>
                  )}
                  {openFolder === "models" && (
                    <div className="grid gap-0.5">
                      {selectableProfiles.map((profile) => {
                        const needsKey = needsApiKey(profile);
                        const badges = capabilityBadges(profile).slice(0, 3);
                        return (
                                  <button
                                    key={profile.profile_id}
                                    type="button"
                                    tabIndex={chromeButtonTabIndex}
                                    draggable
                            onDragStart={(event) => {
                              event.dataTransfer.setData(
                                "application/rumi-widget",
                                JSON.stringify({ id: profile.profile_id, type: "model", label: profile.display_name }),
                              );
                              event.dataTransfer.effectAllowed = "copy";
                            }}
                            onClick={() => {
                              requestModelProfileSelect(profile.profile_id);
                              setMenuOpen(false);
                            }}
                            className="flex items-center justify-between gap-2 rounded-lg px-3 py-1.5 text-left hover:bg-white/[0.06] transition-colors"
                          >
                            <span className="min-w-0">
                              <span className="block truncate text-[13px] text-zinc-200">
                                {compactProfileName(profileDisplayName(profile))}
                              </span>
                              <span className="block truncate text-[10px] text-zinc-500">
                                {profile.provider_display_name ?? profile.provider_id} · {profile.provider_id} · {profile.max_context_tokens ?? profile.max_context ?? "?"} ctx
                              </span>
                              {badges.length > 0 && (
                                <span className="mt-1 flex flex-wrap gap-1">
                                  {badges.map((badge) => (
                                    <span key={badge} className="rounded border border-zinc-700 px-1 py-0.5 text-[9px] leading-none text-zinc-400">
                                      {badge}
                                    </span>
                                  ))}
                                </span>
                              )}
                            </span>
                            {needsKey && (
                              <span className="flex-shrink-0 rounded-full border border-amber-500/30 px-2 py-0.5 text-[10px] text-amber-300">
                                API key
                              </span>
                            )}
                          </button>
                        );
                      })}
                    </div>
                  )}
                  {templateAllowsSlashCommands && openFolder === "commands" && (
                    <div className="grid gap-0.5">
                      {commands.map((command) => (
                                <button
                                  key={command.id}
                                  type="button"
                                  tabIndex={chromeButtonTabIndex}
                                  disabled={command.availability?.status === "unavailable"}
                                  title={command.availability?.reason}
                                  onClick={() => {
                                    chooseCommand(command.id);
                                    setMenuOpen(false);
                                  }}
                          className="flex items-center justify-between gap-3 rounded-lg px-3 py-1.5 text-left transition-colors hover:bg-white/[0.06] disabled:cursor-not-allowed disabled:opacity-45"
                        >
                          <span className="flex min-w-0 items-center gap-2.5">
                            <span className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-lg border border-white/[0.07] bg-white/[0.04] text-zinc-300">
                              <ComposerCommandIcon command={command} />
                            </span>
                            <span className="min-w-0">
                              <span className="block truncate text-[13px] text-zinc-200">/{command.name ?? command.id}</span>
                              {command.description && (
                                <span className="block truncate text-[10px] text-zinc-500">{command.description}</span>
                              )}
                            </span>
                          </span>
                          <span className="flex flex-shrink-0 items-center gap-1">
                            {command.visibility === "advanced" && (
                              <span className="rounded-full border border-zinc-700 px-2 py-0.5 text-[10px] text-zinc-500">advanced</span>
                            )}
                            {commandStateLabel(command) && (
                              <span className={`rounded-full border px-2 py-0.5 text-[10px] ${commandStateLabel(command) === "オン" ? "border-sky-400/25 bg-sky-400/[0.08] text-sky-200" : "border-white/[0.08] bg-white/[0.03] text-zinc-500"}`}>
                                {commandStateLabel(command)}
                              </span>
                            )}
                          </span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </>
          )}

          {voiceStatus !== "idle" && (
            <section
              id="composer-voice-panel"
              aria-labelledby="composer-voice-heading"
              className="rumi-voice-capture mx-3 mt-2 rounded-xl border border-white/[0.09] bg-white/[0.035] px-3 py-3"
            >
              <div className="flex items-start gap-3">
                <span className={`flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full ${voiceStatus === "error" ? "bg-rose-500/10 text-rose-300" : "bg-white/[0.06] text-zinc-100"}`}>
                  {voiceStatus === "error" ? <CircleAlert size={15} aria-hidden="true" /> : voiceStatus === "starting" || voiceStatus === "transcribing" ? <Loader2 size={15} className="animate-spin" /> : <WarmActionIcon kind="mic" size="sm" />}
                </span>
                <div className="min-w-0 flex-1">
                  <h3 id="composer-voice-heading" className="text-sm font-medium text-zinc-100">
                    {voiceStatus === "consent" ? "Reviewable voice input" : voiceStatus === "listening" ? "Recording voice input" : voiceStatus === "review" ? "Review transcript" : voiceStatus === "error" ? "Voice input unavailable" : voiceStatus === "transcribing" ? "Creating transcript" : "Preparing microphone"}
                  </h3>
                  <p className="mt-1 text-xs leading-5 text-zinc-400">
                    Microphone use requires Tobkiri permission and OS permission. Audio is recorded in memory and sent to your configured Tobkiri transcription route. It is never inserted into the draft automatically; the configured provider may process it under that route&apos;s settings.
                  </p>
                </div>
                <button type="button" onClick={cancelVoiceInput} aria-label="Close voice input" title="Close" className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full text-zinc-500 transition-colors hover:bg-white/[0.06] hover:text-zinc-100">
                  <X size={14} />
                </button>
              </div>

              {voiceStatus === "consent" && (
                <div className="mt-3 flex flex-wrap items-end gap-2">
                  <label className="min-w-44 flex-1 text-xs text-zinc-300">
                    Transcription language
                    <select value={voiceLanguage} onChange={(event) => setVoiceLanguage(event.currentTarget.value)} className="mt-1 block h-9 w-full rounded-lg border border-white/10 bg-zinc-900 px-2 text-sm text-zinc-100 outline-none focus:border-sky-400/50">
                      {!['en-US', 'en-GB', 'ja-JP', 'ko-KR', 'zh-CN'].includes(voiceLanguage) && <option value={voiceLanguage}>{voiceLanguage}</option>}
                      <option value="en-US">English (US)</option>
                      <option value="en-GB">English (UK)</option>
                      <option value="ja-JP">日本語</option>
                      <option value="ko-KR">한국어</option>
                      <option value="zh-CN">中文（简体）</option>
                    </select>
                  </label>
                  <button type="button" onClick={() => void startVoiceRecording()} className="min-h-9 rounded-lg bg-zinc-100 px-3 text-sm font-medium text-zinc-950 hover:bg-white">
                    Start microphone
                  </button>
                </div>
              )}

              {(voiceStatus === "starting" || voiceStatus === "transcribing") && (
                <p className="mt-3 text-xs text-zinc-300" role="status" aria-live="polite">
                  {voiceStatus === "starting" ? "Waiting for microphone access…" : "Transcribing the captured audio…"}
                </p>
              )}

              {voiceStatus === "listening" && (
                <div className="mt-3 flex items-center gap-3" role="status" aria-live="polite">
                  <span className="rumi-voice-waveform flex-1" aria-hidden="true">
                    {Array.from({ length: 18 }, (_, index) => <i key={index} style={{ animationDelay: `${(index % 6) * -90}ms` }} />)}
                  </span>
                  <span className="font-mono text-xs tabular-nums text-zinc-300">{formatVoiceDuration(voiceElapsedSeconds)}</span>
                  <button type="button" onClick={() => void stopAndTranscribeVoice()} className="flex min-h-9 items-center gap-2 rounded-lg bg-zinc-100 px-3 text-sm font-medium text-zinc-950 hover:bg-white">
                    <Square size={10} fill="currentColor" />
                    Stop and review
                  </button>
                  <button type="button" onClick={cancelVoiceInput} className="min-h-9 rounded-lg px-3 text-sm text-zinc-300 hover:bg-white/[0.06]">Discard</button>
                </div>
              )}

              {voiceStatus === "review" && (
                <div className="mt-3">
                  <label className="block text-xs text-zinc-300">
                    Editable transcript
                    <textarea value={voiceTranscript} onChange={(event) => setVoiceTranscript(event.currentTarget.value)} rows={3} className="mt-1 block max-h-40 min-h-20 w-full resize-y rounded-lg border border-white/10 bg-zinc-950/70 px-3 py-2 text-sm leading-5 text-zinc-100 outline-none focus:border-sky-400/50" />
                  </label>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button type="button" onClick={() => applyVoiceTranscript("insert")} disabled={!voiceTranscript.trim()} className="min-h-9 rounded-lg bg-zinc-100 px-3 text-sm font-medium text-zinc-950 disabled:opacity-40">Insert at cursor</button>
                    <button type="button" onClick={() => applyVoiceTranscript("append")} disabled={!voiceTranscript.trim()} className="min-h-9 rounded-lg border border-white/10 px-3 text-sm text-zinc-200 disabled:opacity-40">Append</button>
                    <button type="button" onClick={() => applyVoiceTranscript("replace")} disabled={!voiceTranscript.trim()} className="min-h-9 rounded-lg border border-white/10 px-3 text-sm text-zinc-200 disabled:opacity-40">Replace draft</button>
                    <button type="button" onClick={() => setVoiceStatus("consent")} className="min-h-9 rounded-lg px-3 text-sm text-zinc-300 hover:bg-white/[0.06]">Record again</button>
                    <button type="button" onClick={cancelVoiceInput} className="min-h-9 rounded-lg px-3 text-sm text-zinc-300 hover:bg-white/[0.06]">Discard</button>
                  </div>
                </div>
              )}

              {voiceStatus === "error" && (
                <div className="mt-3 flex flex-wrap items-center gap-2" role="alert">
                  <p className="mr-auto text-xs text-rose-200">{voiceError || composerVoiceErrorMessage("unknown")}</p>
                  <ErrorCopyAction copyText={voiceError || composerVoiceErrorMessage("unknown")} label="Copy voice input error" />
                  <button type="button" onClick={() => { setVoiceError(""); setVoiceStatus("consent"); }} className="min-h-9 rounded-lg border border-white/10 px-3 text-sm text-zinc-100">Try again</button>
                </div>
              )}
            </section>
          )}

          {!isNewConversation && visibleSteerPreviewItems.length > 0 && (
            <div className="mx-2 mt-1 overflow-hidden rounded-xl bg-zinc-900/45 px-2 py-1.5 max-[640px]:mx-1.5 max-[640px]:px-1.5">
              <div className="flex items-center justify-between gap-2 pb-1 text-[10px] leading-none text-zinc-500">
                <div className="flex min-w-0 items-center gap-1.5">
                  <CornerDownRight size={12} className="flex-shrink-0" />
                  {visibleSteerPreviewItems.length > 1 && (
                    <span className="rounded-full bg-zinc-800/80 px-1.5 py-0.5 text-[9px] leading-none">
                      {visibleSteerPreviewItems.length}
                    </span>
                  )}
                </div>
                <div className="flex min-w-0 flex-shrink items-center justify-end gap-1.5">
                  {steerBusy && <Loader2 size={11} className="flex-shrink-0 animate-spin" />}
                  {steerSuccessStatus && <span className="truncate">{steerSuccessStatus}</span>}
                </div>
              </div>
              <div className="grid gap-1">
                {visibleSteerPreviewItems.map((item) => (
                  <div key={item.id} className="grid gap-1 rounded-lg bg-zinc-950/30 px-2 py-1.5">
                    <div className="flex items-center gap-2">
                      <span className="rounded bg-zinc-800/80 px-1.5 py-0.5 text-[9px] leading-none text-zinc-500">
                        {steerStatusLabel(item.status)}
                      </span>
                    </div>
                    <div className="max-h-16 overflow-y-auto whitespace-pre-wrap break-words text-[12px] leading-4 text-zinc-300">
                      {String(item.prompt ?? "").trim()}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {steerError && (
            <ErrorNotice
              className="mx-2 mt-1 rounded-xl px-2 py-1.5 text-[10px] leading-4"
              copyLabel="ステアエラーをコピー"
              errorIcon="conversation-steer"
              message={steerError}
              title="追加指示を送信できませんでした"
            />
          )}

          {composerInputBlocked && (
            <div
              id="composer-submission-preparing"
              role="status"
              aria-live="polite"
              data-composer-controls="preparing"
              className="mx-4 mt-2 text-xs text-zinc-500 max-[640px]:mx-3"
            >
              会話を準備しています。
            </div>
          )}

          {pendingRecovery && (
            <div
              id="composer-pending-recovery"
              data-composer-controls="recovery"
              className="mx-4 mt-2 rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2 text-xs leading-5 text-zinc-300 max-[640px]:mx-3"
            >
              <p role="status" aria-live="polite">{pendingRecovery.message}</p>
              {pendingRecovery.onDetach && (
                <>
                  <button
                    type="button"
                    onClick={pendingRecovery.onDetach}
                    className="mt-1 min-h-9 rounded-lg border border-white/15 px-3 py-1 text-zinc-100 transition-colors hover:bg-white/[0.06]"
                  >
                    記録を残して待機を解除
                  </button>
                  <p className="mt-1 text-zinc-400">元の送信の停止・再送は行いません。</p>
                </>
              )}
              {pendingRecovery.entries.length > 0 && (
                <details className="mt-1">
                  <summary className="cursor-pointer text-zinc-400">未確認の送信記録（{pendingRecovery.entries.length}件）</summary>
                  <ul className="mt-1 max-h-40 space-y-2 overflow-y-auto">
                    {pendingRecovery.entries.map((entry) => (
                      <li key={entry.id}>
                        <p className="text-zinc-400">{entry.label}</p>
                        {entry.submittedText && <p className="whitespace-pre-wrap break-words">{entry.submittedText}</p>}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          )}

          {toolSelectionReview && (
            <ToolSelectionReviewCard
              review={toolSelectionReview}
              labelForService={labelForServiceId}
              onApprove={() => onToolSelectionReviewApprove?.()}
              onEdit={() => {
                onToolSelectionReviewEdit?.();
                setOpenFolder("tools");
                setMenuOpen(true);
              }}
              onNoTools={() => onToolSelectionReviewNoTools?.()}
              onCancel={() => onToolSelectionReviewCancel?.()}
            />
          )}

          {visibleDroppedWidgets.length > 0 && (
            <div className="rumi-composer-context-strip flex max-w-full flex-wrap gap-1.5 px-4 pb-0.5 pt-2 max-[640px]:px-3">
              {visibleDroppedWidgets.map((widget) => (
                <DroppedWidgetChip
                  key={widget.id}
                  widget={widget.type === "tool" ? { ...widget, enabled: selectedToolIdSet.has(widget.sourceItemId || widget.id) } : widget}
                  onAction={onWidgetAction}
                  onToggle={onWidgetToggle}
                />
              ))}
            </div>
          )}

          {isNewConversation ? (
            <div className="grid gap-1.5">
              <div className="rumi-composer-main-panel flex flex-col justify-between gap-2 rounded-[1.5rem] border border-white/[0.09] bg-[#17181d] p-3 shadow-xl transition-all duration-300">
                <ComposerAttachmentRegion
                  attachedFiles={attachedFiles}
                  pendingPaths={pendingMentionAttachmentPaths}
                  error={attachmentError}
                  onFileRemove={onFileRemove}
                  onPendingRemove={onPendingMentionAttachmentRemove}
                  onTranscribe={transcribeAttachedAudio}
                />
                <div className={`rumi-composer-editor-row grid min-h-11 items-end gap-x-3 ${
                  newConversationInlineLeadingWidgets.length > 0
                    ? "grid-cols-[44px_minmax(0,1fr)_auto]"
                    : newConversationTopRightWidgets.length > 0
                      ? "grid-cols-[minmax(0,1fr)_auto]"
                      : "grid-cols-1"
                }`}>
                  {newConversationInlineLeadingWidgets.length > 0 && (
                    <div className="flex items-center justify-center self-end">
                      {newConversationInlineLeadingWidgets.map((widget) => (
                        <ComposerChromeWidget key={widget.id} widget={widget} />
                      ))}
                    </div>
                  )}
                  <div className="rumi-composer-editor relative min-w-0 self-end">
                    {hasInlineMentions && (
                      <div
                        ref={inlineMentionLayerRef}
                        aria-hidden="true"
                        data-composer-inline-mentions
                        className={`rumi-composer-inline-mention-layer absolute inset-0 overflow-hidden whitespace-pre-wrap break-words px-0 py-2.5 text-[16px] font-medium leading-[24px] text-zinc-100 ${textareaCanCollapse ? "pr-9" : ""}`}
                      >
                        {inlineMentionParts.map((part, index) => (
                          <span key={`${index}:${part.text}`} className={part.mention ? "rumi-composer-inline-mention" : undefined}>{part.text}</span>
                        ))}
                      </div>
                    )}
                    <textarea
                      ref={textareaRef}
                      autoFocus
                      rows={1}
                      value={input}
                      readOnly={voiceStatus !== "idle" || (surfaceMode === "thread" && isGenerating)}
                      data-template-composer-input={templateComposerInputId || undefined}
                      onChange={(event) => {
                        resizeComposerTextarea(event.currentTarget);
                        handleInputChange(event.currentTarget.value);
                      }}
                      placeholder={effectiveComposerPlaceholder}
                      aria-label="Tobkiriにメッセージを送信"
                      aria-describedby={isPendingRecovery ? "composer-pending-recovery" : composerInputBlocked ? "composer-submission-preparing" : undefined}
                      aria-autocomplete="list"
                      aria-controls={activeComposerListboxId}
                      aria-activedescendant={activeComposerOptionId}
                      aria-expanded={showAtMentionSuggestions || showCommandSuggestions || Boolean(commandArgumentPalette)}
                      role="combobox"
                      disabled={composerInputBlocked}
                      className={`rumi-composer-input-new rumi-composer-textarea relative rumi-layer-panel block min-h-[44px] w-full max-h-[240px] select-text resize-none overflow-x-hidden overflow-y-auto border-none bg-transparent px-0 py-2.5 text-[16px] font-medium leading-[24px] caret-zinc-100 outline-none placeholder:text-zinc-500/70 ${hasInlineMentions ? "rumi-composer-textarea-highlighted text-transparent" : "text-zinc-100"} ${textareaCanCollapse ? "pr-9" : ""}`}
                      onScroll={(event) => syncInlineMentionScroll(event.currentTarget)}
                      onFocus={(event) => {
                        setTextareaFocused(true);
                        updateAtMentionStateFromInput(event.currentTarget.value);
                      }}
                      onBlur={() => {
                        window.setTimeout(() => {
                          if (document.activeElement !== textareaRef.current) setTextareaFocused(false);
                        }, 0);
                      }}
                      onClick={() => updateAtMentionStateFromInput(input)}
                      onKeyUp={(event) => {
                        if (event.key !== "Escape") updateAtMentionStateFromInput(input);
                      }}
                      onKeyDownCapture={(event) => {
                        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
                          event.stopPropagation();
                        }
                      }}
                      onCompositionStart={() => { imeActiveRef.current = true; imeGenerationRef.current += 1; }}
                      onCompositionEnd={() => { imeActiveRef.current = false; imeEndedAtRef.current = Date.now(); }}
                      onKeyDown={handleKeyDown}
                      onCopy={handleCopy}
                      onPaste={handlePaste}
                    />
                    <ComposerTextareaResizeButton
                      collapsed={textareaCollapsed}
                      visible={textareaCanCollapse || textareaCollapsed}
                      onToggle={() => setTextareaCollapsed((current) => !current)}
                    />
                  </div>
                  {newConversationTopRightWidgets.length > 0 && (
                    <div className="flex items-center justify-end gap-2 self-end">
                      {newConversationTopRightWidgets.map((widget) => (
                        <ComposerChromeWidget key={widget.id} widget={widget} />
                      ))}
                    </div>
                  )}
                </div>

                <div className="rumi-composer-toolbar flex items-center justify-between border-t border-white/5 pt-2">
                  <div className="flex min-w-0 items-center gap-2 overflow-visible">
                    {newConversationInlineActionWidgets.map((widget) => (
                      <ComposerChromeWidget key={widget.id} widget={widget} />
                    ))}
                  </div>
                  {newConversationTrailingWidgets.length > 0 && (
                    <div className="rumi-composer-model-dock flex min-w-0 items-center justify-end gap-2 max-[640px]:hidden">
                      {newConversationTrailingWidgets.map((widget) => (
                        <ComposerChromeWidget
                          key={widget.id}
                          widget={widget}
                          onNodeChange={registerChromeWidgetNode}
                        />
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </div>
          ) : (
            <>
              <ComposerAttachmentRegion
                attachedFiles={attachedFiles}
                pendingPaths={pendingMentionAttachmentPaths}
                error={attachmentError}
                onFileRemove={onFileRemove}
                onPendingRemove={onPendingMentionAttachmentRemove}
                onTranscribe={transcribeAttachedAudio}
              />
              <div className={`grid min-w-0 items-end gap-1 px-2 ${conversationFileAttachWidget ? "grid-cols-[44px_minmax(0,1fr)]" : "grid-cols-1"}`}>
                {conversationFileAttachWidget && (
                  <div className="self-start pb-0.5 pt-1.5">
                    <ComposerChromeWidget widget={conversationFileAttachWidget} />
                  </div>
                )}
                <div className="relative min-w-0">
                  {hasInlineMentions && (
                    <div
                      ref={inlineMentionLayerRef}
                      aria-hidden="true"
                      data-composer-inline-mentions
                      className={`rumi-composer-inline-mention-layer absolute inset-0 overflow-hidden whitespace-pre-wrap break-words px-2 pb-0 pt-2.5 text-[15px] font-normal leading-[22px] text-zinc-100 max-[640px]:pb-0 max-[640px]:pt-2.5 max-[640px]:text-[13px] ${textareaCanCollapse ? "pr-11 max-[640px]:pr-10" : ""}`}
                    >
                      {inlineMentionParts.map((part, index) => (
                        <span key={`${index}:${part.text}`} className={part.mention ? "rumi-composer-inline-mention" : undefined}>{part.text}</span>
                      ))}
                    </div>
                  )}
                  <textarea
                    ref={textareaRef}
                    rows={1}
                    value={input}
                    readOnly={voiceStatus !== "idle" || (surfaceMode === "thread" && isGenerating)}
                    data-template-composer-input={templateComposerInputId || undefined}
                    onChange={(event) => {
                      resizeComposerTextarea(event.currentTarget);
                      handleInputChange(event.currentTarget.value);
                    }}
                    placeholder={effectiveComposerPlaceholder}
                    aria-label="Tobkiriにメッセージを送信"
                    aria-describedby={isPendingRecovery ? "composer-pending-recovery" : composerInputBlocked ? "composer-submission-preparing" : undefined}
                    aria-autocomplete="list"
                    aria-controls={activeComposerListboxId}
                    aria-activedescendant={activeComposerOptionId}
                    aria-expanded={showAtMentionSuggestions || showCommandSuggestions || Boolean(commandArgumentPalette)}
                    role="combobox"
                    disabled={composerInputBlocked}
                    className={`rumi-composer-textarea relative min-h-[24px] w-full max-h-[240px] select-text resize-none overflow-x-hidden overflow-y-auto border-none bg-transparent px-2 pb-0 pt-2.5 text-[15px] font-normal leading-[22px] caret-zinc-100 outline-none placeholder:text-zinc-500/70 max-[640px]:min-h-[24px] max-[640px]:pb-0 max-[640px]:pt-2.5 max-[640px]:text-[13px] ${hasInlineMentions ? "rumi-composer-textarea-highlighted text-transparent" : "text-zinc-100"} ${textareaCanCollapse ? "pr-11 max-[640px]:pr-10" : ""}`}
                    onScroll={(event) => syncInlineMentionScroll(event.currentTarget)}
                    onFocus={(event) => {
                      setTextareaFocused(true);
                      updateAtMentionStateFromInput(event.currentTarget.value);
                    }}
                    onBlur={() => {
                      window.setTimeout(() => {
                        if (document.activeElement !== textareaRef.current) setTextareaFocused(false);
                      }, 0);
                    }}
                    onClick={() => updateAtMentionStateFromInput(input)}
                    onKeyUp={(event) => {
                      if (event.key !== "Escape") updateAtMentionStateFromInput(input);
                    }}
                    onKeyDownCapture={(event) => {
                      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
                        event.stopPropagation();
                      }
                    }}
                    onCompositionStart={() => { imeActiveRef.current = true; imeGenerationRef.current += 1; }}
                      onCompositionEnd={() => { imeActiveRef.current = false; imeEndedAtRef.current = Date.now(); }}
                      onKeyDown={handleKeyDown}
                    onCopy={handleCopy}
                    onPaste={handlePaste}
                  />
                  <ComposerTextareaResizeButton
                    collapsed={textareaCollapsed}
                    visible={textareaCanCollapse || textareaCollapsed}
                    onToggle={() => setTextareaCollapsed((current) => !current)}
                  />
                </div>
              </div>
            </>
          )}

          {!isSteerMode && (effectiveComposerHelp || (!isNewConversation && templateComposerInfoItems.length > 0)) && (
            <div className="flex flex-wrap gap-2 px-5 pt-1 text-[10px] text-zinc-500">
              {effectiveComposerHelp && <span>{effectiveComposerHelp}</span>}
              {!isNewConversation && templateComposerInfoItems.map((item) => <span key={item}>{item}</span>)}
            </div>
          )}

          <input
            ref={fileInputRef}
            type="file"
            multiple
            disabled={!templateAllowsFileAttachments}
            className="hidden"
            onChange={(event) => {
              void attachFiles(event.target.files).finally(() => {
                event.target.value = "";
              });
            }}
          />
          <input
            ref={imageInputRef}
            type="file"
            accept="image/*"
            multiple
            disabled={!templateAllowsFileAttachments}
            className="hidden"
            onChange={(event) => {
              void attachFiles(event.target.files).finally(() => {
                event.target.value = "";
              });
            }}
          />

          {!isNewConversation && (
            <div className={surfaceMode === "scheduled" ? "grid min-w-0 gap-1 p-2" : "px-3 pb-2.5 pt-1 flex items-center justify-between gap-2 max-[640px]:gap-1.5 max-[640px]:px-2 max-[640px]:pb-1.5"}>
              <div className="flex min-w-0 items-center gap-1 overflow-visible">
                {leadingChromeWidgets.map((widget) => (
                  <ComposerChromeWidget key={widget.id} widget={widget} />
                ))}
              </div>

              <div className="rumi-composer-submit-area flex flex-shrink-0 items-center justify-end gap-2">
                {trailingChromeWidgets.map((widget) => (
                  <ComposerChromeWidget
                    key={widget.id}
                    widget={widget}
                    onNodeChange={registerChromeWidgetNode}
                  />
                ))}
              </div>
            </div>
          )}

          {mode === "coding" && codingContext && (
            <div className="px-5 pb-2 pt-0 flex flex-wrap items-center gap-2 text-[11px] text-zinc-500 max-[640px]:px-3">
              <CodingWorkspaceBadge workspace={selectedCodingWorkspace} compact />
              <CodingWorkspacePicker
                workspaces={codingWorkspaces}
                selectedWorkspaceId={selectedCodingWorkspace?.workspace_id ?? selectedCodingWorkspaceId ?? codingContext.workspaceId ?? null}
                disabled={isGenerating}
                onSelect={onCodingWorkspaceSelect}
                onTrust={onCodingWorkspaceTrust}
                onCreate={onCodingWorkspaceCreate}
                onRefresh={onCodingWorkspacesRefresh}
              />
              <span className="inline-flex min-w-0 items-center gap-1">
                <GitBranch size={11} />
                {branchOptions.length > 1 ? (
                  <select
                    value={codingContext.branch ?? ""}
                    onChange={(event) => event.target.value && onCodingBranchSwitch?.(event.target.value, false)}
                    disabled={isGenerating}
                    className="max-w-[140px] bg-transparent font-mono text-zinc-400 outline-none hover:text-zinc-200 disabled:opacity-50"
                    title="ブランチを切り替え"
                  >
                    {branchOptions.map((branch) => (
                      <option key={branch} value={branch} className="bg-zinc-900 text-zinc-100">
                        {branch}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span className="font-mono">{codingContext.branch ?? "no git"}</span>
                )}
              </span>
              <span className="inline-flex items-center gap-1">
                <FileText size={11} />
                <select
                  value={currentDirectory}
                  onChange={(event) => onCodingDirectoryChange?.(event.target.value)}
                  disabled={isGenerating}
                  className="max-w-[140px] bg-transparent font-mono text-zinc-400 outline-none hover:text-zinc-200 disabled:opacity-50"
                  title="target folder"
                >
                  <option value="." className="bg-zinc-900 text-zinc-100">.</option>
                  {directoryEntries.map((entry) => (
                    <option key={entry.path} value={entry.path} className="bg-zinc-900 text-zinc-100">
                      {entry.path}
                    </option>
                  ))}
                </select>
              </span>
            </div>
          )}
        </ComposerFrame>
      </div>
    </div>
  );
}
