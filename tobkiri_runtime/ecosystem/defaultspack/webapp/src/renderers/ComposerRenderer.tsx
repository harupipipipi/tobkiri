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
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
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
import { RuntimeCapabilityBanner } from "../components/RuntimeCapabilityBanner";
import { StructuredComposerPanel } from "../components/StructuredComposerPanel";
import { WarmActionIcon } from "../components/WarmActionIcon";
import { chatComposerResources } from "../features/chat/resources/chatComposerResources";
import {
  DEFAULT_MODEL_SELECTOR_SCHEMA,
  filterModelProfilesBySelector,
  modelSelectorSchemaForSurface,
} from "../features/models";
import { ActionApprovalControl } from "../features/tools/ActionApprovalControl";
import { ProjectPicker } from "../features/projects/ProjectPicker";
import { ToolOverrideChips } from "../features/tools/ToolOverrideChips";
import { ToolSelectionReviewCard } from "../features/tools/ToolSelectionReviewCard";
import {
  isAudioAttachment,
  modelSupportsAudioInput,
  readableTranscriptionError,
  requestComposerAudioTranscript,
  transcriptAttachmentFromAudio,
} from "../features/voice/composerVoice";
import { fileToAttachment } from "../lib/attachments";
import { composerFileMentionWidget, composerKnownMentionValues, composerMentionToolIdsFromWidgets, composerServiceMentionWidget, composerSkillMentionDisplay, composerSkillMentionWidget, composerToolMentionDisplay, composerToolMentionWidget, filterComposerSkillMentions, filterComposerToolMentions, resolveComposerWidgetDrop, skillMentionIdsFromText, toolMentionIdsFromText } from "../lib/composerWidgets";
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
import { HISTORY_CHAT_DROP_MIME, parseHistoryChatDrop } from "../lib/historyComposer";
import { activeMentionAtCursor, isMentionStart, utf16OffsetToCodePointIndex } from "../lib/mentionContract";
import { sortedToolGroups, toolGroupFor } from "../lib/toolUi";
import { startPinchAudioRecorder, type ActiveAudioRecorder, type AmbientAudioRecording } from "../ambient/ambientMedia";
import composerPaletteTemplateJson from "../templates/composerPalette.template.json";

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
  keyCode?: number;
  nativeEvent?: { isComposing?: boolean };
}): boolean {
  return event.keyCode === 229 || event.nativeEvent?.isComposing === true;
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

const COMPOSER_CONTROL_SURFACE_CLASSNAME = "rumi-composer-control-surface flex h-[44px] min-h-[44px] min-w-0 items-center rounded-xl border border-white/[0.08] bg-white/[0.045] px-2.5";
const AT_MENTION_LISTBOX_ID = "composer-at-mention-listbox";
const COMPOSER_MODEL_CONTROL_MIN_CH = 9;
const COMPOSER_MODEL_CONTROL_MAX_CH = 18;
const COMPOSER_MODEL_CONTROL_CHROME_CH = 6;
const NEW_CONVERSATION_TEXTAREA_MIN_HEIGHT = 22;
const NEW_CONVERSATION_TEXTAREA_MAX_HEIGHT = 240;
const CONVERSATION_TEXTAREA_MIN_HEIGHT = 24;
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
      className="absolute right-1 top-1 rumi-layer-panel flex h-7 w-7 items-center justify-center rounded-lg border border-white/[0.07] bg-[#17181d]/90 text-zinc-500 shadow-sm transition-colors hover:bg-zinc-800 hover:text-zinc-100"
    >
      <Icon size={13} />
    </button>
  );
}

function composerIconForName(iconName: string | undefined, fallback: LucideIcon): LucideIcon {
  const normalized = String(iconName ?? "").trim().toLowerCase();
  if (/search|browser|web|globe/.test(normalized)) return Search;
  if (/file|document|pdf|text/.test(normalized)) return FileText;
  if (/folder|directory/.test(normalized)) return Folder;
  if (/git|branch|repo/.test(normalized)) return GitBranch;
  if (/code|terminal|shell|cli/.test(normalized)) return Code2;
  if (/model|cpu|provider|ai/.test(normalized)) return Cpu;
  if (/think|brain|reason/.test(normalized)) return BrainCircuit;
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
  deepthink: BrainCircuit,
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

function modelSearchItemToProfile(item: ModelSearchItem): ModelProfile {
  const providerId = String(item.provider_id ?? "").trim();
  const modelId = String(item.model_id ?? "").trim();
  const profileId = String(item.profile_id ?? item.qualified_model_id ?? (providerId && modelId ? `${providerId}/${modelId}` : "")).trim();
  const metadata = item.metadata && typeof item.metadata === "object" ? item.metadata : undefined;
  const rawMaxContext = Number((metadata as Record<string, unknown> | undefined)?.max_context ?? NaN);
  return {
    profile_id: profileId,
    qualified_model_id: String(item.qualified_model_id ?? profileId).trim() || profileId,
    display_name: String(item.display_name ?? item.label ?? profileId).trim() || profileId,
    provider_id: providerId,
    provider_display_name: String(item.provider_display_name ?? providerId).trim() || providerId,
    model_id: modelId,
    max_context: Number.isFinite(rawMaxContext) ? rawMaxContext : undefined,
    max_context_tokens: Number.isFinite(rawMaxContext) ? rawMaxContext : undefined,
    supports_thinking: item.supports_thinking,
    supports_vision: item.supports_vision,
    supports_image_input: item.supports_image_input,
    supports_tool_calling: item.supports_tool_calling,
    supports_fast: item.supports_fast,
    speed_tier: item.speed_tier,
    quality_tier: item.quality_tier,
    cost_tier: item.cost_tier,
    knowledge_level: item.knowledge_level,
    capability_tags: item.capability_tags,
    availability: item.availability,
    metadata,
  };
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
        <span
          role="alert"
          title={transcriptionError}
          className="absolute inset-x-1 bottom-1 line-clamp-3 rounded bg-rose-950/95 px-1.5 py-1 text-[8px] leading-tight text-rose-100"
        >
          {transcriptionError}
        </span>
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
  onFileRemove,
  onPendingRemove,
  onTranscribe,
}: {
  attachedFiles: AttachedFile[];
  pendingPaths: string[];
  onFileRemove?: (id: string) => void;
  onPendingRemove?: (path: string) => void;
  onTranscribe?: (file: AttachedFile) => Promise<void>;
}) {
  const hasAttachments = attachedFiles.length > 0 || pendingPaths.length > 0;
  return (
    <div
      className="rumi-composer-attachment-reveal"
      data-composer-attachment-region
      data-attachment-state={hasAttachments ? "expanded" : "collapsed"}
      aria-hidden={!hasAttachments}
    >
      <div className="rumi-composer-attachment-reveal-inner">
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
        <ConversationIcon size={11} className="flex-shrink-0" />
        <span className="truncate">{widget.label}</span>
      </button>
    );
  }

  if (widget.widgetKind !== "tool_toggle" && widget.type !== "tool") {
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
        <Icon size={10} />
        <span className="truncate">{widget.label}</span>
      </button>
    );
  }

  const ToolIcon = composerIconForName(widget.icon, Wrench);
  const toolToggleClassName = `inline-flex items-center gap-1.5 rounded-lg border px-2 py-1 text-[11px] transition-colors ${
    widget.enabled
      ? "border-sky-400/25 bg-sky-400/[0.08] text-sky-100"
      : "border-white/[0.07] bg-white/[0.04] text-zinc-400"
  }`;
  const toolToggleContent = (
    <>
      <ToolIcon size={11} className="flex-shrink-0" />
      <span className="truncate">{widget.label}</span>
    </>
  );

  if (!onToggle) {
    return (
      <span className={`${toolToggleClassName} cursor-default`}>
        {toolToggleContent}
      </span>
    );
  }

  return (
    <button
      type="button"
      title={widget.description ?? widget.label}
      className={`${toolToggleClassName} cursor-pointer hover:bg-white/[0.08]`}
      onClick={() => onToggle(widget.id)}
    >
      {toolToggleContent}
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
          {error && <p className="text-[11px] text-red-300">{error}</p>}
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

function ModelDropdown({
  profiles,
  selectedProfile,
  isGenerating,
  placement = "above",
  onSelect,
  onClose,
  selectorSchema = DEFAULT_MODEL_SELECTOR_SCHEMA,
}: {
  profiles: ModelProfile[];
  selectedProfile: ModelProfile | null;
  isGenerating: boolean;
  placement?: "above" | "below";
  onSelect: (profile: ModelProfile) => void;
  onClose: () => void;
  selectorSchema?: typeof DEFAULT_MODEL_SELECTOR_SCHEMA;
}) {
  const [search, setSearch] = useState("");
  const [remoteProfiles, setRemoteProfiles] = useState<ModelProfile[]>([]);
  const [activeProviderIndex, setActiveProviderIndex] = useState(0);
  const [activeModelIndex, setActiveModelIndex] = useState(0);
  const searchRequestSeqRef = useRef(0);
  const activeOptionRef = useRef<HTMLButtonElement | null>(null);
  const trimmedSearch = search.trim();
  const resolvedSelectorSchema = useMemo(
    () => modelSelectorSchemaForSurface(selectorSchema, "composer"),
    [selectorSchema],
  );
  const providerTrigger = resolvedSelectorSchema.layout.provider_trigger;
  const providerState = useMemo(
    () => modelProviderSearchState(search, providerTrigger),
    [providerTrigger, search],
  );
  const eligibleProfiles = useMemo(
    () => filterModelProfilesBySelector(profiles, resolvedSelectorSchema, "composer"),
    [profiles, resolvedSelectorSchema],
  );
  const providerOptions = useMemo(() => modelProviderOptions(eligibleProfiles), [eligibleProfiles]);
  const providerSuggestions = useMemo(() => {
    if (!providerState.active) return [];
    const query = normalizeProviderSearchToken(providerState.providerQuery);
    const selectedProviderId = profileProviderId(selectedProfile);
    return providerOptions
      .filter((provider) => !query || [provider.id, provider.label].some((value) => normalizeProviderSearchToken(value).includes(query)))
      .sort((left, right) => {
        if (left.id === selectedProviderId) return -1;
        if (right.id === selectedProviderId) return 1;
        return left.label.localeCompare(right.label, "ja");
      });
  }, [providerOptions, providerState.active, providerState.providerQuery, selectedProfile]);
  const filtered = useMemo(
    () => filterModelProfilesBySearch(
      eligibleProfiles,
      search,
      providerTrigger,
    ),
    [eligibleProfiles, providerTrigger, search],
  );

  useEffect(() => {
    searchRequestSeqRef.current += 1;
    const requestSeq = searchRequestSeqRef.current;
    setRemoteProfiles([]);
    if (!trimmedSearch || providerState.active) {
      return;
    }
    let disposed = false;
    const timer = window.setTimeout(() => {
      chatComposerResources.searchModels({
        query: trimmedSearch,
        max_results: resolvedSelectorSchema.layout.max_visible_options,
      })
        .then((result) => {
          if (disposed || requestSeq !== searchRequestSeqRef.current) return;
          setRemoteProfiles(filterModelProfilesBySelector(
            (result.models ?? []).map(modelSearchItemToProfile),
            resolvedSelectorSchema,
            "composer",
          ));
        })
        .catch(() => {
          if (disposed || requestSeq !== searchRequestSeqRef.current) return;
          setRemoteProfiles([]);
        });
    }, 160);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [providerState.active, resolvedSelectorSchema, trimmedSearch]);

  const visibleProfiles = useMemo(() => {
    const byId = new Map<string, ModelProfile>();
    for (const profile of [...filtered, ...remoteProfiles]) {
      const key = profile.profile_id || profile.qualified_model_id || `${profile.provider_id ?? ""}/${profile.model_id ?? ""}`;
      if (!key || byId.has(key)) continue;
      byId.set(key, profile);
    }
    const values = [...byId.values()];
    if (!trimmedSearch && selectedProfile && resolvedSelectorSchema.layout.selected_position === "first") {
      const selectedId = selectedProfile.profile_id || selectedProfile.qualified_model_id;
      const selectedIndex = values.findIndex((profile) => (profile.profile_id || profile.qualified_model_id) === selectedId);
      if (selectedIndex > 0) values.unshift(...values.splice(selectedIndex, 1));
    }
    return values.slice(0, resolvedSelectorSchema.layout.max_visible_options);
  }, [filtered, remoteProfiles, resolvedSelectorSchema.layout.max_visible_options, resolvedSelectorSchema.layout.selected_position, selectedProfile, trimmedSearch]);

  useEffect(() => {
    setActiveProviderIndex(0);
  }, [search, providerSuggestions.length]);

  useEffect(() => {
    setActiveModelIndex(0);
  }, [search, visibleProfiles.length]);

  useEffect(() => {
    activeOptionRef.current?.scrollIntoView({ block: "nearest" });
  }, [activeModelIndex, activeProviderIndex, providerState.active]);

  const groupedByProvider = useMemo(() => {
    if (resolvedSelectorSchema.layout.group_by === "none") {
      return [["", visibleProfiles] as [string, ModelProfile[]]];
    }
    const map = new Map<string, ModelProfile[]>();
    for (const profile of visibleProfiles) {
      const provider = profile.provider_id ?? "other";
      const list = map.get(provider) ?? [];
      list.push(profile);
      map.set(provider, list);
    }
    return [...map.entries()];
  }, [resolvedSelectorSchema.layout.group_by, visibleProfiles]);

  const activeProvider = providerSuggestions[Math.min(activeProviderIndex, Math.max(0, providerSuggestions.length - 1))] ?? null;
  const activeProfile = visibleProfiles[Math.min(activeModelIndex, Math.max(0, visibleProfiles.length - 1))] ?? null;
  const highlightedPrefix = providerState.highlightPrefix;
  const highlightedSuffix = highlightedPrefix ? search.slice(highlightedPrefix.length) : "";
  const handleSearchKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    const action = modelSearchKeyAction({
      key: event.key,
      shiftKey: event.shiftKey,
      providerMode: providerState.active,
      providerCount: providerSuggestions.length,
      providerIndex: activeProviderIndex,
      modelCount: visibleProfiles.length,
      modelIndex: activeModelIndex,
      providerConfirmKey: resolvedSelectorSchema.layout.provider_confirm_key,
      modelConfirmKeys: resolvedSelectorSchema.layout.model_confirm_keys,
    });
    if (!action.handled) return;
    event.preventDefault();
    if (action.type === "close") {
      onClose();
      return;
    }
    if (action.type === "move_provider") {
      setActiveProviderIndex(action.index);
      return;
    }
    if (action.type === "confirm_provider") {
      const provider = providerSuggestions[action.index];
      if (provider) setSearch(`${providerTrigger}${provider.id} `);
      return;
    }
    if (action.type === "move_model") {
      setActiveModelIndex(action.index);
      return;
    }
    const profile = visibleProfiles[action.index];
    if (profile) {
      onSelect(profile);
      onClose();
    }
  };

  return (
    <>
      <button type="button" aria-label="close model dropdown" className="fixed inset-0 rumi-layer-local-popover cursor-default" onClick={onClose} />
      <div
        className={`absolute rumi-layer-command-palette w-[min(400px,calc(100vw-88px))] max-w-[calc(100vw-88px)] overflow-hidden rumi-popover ${
          modelDropdownPlacementClassName(placement)
        }`}
      >
        <div className="border-b border-white/[0.06] p-2.5">
          <div className="mb-2 flex min-w-0 items-center justify-between gap-2 px-0.5 text-[10px] text-zinc-500">
            <span className="truncate">現在: {compactProfileName(profileDisplayName(selectedProfile))}</span>
            <span className="flex-shrink-0">↑↓ で移動</span>
          </div>
          <label className="flex h-9 items-center gap-2 rounded-lg border border-white/[0.08] bg-black/25 px-2.5 text-sm focus-within:border-sky-400/50">
            <Search size={14} className="flex-shrink-0 text-zinc-500" />
            <span className="relative min-w-0 flex-1">
              {highlightedPrefix && (
                <span aria-hidden="true" className="pointer-events-none absolute inset-0 flex items-center whitespace-pre text-sm">
                  <span className="font-medium text-sky-300">{highlightedPrefix}</span>
                  <span className="text-zinc-200">{highlightedSuffix}</span>
                </span>
              )}
              <input
                type="text"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                onKeyDown={handleSearchKeyDown}
                placeholder={`モデルを検索... ${providerTrigger} でプロバイダー`}
                role="combobox"
                aria-label="モデルを検索"
                aria-expanded="true"
                aria-controls={providerState.active ? "model-provider-options" : "model-search-options"}
                aria-activedescendant={providerState.active
                  ? activeProvider ? `model-provider-${activeProvider.id}` : undefined
                  : activeProfile ? `model-option-${activeProfile.profile_id}` : undefined}
                className={`relative w-full bg-transparent outline-none placeholder:text-zinc-600 ${highlightedPrefix ? "text-transparent caret-zinc-100" : "text-zinc-200"}`}
                autoFocus
              />
            </span>
          </label>
          {providerState.active && (
            <div className="mt-2 rounded-lg border border-sky-400/20 bg-sky-400/[0.07] px-2.5 py-2" aria-live="polite">
              <div className="flex items-center justify-between gap-2">
                <span className="text-[10px] font-semibold uppercase tracking-wider text-sky-300">プロバイダー選択中</span>
                <span className="rounded border border-sky-400/20 bg-sky-400/[0.08] px-1.5 py-0.5 text-[9px] text-sky-200">
                  {resolvedSelectorSchema.layout.provider_confirm_key}でプロバイダーを確定
                </span>
              </div>
              <p className="mt-1 truncate text-[12px] font-medium text-zinc-100">
                {activeProvider?.label ?? "一致するプロバイダーなし"}
              </p>
              <p className="truncate text-[10px] text-zinc-500">
                {activeProvider
                  ? `${activeProvider.id}${resolvedSelectorSchema.layout.show_provider_count ? ` · ${activeProvider.modelCount} models` : ""}`
                  : `${providerTrigger} の後にプロバイダー名を入力`}
              </p>
            </div>
          )}
        </div>
        <div
          id={providerState.active ? "model-provider-options" : "model-search-options"}
          role="listbox"
          aria-label={providerState.active ? "プロバイダー候補" : "モデル候補"}
          className="max-h-64 overflow-y-auto py-1"
        >
          {providerState.active ? providerSuggestions.map((provider, index) => {
            const active = index === activeProviderIndex;
            return (
              <button
                ref={active ? activeOptionRef : undefined}
                id={`model-provider-${provider.id}`}
                key={provider.id}
                type="button"
                role="option"
                aria-selected={active}
                onMouseMove={() => setActiveProviderIndex(index)}
                onClick={() => setSearch(`${providerTrigger}${provider.id} `)}
                className={`flex w-full items-center justify-between gap-3 border-l-2 px-3 py-2 text-left transition-colors ${
                  active ? "border-sky-400 bg-sky-400/[0.09] text-zinc-100" : "border-transparent text-zinc-400 hover:bg-white/[0.05] hover:text-zinc-200"
                }`}
              >
                <span className="min-w-0">
                  <span className="block truncate text-[13px] font-medium">{providerTrigger}{provider.label}</span>
                  <span className="block truncate text-[10px] text-zinc-500">
                    {provider.id}{resolvedSelectorSchema.layout.show_provider_count ? ` · ${provider.modelCount} models` : ""}
                  </span>
                </span>
                {active && <span className="flex items-center gap-1 text-[10px] text-sky-300"><Check size={12} /> {resolvedSelectorSchema.layout.provider_confirm_key}</span>}
              </button>
            );
          }) : groupedByProvider.map(([provider, profiles]) => (
            <div key={provider}>
              {provider && <div className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-600">{provider}</div>}
              {profiles.map((profile) => {
                const needsKey = profileNeedsApiKey(profile);
                const badges = capabilityBadges(profile).slice(0, 4);
                const keyboardActive = activeProfile === profile;
                const current = selectedProfile?.profile_id === profile.profile_id;
                return (
                  <button
                    ref={keyboardActive ? activeOptionRef : undefined}
                    id={`model-option-${profile.profile_id}`}
                    key={profile.profile_id}
                    type="button"
                    role="option"
                    aria-selected={keyboardActive}
                    draggable
                    disabled={isGenerating}
                    onMouseMove={() => setActiveModelIndex(visibleProfiles.indexOf(profile))}
                    onDragStart={(event) => {
                      event.dataTransfer.setData(
                        "application/rumi-widget",
                        JSON.stringify({ id: profile.profile_id, type: "model", label: profile.display_name }),
                      );
                      event.dataTransfer.effectAllowed = "copy";
                    }}
                    onClick={() => {
                      onSelect(profile);
                      onClose();
                    }}
                    className={`w-full flex items-center justify-between gap-2 border-l-2 px-3 py-1.5 text-left transition-colors hover:bg-white/[0.06] disabled:opacity-50 ${
                      keyboardActive ? "border-sky-400 bg-sky-400/[0.09]" : current ? "border-zinc-600 bg-zinc-800/60" : "border-transparent"
                    }`}
                  >
                    <span className="min-w-0">
                      <span className="block truncate text-[13px] text-zinc-200">{compactProfileName(profileDisplayName(profile))}</span>
                      <span className="block truncate text-[10px] text-zinc-500">
                        {profile.provider_display_name ?? profile.provider_id} · {profile.provider_id}/{profile.model_id}
                      </span>
                      {resolvedSelectorSchema.layout.show_capability_tags && badges.length > 0 && (
                        <span className="mt-1 flex flex-wrap gap-1">
                          {badges.map((badge) => (
                            <span key={badge} className="rounded border border-zinc-700 px-1.5 py-0.5 text-[9px] leading-none text-zinc-400">
                              {badge}
                            </span>
                          ))}
                        </span>
                      )}
                    </span>
                    {keyboardActive ? (
                      <span className="flex flex-shrink-0 items-center gap-1 text-[10px] text-sky-300"><Check size={12} /> {resolvedSelectorSchema.layout.model_confirm_keys[0] ?? "Enter"}</span>
                    ) : needsKey ? (
                      <span className="flex-shrink-0 rounded-full border border-amber-500/30 px-2 py-0.5 text-[10px] text-amber-300">
                        API key
                      </span>
                    ) : (
                      <span className="flex-shrink-0 text-right text-[10px] text-zinc-500">
                        <span className="block">{profile.max_context_tokens ?? profile.max_context ?? "?"}</span>
                        {typeof profile.knowledge_level === "number" && <span className="block">KL {profile.knowledge_level}</span>}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          ))}
          {providerState.active && providerSuggestions.length === 0 && (
            <div className="px-3 py-4 text-center text-xs text-zinc-500">プロバイダーが見つかりません</div>
          )}
          {!providerState.active && groupedByProvider.length === 0 && (
            <div className="px-3 py-4 text-center text-xs text-zinc-500">モデルが見つかりません</div>
          )}
        </div>
      </div>
    </>
  );
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

type ComposerAtMentionCandidate =
  | { kind: "tool"; id: string; label: string; description?: string; item: ComposerExtensionItem }
  | { kind: "service"; id: string; label: string; description?: string; service: ToolGroup }
  | { kind: "skill"; id: string; label: string; description?: string; skill: ComposerSkillItem }
  | { kind: "file"; id: string; label: string; description?: string; file: string };

export type JsonListPanelItem = {
  id: string;
  title: string;
  description?: string;
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
}: {
  payload: JsonListPanelPayload;
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
          return (
            <button
              key={item.id}
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
          );
        })}
      </div>
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
          : candidate.file;
    const tone: NonNullable<JsonListPanelItem["badges"]>[number]["tone"] = candidate.kind === "tool"
      ? "sky"
      : candidate.kind === "service"
        ? "cyan"
        : candidate.kind === "skill"
          ? "violet"
          : "blue";
    return {
      id: candidate.id,
      title: candidate.label,
      description: candidate.description,
      icon,
      fallbackIcon: candidate.kind,
      badges: [{ label: candidate.kind, tone }],
    };
  });

  return jsonListPanelPayload({
    id: "composer-at-mention",
    listboxId: AT_MENTION_LISTBOX_ID,
    ariaLabel: "Composer mentions",
    testId: "composer-at-mention-candidates",
    header: { label: "Mentions", icon: "wrench" },
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
          { label: command.risk, tone: riskTone[command.risk] },
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

export function insertAtMentionText(
  input: string,
  cursorPos: number,
  label: string,
  knownValues?: Iterable<string>,
): { value: string; cursor: number } {
  const activeMention = activeMentionAtCursor(input, cursorPos, knownValues);
  const insertAt = activeMention?.start ?? cursorPos;
  const before = input.slice(0, insertAt);
  const after = input.slice(cursorPos);
  const value = `${before}@${label} ${after}`;
  return { value, cursor: insertAt + label.length + 2 };
}

export type ComposerInlineMentionPart = {
  mention: boolean;
  text: string;
};

const INLINE_MENTION_TOKEN_CHAR = /[\p{L}\p{M}\p{N}_./:-]/u;

function composerMentionSyntaxFromWidget(widget: DroppedWidget): string | null {
  if (widget.metadata?.source !== "composer_at_mention") return null;
  const mention = widget.metadata.mention;
  if (!mention || typeof mention !== "object" || Array.isArray(mention)) return null;
  const syntax = String((mention as Record<string, unknown>).syntax ?? "").trim();
  return syntax.startsWith("@") && syntax.length > 1 ? syntax : null;
}

function inlineMentionMatchesAt(input: string, syntax: string, offset: number): boolean {
  const codePointIndex = utf16OffsetToCodePointIndex(input, offset);
  if (!isMentionStart(input, codePointIndex, [syntax.slice(1)])) return false;
  const followingCharacters = [...input.slice(offset + syntax.length)];
  const nextCharacter = followingCharacters[0] ?? "";
  if (!nextCharacter) return true;
  if (nextCharacter === ".") return !INLINE_MENTION_TOKEN_CHAR.test(followingCharacters[1] ?? "");
  return !INLINE_MENTION_TOKEN_CHAR.test(nextCharacter);
}

/** Split composer text into ordinary and selected semantic-mention runs. */
export function composerInlineMentionParts(
  input: string,
  widgets: DroppedWidget[],
): ComposerInlineMentionPart[] {
  if (!input) return [];
  const syntaxes = [...new Set(
    widgets
      .map(composerMentionSyntaxFromWidget)
      .filter((syntax): syntax is string => Boolean(syntax)),
  )].sort((left, right) => right.length - left.length);
  if (syntaxes.length === 0) return [{ mention: false, text: input }];

  const matches: Array<{ end: number; start: number }> = [];
  let cursor = 0;
  while (cursor < input.length) {
    let best: { end: number; start: number } | null = null;
    for (const syntax of syntaxes) {
      for (let offset = input.indexOf(syntax, cursor); offset >= 0; offset = input.indexOf(syntax, offset + 1)) {
        if (!inlineMentionMatchesAt(input, syntax, offset)) continue;
        const candidate = { start: offset, end: offset + syntax.length };
        if (!best || candidate.start < best.start || (candidate.start === best.start && candidate.end > best.end)) {
          best = candidate;
        }
        break;
      }
    }
    if (!best) break;
    matches.push(best);
    cursor = best.end;
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
): AtMentionMenuKeyAction {
  if (key === "Escape") return { handled: true, type: "close" };
  if (candidateCount <= 0) return { handled: false };
  if ((key === "Tab" && !shiftKey) || (key === "Enter" && !shiftKey)) {
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
  input,
  placeholder,
  isNewConversation = false,
  isGenerating,
  selectedProfile,
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
  selectedToolIds = [],
  actionApprovalMode = "ask",
  toolSelectionTargets = [],
  toolSelectionReview = null,
  keyboardButtonNavigation = true,
  steerStatus = null,
  steerBusy = false,
  steerQueuedCount = 0,
  steerPreviewItems = [],
  suppressPopovers = false,
  onOpenModelManager,
  onOpenToolSettings,
  onActionApprovalModeChange,
  onToolSelectionTargetRemove,
  onToolSelectionReviewApprove,
  onToolSelectionReviewEdit,
  onToolSelectionReviewNoTools,
  onToolSelectionReviewCancel,
  onSwitchToVisionModel,
  onExtensionSelect,
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
  onProjectStoragePrepare,
}: ComposerRendererProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [attachmentMenuOpen, setAttachmentMenuOpen] = useState(false);
  const [openFolder, setOpenFolder] = useState<"tools" | "models" | "commands">("tools");
  const [openToolGroup, setOpenToolGroup] = useState<string | null>(null);
  const [modelDropdownOpen, setModelDropdownOpen] = useState(false);
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
  const [voiceStatus, setVoiceStatus] = useState<"idle" | "starting" | "listening" | "transcribing" | "error">("idle");
  const [voiceElapsedSeconds, setVoiceElapsedSeconds] = useState(0);
  const [voiceError, setVoiceError] = useState<string | null>(null);
  const [textareaCollapsed, setTextareaCollapsed] = useState(false);
  const [textareaCanCollapse, setTextareaCanCollapse] = useState(false);
  const [textareaFocused, setTextareaFocused] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuButtonRef = useRef<HTMLButtonElement | null>(null);
  const attachmentMenuRef = useRef<HTMLDivElement | null>(null);
  const attachmentMenuButtonRef = useRef<HTMLButtonElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const inlineMentionLayerRef = useRef<HTMLDivElement | null>(null);
  const voiceRecorderRef = useRef<ActiveAudioRecorder | null>(null);
  const voiceStartedAtRef = useRef(0);
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
  const templateAllowsSlashCommands = templateFeatureFlags.slash_commands !== false;
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
  const templateAllowsFileAttachments = templateFeatureFlags.file_attachments !== false
    && templateFeatureFlags.attachments !== false
    && (!templateHasModalityLimit || templateAcceptedModalities.some((item) => (
      item === "file" || item === "files" || item === "image" || item === "images" || item === "audio" || item === "video"
    )));
  const templateAllowsVoiceInput = templateFeatureFlags.voice_input !== false
    && templateFeatureFlags.voice !== false
    && (!templateHasModalityLimit || templateAcceptedModalities.some((item) => (
      item === "voice" || item === "speech" || item === "audio"
    )));
  const templateAllowsAtMentions = templateFeatureFlags.at_mentions !== false
    && templateFeatureFlags.mentions !== false;
	  const toolItems = useMemo(() => [...inlineExtensions, ...belowExtensions], [inlineExtensions, belowExtensions]);
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
    () => droppedWidgets.filter((widget) => widget.metadata?.source !== "composer_at_mention"),
    [droppedWidgets],
  );
  const visibleToolWidgetIdSet = useMemo(
    () => new Set(
      visibleDroppedWidgets
        .filter((widget) => widget.type === "tool" || widget.widgetKind === "tool_toggle")
        .map((widget) => widget.sourceItemId || widget.id),
    ),
    [visibleDroppedWidgets],
  );
  const inlineMentionToolIdSet = useMemo(
    () => new Set(composerMentionToolIdsFromWidgets(droppedWidgets)),
    [droppedWidgets],
  );
  const visibleToolSelectionTargets = useMemo(
    () => toolSelectionTargets.filter((target) => (
      target.kind !== "tool"
      || (!inlineMentionToolIdSet.has(target.id) && !visibleToolWidgetIdSet.has(target.id))
    )),
    [inlineMentionToolIdSet, toolSelectionTargets, visibleToolWidgetIdSet],
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
  const serviceLabelById = useMemo(() => new Map(toolGroups.map((group) => [group.id, group.label])), [toolGroups]);
  const toolLabelById = useMemo(() => new Map(toolItems.map((item) => [item.id, item.label || item.id])), [toolItems]);
  const labelForServiceId = useCallback((serviceId: string) => serviceLabelById.get(serviceId) ?? serviceId, [serviceLabelById]);
  const labelForToolTarget = useCallback((target: { kind: string; id: string }) => (
    target.kind === "tool" ? (toolLabelById.get(target.id) ?? target.id) : labelForServiceId(target.id)
  ), [labelForServiceId, toolLabelById]);
  const computerUseSelected = selectedToolIds.some((toolId) => (
    toolId === "computer_use"
    || toolId === "browser_computer"
    || toolId === "browser_use"
    || toolId === "browser_companion"
  ));
  const hasAttachedImages = attachedFiles.some((file) => String(file.type ?? "").startsWith("image/"));
  const imageBridgePlanned = hasAttachedImages && !selectedProfile?.supports_vision && !selectedProfile?.supports_image_input;
  const activeToolGroup = toolGroups.find((group) => group.id === openToolGroup) ?? toolGroups[0] ?? null;
  const showToolGroups = toolItems.length > 4;
  const isEscapedSlash = input.startsWith("//");
  const isSteerMode = isGenerating && !isNewConversation;
  const effectiveComposerPlaceholder = composerPlaceholderCopy({
    isSteerMode,
    mode,
    placeholder,
    templatePlaceholder: templateComposerPlaceholder,
  });
  const effectiveComposerHelp = composerHelperCopy({
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
  const matchedCommands = hasSlashCommandPrefix
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
      : commands.filter((command) => {
          const haystack = `${command.id} ${command.name} ${(command.aliases ?? []).join(" ")} ${command.label} ${command.description ?? ""}`.toLowerCase();
          return !slashQuery || haystack.includes(slashQuery);
        })
    : [];
  const activeCommandArgumentGuide = commandArgumentGuideForInput(input, commands);
  const hasModelCommandCandidates = textareaFocused && modelCommandCandidates.length > 0;
  const showCommandSuggestions = shouldShowComposerCommandSuggestions({
    focused: textareaFocused,
    slashCommandsEnabled: templateAllowsSlashCommands,
    hasModelCandidates: hasModelCommandCandidates,
    matchCount: activeCommandArgumentGuide ? 0 : matchedCommands.length,
  });
  const persistentToggleCommands = persistentComposerToggleCommands(commands).slice(0, 3);
  const visibleSteerPreviewItems = steerPreviewItems.filter((item) => (
    item.visible !== false && String(item.prompt ?? "").trim()
  ));
  const currentModeMeta = MODE_META[mode];
  const ModeIcon = currentModeMeta.icon;
  const directoryEntries = (codingContext?.entries ?? []).filter((entry) => entry.is_dir);
  const branchOptions = codingContext?.branches?.length ? codingContext.branches : codingContext?.branch ? [codingContext.branch] : [];
  const currentDirectory = codingContext?.directory || ".";
  const selectedCodingWorkspace = codingWorkspaces.find((workspace) => workspace.workspace_id === (selectedCodingWorkspaceId || codingContext?.workspaceId)) ?? codingWorkspaces[0] ?? null;
  const atMentionKnownValues = useMemo(() => [
    ...composerKnownMentionValues(toolItems),
    ...composerKnownMentionValues(skillExtensions),
    ...toolGroups.flatMap((service) => [service.id, service.label]),
    ...(mode === "coding" ? codingContext?.files ?? [] : []),
  ], [codingContext?.files, mode, skillExtensions, toolGroups, toolItems]);
  const atMentionCandidates = useMemo<ComposerAtMentionCandidate[]>(() => {
    const toolCandidates = filterComposerToolMentions(toolItems, atMentionQuery, 14).map((item) => {
      const display = composerToolMentionDisplay(item);
      return {
        kind: "tool" as const,
        id: `tool:${item.id}`,
        label: display.label,
        description: display.description,
        item,
      };
    });
    const skillCandidates = filterComposerSkillMentions(skillExtensions, atMentionQuery, 8).map((skill) => {
      const display = composerSkillMentionDisplay(skill);
      return {
        kind: "skill" as const,
        id: `skill:${skill.id}`,
        label: display.label,
        description: display.description,
        skill,
      };
    });
    const normalizedServiceQuery = atMentionQuery.trim().toLowerCase();
    const serviceCandidates = toolGroups
      .filter((service) => service.items.some((item) => !item.disabled))
      .filter((service) => (
        !normalizedServiceQuery
        || `${service.id} ${service.label} ${service.description}`
          .toLowerCase()
          .includes(normalizedServiceQuery)
      ))
      .slice(0, 8)
      .map((service) => ({
        kind: "service" as const,
        id: `service:${service.id}`,
        label: service.label,
        description: service.description,
        service,
      }));
    const fileCandidates = mode === "coding"
      ? filterAtMentionFiles(codingContext?.files ?? [], atMentionQuery).slice(0, 8).map((file) => ({
          kind: "file" as const,
          id: `file:${file}`,
          label: file,
          description: "workspace file",
          file,
        }))
      : [];
	    return [...toolCandidates, ...skillCandidates, ...serviceCandidates, ...fileCandidates];
	  }, [atMentionQuery, codingContext?.files, mode, skillExtensions, toolGroups, toolItems]);

  const atMentionPalette = useMemo(() => atMentionPalettePayload(atMentionCandidates), [atMentionCandidates]);
  const commandPalette = useMemo(() => commandPalettePayload(matchedCommands), [matchedCommands]);
  const commandArgumentPalette = useMemo(
    () => activeCommandArgumentGuide
      ? commandArgumentPalettePayload(activeCommandArgumentGuide)
      : null,
    [activeCommandArgumentGuide],
  );
  const activeComposerListboxId = atMentionOpen
    ? AT_MENTION_LISTBOX_ID
    : showCommandSuggestions
      ? COMMAND_LISTBOX_ID
      : commandArgumentPalette
        ? COMMAND_ARGUMENT_LISTBOX_ID
        : undefined;
  const activeComposerOptionId = atMentionOpen && atMentionCandidates.length > 0
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
    if (!attachmentMenuOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (attachmentMenuRef.current?.contains(target) || attachmentMenuButtonRef.current?.contains(target)) return;
      setAttachmentMenuOpen(false);
    };
    const handleDocumentKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setAttachmentMenuOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleDocumentKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleDocumentKeyDown);
    };
  }, [attachmentMenuOpen]);

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
    if (!templateAllowsSlashCommands) return;
    const protocolOption = matchedCommands.find((command) => command.id === commandId) as (
      ComposerCommandItem & { protocol_source_command_id?: string; protocol_option_value?: string }
    ) | undefined;
    if (protocolOption?.protocol_source_command_id && protocolOption.protocol_option_value) {
      const source = commands.find((command) => command.id === protocolOption.protocol_source_command_id);
      if (!source) return;
      onCommandSelect?.(source.id, `/${source.name} ${protocolOption.protocol_option_value}`);
      onInputChange("");
      return;
    }

    const command = commands.find((item) => item.id === commandId);
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
    onCommandSelect?.(commandId, rawInput);
    if (!(command?.protocol_presentation?.input.kind === "search_select" && rawHasArgs)) {
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
      if (!textarea || !textareaOwnsFocus || suppressPopovers || !templateAllowsAtMentions) {
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
    [atMentionKnownValues, suppressPopovers, templateAllowsAtMentions],
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
      onInputChange(value);
      onEntityReferencesChange?.(mergeComposerReferences(entityReferences, [], value));
      updateAtMentionStateFromInput(value);

      if (!templateAllowsSlashCommands || !value.startsWith("/") || value.startsWith("//")) {
        setSelectedCommandIndex(0);
      }
    },
    [entityReferences, onEntityReferencesChange, onInputChange, templateAllowsSlashCommands, updateAtMentionStateFromInput],
  );

  const handleAtMentionSelect = useCallback(
    (candidate: ComposerAtMentionCandidate) => {
      const textarea = textareaRef.current;
      if (!textarea) return;

      const cursorPos = atMentionStart === null
        ? textarea.selectionStart
        : atMentionStart + atMentionQuery.length + 1;
      const next = insertAtMentionText(input, cursorPos, candidate.label, atMentionKnownValues);
	      if (candidate.kind === "tool") {
	        onDropWidget?.(composerToolMentionWidget(candidate.item));
	      } else if (candidate.kind === "skill") {
	        onDropWidget?.(composerSkillMentionWidget(candidate.skill));
	      } else if (candidate.kind === "service") {
	        onDropWidget?.(composerServiceMentionWidget({
	          id: candidate.service.id,
	          label: candidate.service.label,
	          description: candidate.service.description,
	          toolIds: candidate.service.items.map((item) => item.id),
	        }));
	      } else {
	        onDropWidget?.(composerFileMentionWidget(candidate.file));
	      }
	      onInputChange(next.value);
	      if (candidate.kind !== "service") {
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
        textarea.setSelectionRange(next.cursor, next.cursor);
        textarea.focus();
      }, 0);
    },
		    [atMentionKnownValues, atMentionQuery.length, atMentionStart, entityReferences, input, mode, onAtFileAttach, onDropWidget, onEntityReferencesChange, onInputChange],
		  );

  const attachFiles = useCallback(async (files: FileList | File[] | null) => {
    if (!files?.length) return;
    if (!templateAllowsFileAttachments) return;
    const newFiles: AttachedFile[] = await Promise.all(Array.from(files).map(fileToAttachment));
    onFileAttach?.(newFiles);
  }, [onFileAttach, templateAllowsFileAttachments]);

  const handleCopy = useCallback((event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const textarea = event.currentTarget;
    const selectedText = input.slice(textarea.selectionStart, textarea.selectionEnd);
    const serialized = serializeComposerReferences(selectedText, entityReferences);
    if (!serialized) return;
    event.preventDefault();
    event.clipboardData.setData("text/plain", composerReferencesAsMarkdown(selectedText, entityReferences));
    event.clipboardData.setData(COMPOSER_REFERENCE_MIME, serialized);
  }, [entityReferences, input]);

  const handlePaste = useCallback((event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const files = composerClipboardFiles(event.clipboardData);
    if (files.length > 0) {
      event.preventDefault();
      void attachFiles(files);
      return;
    }
    const raw = event.clipboardData.getData(COMPOSER_REFERENCE_MIME);
    const catalog = {
      tools: toolItems,
      skills: skillExtensions,
      files: mode === "coding" ? codingContext?.files ?? [] : [],
    };
    const restored = raw
      ? restoreComposerReferences(raw, catalog)
      : restoreComposerMarkdownReferences(event.clipboardData.getData("text/plain"), catalog);
    if (!restored) return;
    event.preventDefault();
    const textarea = event.currentTarget;
    const next = insertComposerReferencePaste(input, textarea.selectionStart, textarea.selectionEnd, restored);
    onInputChange(next.value);
    onEntityReferencesChange?.(mergeComposerReferences(entityReferences, next.references, next.value));
    for (const reference of next.references) {
      if (reference.kind === "tool") {
        const item = toolItems.find((candidate) => candidate.id === reference.id);
        if (item) onDropWidget?.(composerToolMentionWidget(item, reference.syntax));
      } else if (reference.kind === "skill") {
        const skill = skillExtensions.find((candidate) => candidate.id === reference.id);
        if (skill) onDropWidget?.(composerSkillMentionWidget(skill, reference.syntax));
      } else if (mode === "coding") {
        onDropWidget?.(composerFileMentionWidget(reference.id, reference.syntax));
        onAtFileAttach?.(reference.id);
      }
    }
    setTimeout(() => {
      textarea.setSelectionRange(next.cursor, next.cursor);
      textarea.focus();
    }, 0);
  }, [attachFiles, codingContext?.files, entityReferences, input, mode, onAtFileAttach, onDropWidget, onEntityReferencesChange, onInputChange, skillExtensions, toolItems]);

  const requestAudioTranscript = useCallback(async (
    file: AttachedFile,
    metadata: Record<string, unknown>,
  ): Promise<string> => {
    return requestComposerAudioTranscript(file, {
      profile: selectedProfile,
      language: "ja",
      metadata,
    });
  }, [selectedProfile]);

  const transcribeAttachedAudio = useCallback(async (file: AttachedFile) => {
    const transcript = await requestAudioTranscript(file, {
      action: "replace_audio_attachment_with_transcript",
      source_attachment_id: file.id,
    });
    const transcriptFile = transcriptAttachmentFromAudio(file, transcript);
    onFileRemove?.(file.id);
    onFileAttach?.([transcriptFile]);
  }, [onFileAttach, onFileRemove, requestAudioTranscript]);

  const handleDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      if (event.dataTransfer.files.length > 0) {
        void attachFiles(event.dataTransfer.files);
        return;
      }

      const historyData = event.dataTransfer.getData(HISTORY_CHAT_DROP_MIME);
      if (historyData) {
        const widget = parseHistoryChatDrop(historyData);
        if (widget) onDropWidget?.(widget);
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
    [attachFiles, onDropWidget, requestModelProfileSelect, toolItems],
  );

  const handleDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  }, []);

  const handleSubmitWithApiKeyGuard = useCallback(
    (event: React.SyntheticEvent) => {
      event.preventDefault();
      if (isGenerating) {
        const prompt = input.trim();
        if (prompt && !steerBusy) {
          onSteerSubmit?.(prompt);
        } else if (!prompt) {
          onStopGenerating?.();
        }
        return;
      }
      if (pendingMentionAttachmentPaths.length > 0) return;
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
    [attachedFiles, input, isGenerating, needsApiKey, onStopGenerating, onSteerSubmit, onSubmit, pendingMentionAttachmentPaths.length, selectedProfile, steerBusy],
  );

  const handleSendButtonClick = useCallback(
    (event: React.MouseEvent<HTMLButtonElement>) => {
      if (!isGenerating) return;
      handleSubmitWithApiKeyGuard(event);
    },
    [handleSubmitWithApiKeyGuard, isGenerating],
  );

  useEffect(() => {
    if (voiceStatus !== "listening") return;
    const timer = window.setInterval(() => {
      setVoiceElapsedSeconds(Math.max(0, Math.floor((performance.now() - voiceStartedAtRef.current) / 1000)));
    }, 250);
    return () => window.clearInterval(timer);
  }, [voiceStatus]);

  useEffect(() => () => {
    voiceRecorderRef.current?.cancel();
    voiceRecorderRef.current = null;
  }, []);

  const cancelVoiceInput = useCallback(() => {
    voiceRecorderRef.current?.cancel();
    voiceRecorderRef.current = null;
    setVoiceElapsedSeconds(0);
    setVoiceError(null);
    setVoiceStatus("idle");
    window.setTimeout(() => textareaRef.current?.focus({ preventScroll: true }), 0);
  }, []);

  const stopAndTranscribeVoice = useCallback(async () => {
    const recorder = voiceRecorderRef.current;
    if (!recorder) return;
    voiceRecorderRef.current = null;
    setVoiceStatus("transcribing");
    setVoiceError(null);
    let recording: AmbientAudioRecording | null = null;
    try {
      recording = await recorder.stop();
      const audioFile: AttachedFile = {
        id: `voice-${Date.now()}`,
        name: `voice-${new Date().toISOString().replace(/[:.]/g, "-")}.${recording.extension}`,
        size: recording.size,
        type: recording.mimeType,
        dataUrl: recording.dataUrl,
      };
      if (modelSupportsAudioInput(selectedProfile)) {
        onFileAttach?.([audioFile]);
        setVoiceStatus("idle");
        setVoiceElapsedSeconds(0);
        return;
      }
      const transcript = await requestAudioTranscript(audioFile, {
        duration_ms: recording.durationMs,
        action: "automatic_transcription_for_unsupported_model",
        voice_input_use_ai: voiceInputUseAi,
      });
      const prefix = voiceInputUseAi ? "文字起こしして: " : "";
      const base = input.trimEnd();
      onInputChange(`${base}${base ? "\n" : ""}${prefix}${transcript}`);
      setVoiceStatus("idle");
      setVoiceElapsedSeconds(0);
    } catch (error) {
      const message = error instanceof Error ? error.message : "音声入力に失敗しました";
      if (recording && onFileAttach) {
        onFileAttach([{
          id: `voice-${Date.now()}`,
          name: `voice-${new Date().toISOString().replace(/[:.]/g, "-")}.${recording.extension}`,
          size: recording.size,
          type: recording.mimeType,
          dataUrl: recording.dataUrl,
        }]);
        setVoiceError(`${message}。録音をファイルとして添付しました。`);
      } else {
        setVoiceError(message);
      }
      setVoiceStatus("error");
    } finally {
      window.setTimeout(() => textareaRef.current?.focus({ preventScroll: true }), 0);
    }
  }, [input, onFileAttach, onInputChange, requestAudioTranscript, selectedProfile, voiceInputUseAi]);

  const toggleVoiceInput = useCallback(async () => {
    if (!voiceInputEnabled || !templateAllowsVoiceInput) return;
    if (voiceStatus === "listening") {
      await stopAndTranscribeVoice();
      return;
    }
    if (voiceStatus === "starting" || voiceStatus === "transcribing") return;
    setVoiceStatus("starting");
    setVoiceError(null);
    try {
      const recorder = await startPinchAudioRecorder();
      voiceRecorderRef.current = recorder;
      voiceStartedAtRef.current = performance.now();
      setVoiceElapsedSeconds(0);
      setVoiceStatus("listening");
    } catch (error) {
      setVoiceError(error instanceof Error ? error.message : "マイクを開始できませんでした");
      setVoiceStatus("error");
    }
  }, [stopAndTranscribeVoice, templateAllowsVoiceInput, voiceInputEnabled, voiceStatus]);

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
        event.stopPropagation();
        return;
      }

      if (isComposerImeEvent(event)) return;

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
        const composerOwnsEscape = atMentionOpen
          || hasSlashCommandPrefix
          || hasModelCommandCandidates
          || menuOpen
          || modelDropdownOpen
          || modeSelectorOpen
          || openModelStatusId !== null;
        if (composerOwnsEscape) {
          event.preventDefault();
          event.stopPropagation();
          if (atMentionOpen) {
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
          setMenuOpen(false);
          setModelDropdownOpen(false);
          setModeSelectorOpen(false);
          setOpenModelStatusId(null);
          onModelCommandCandidatesClose?.();
          if (hasSlashCommandPrefix) onInputChange("");
          return;
        }
      }

      if (atMentionOpen) {
        const action = atMentionMenuKeyAction(
          event.key,
          event.shiftKey,
          selectedAtMentionIndex,
          atMentionCandidates.length,
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
      atMentionCandidates,
      atMentionKnownValues,
      atMentionOpen,
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
      visible: templateAllowsFileAttachments,
      width: COMPOSER_CHROME_WIDTHS.icon,
      className: "relative overflow-visible",
      render: () => (
        <>
          <button
            ref={attachmentMenuButtonRef}
            type="button"
            tabIndex={chromeButtonTabIndex}
            aria-label="ファイルを添付"
            aria-expanded={attachmentMenuOpen}
            disabled={!templateAllowsFileAttachments}
            title="写真とファイルを追加"
            onClick={() => setAttachmentMenuOpen((open) => !open)}
            className="rumi-icon-button text-zinc-300"
          >
            <Plus aria-hidden="true" size={isNewConversation ? 24 : 20} strokeWidth={1.8} />
          </button>
          {attachmentMenuOpen && (
            <div
              ref={attachmentMenuRef}
              role="menu"
              aria-label="添付メニュー"
              className="rumi-attachment-menu rumi-popover absolute left-0 top-full rumi-layer-modal mt-2 w-[min(360px,calc(100vw-32px))] p-2"
            >
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setAttachmentMenuOpen(false);
                  fileInputRef.current?.click();
                }}
                className="flex min-h-12 w-full items-center gap-3 rounded-xl px-3 text-left text-sm text-zinc-100 transition-colors hover:bg-white/[0.06]"
              >
                <span className="flex h-8 w-8 items-center justify-center rounded-lg border border-white/[0.08] bg-white/[0.04] text-zinc-200">
                  <CloudUpload aria-hidden="true" size={17} />
                </span>
                <span className="min-w-0">
                  <span className="block font-medium">写真とファイルを追加</span>
                  <span className="block text-xs text-zinc-500">コンピューターからアップロード</span>
                </span>
              </button>
            </div>
          )}
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
	          aria-label={isVoiceListening ? "音声入力を停止" : "音声入力を開始"}
		          disabled={!voiceInputEnabled || !templateAllowsVoiceInput || voiceStatus === "starting" || voiceStatus === "transcribing"}
		          title={isVoiceListening ? "録音を停止して文字起こし" : voiceStatus === "transcribing" ? "文字起こし中" : voiceInputUseAi ? "音声入力（AI文字起こし）" : "音声入力"}
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
      width: { basis: "auto", min: "4rem", max: "8.5rem", shrink: 1 },
      className: "rumi-composer-dock-control",
      render: () => (
        <ActionApprovalControl
          mode={actionApprovalMode}
          disabled={isGenerating}
          surfaceClassName={COMPOSER_CONTROL_SURFACE_CLASSNAME}
          tabIndex={chromeButtonTabIndex}
          onModeChange={(nextMode) => onActionApprovalModeChange?.(nextMode)}
          onOpenSettings={onOpenToolSettings}
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
          selectedProjectId={selectedProjectId}
          disabled={isGenerating}
          codingWorkspaces={codingWorkspaces}
          onSelect={(project) => onProjectSelect?.(project)}
          onDirectorySelect={onProjectDirectorySelect}
          onCodingWorkspaceCreate={onCodingWorkspaceCreate}
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
      id: "vision-bridge-status",
      slot: "leading",
      homeSlot: "toolbar-leading",
      order: 70,
      visible: imageBridgePlanned,
      width: COMPOSER_CHROME_WIDTHS.badge,
      className: "overflow-hidden",
      render: () => (
        <span aria-label="Vision Bridge" title="Vision Bridge" className="inline-flex items-center gap-1 rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-[11px] font-medium text-sky-300 max-[430px]:px-1.5">
          <FileText size={12} className="flex-shrink-0" />
          <span className="truncate max-[430px]:hidden">Vision Bridge</span>
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
                profiles={selectableProfiles}
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
          type={isGenerating ? "button" : "submit"}
          onClick={handleSendButtonClick}
          tabIndex={chromeButtonTabIndex}
          aria-label={isGenerating
            ? (input.trim() ? "追加指示を送る" : "生成を停止")
            : pendingMentionAttachmentPaths.length > 0
              ? "ファイルを読み込み中"
              : "メッセージを送信"}
          disabled={!isGenerating && (
            pendingMentionAttachmentPaths.length > 0
            || (!input.trim() && attachedFiles.length === 0)
          )}
          title={isGenerating
            ? (input.trim() ? "追加指示を送る" : "停止")
            : pendingMentionAttachmentPaths.length > 0
              ? "ファイルを読み込み中"
              : "送信"}
          className={`rumi-send-button flex flex-shrink-0 items-center justify-center rounded-full transition-all duration-150 disabled:cursor-not-allowed ${
            "h-[44px] min-h-[44px] w-[44px] min-w-[44px]"
          } ${
            isGenerating
              ? input.trim()
                ? "bg-zinc-100 text-zinc-950 hover:bg-white"
                : "bg-zinc-100 text-zinc-900 hover:bg-white"
              : pendingMentionAttachmentPaths.length > 0 || (!input.trim() && attachedFiles.length === 0)
                ? "bg-white/[0.06] text-zinc-500"
                : "bg-zinc-100 text-zinc-950 shadow-[0_6px_18px_rgba(0,0,0,0.28)] hover:bg-white"
          }`}
        >
          {isGenerating && !input.trim() ? (
            <Square size={11} strokeWidth={2.4} fill="currentColor" aria-hidden="true" />
          ) : isGenerating ? (
            <CornerDownRight size={15} strokeWidth={2.4} />
          ) : (
            <SendButtonIcon size={isNewConversation ? 18 : 16} />
          )}
        </button>
      ),
    },
  ];

  const conversationFileAttachWidget = chromeWidgets.find((widget) => widget.id === "file-attach" && widget.visible !== false);
  const leadingChromeWidgets = composerChromeWidgetsForSlot(chromeWidgets, "leading")
    .filter((widget) => widget.id !== "file-attach");
  const trailingChromeWidgets = composerChromeWidgetsForSlot(chromeWidgets, "trailing");
  const newConversationInlineLeadingWidgets = composerChromeWidgetsForHomeSlot(chromeWidgets, "editor-leading");
  const newConversationTopRightWidgets = composerChromeWidgetsForHomeSlot(chromeWidgets, "editor-trailing");
  const newConversationInlineActionWidgets = composerChromeWidgetsForHomeSlot(chromeWidgets, "toolbar-leading");
  const newConversationTrailingWidgets = composerChromeWidgetsForHomeSlot(chromeWidgets, "toolbar-trailing");
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

  return (
    <div
      className={`${isNewConversation ? "w-full px-4" : "rumi-composer-dock px-4 pb-3 pt-2 bg-[#09090b] flex-shrink-0 max-[640px]:px-2 max-[640px]:pb-2"}`}
      onDrop={handleDrop}
      onDragOver={handleDragOver}
    >
      <div className={`rumi-composer-shell ${isNewConversation ? "rumi-composer-shell-new mx-auto" : "mx-auto"}`}>
        <RuntimeCapabilityBanner
          visible={imageBridgePlanned}
          onSwitchToVisionModel={onSwitchToVisionModel}
          onOpenModelManager={onOpenModelManager}
          onOpenToolSettings={onOpenToolSettings}
        />
        <form
          onSubmit={handleSubmitWithApiKeyGuard}
          className={`rumi-composer-frame ${
            isNewConversation
              ? "rumi-composer-new border-transparent bg-transparent"
              : "rounded-2xl max-[640px]:rounded-2xl"
          } relative flex flex-col border overflow-visible`}
        >
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
            <JsonListPanel
              payload={commandPalette}
              activeIndex={selectedCommandIndex}
              onActiveIndexChange={setSelectedCommandIndex}
              onSelect={(index) => chooseCommand(matchedCommands[index].id)}
            />
          )}

          {commandArgumentPalette && (
            <JsonListPanel
              payload={commandArgumentPalette}
              activeIndex={0}
              onActiveIndexChange={() => undefined}
              onSelect={() => textareaRef.current?.focus({ preventScroll: true })}
            />
          )}

          {atMentionOpen && (
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
            <div className="rumi-voice-capture mx-3 mt-2 flex min-h-11 items-center gap-3 rounded-xl border border-white/[0.09] bg-white/[0.035] px-3 py-2" role="status" aria-live="polite">
              <span className={`flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full ${voiceStatus === "error" ? "bg-rose-500/10 text-rose-300" : "bg-white/[0.06] text-zinc-100"}`}>
                {voiceStatus === "starting" || voiceStatus === "transcribing" ? <Loader2 size={15} className="animate-spin" /> : <WarmActionIcon kind="mic" size="sm" />}
              </span>
              <span className="min-w-0 flex-1">
                {voiceStatus === "listening" ? (
                  <span className="flex items-center gap-3">
                    <span className="rumi-voice-waveform" aria-hidden="true">
                      {Array.from({ length: 18 }, (_, index) => <i key={index} style={{ animationDelay: `${(index % 6) * -90}ms` }} />)}
                    </span>
                    <span className="flex-shrink-0 font-mono text-[11px] tabular-nums text-zinc-400">{formatVoiceDuration(voiceElapsedSeconds)}</span>
                  </span>
                ) : (
                  <span className={`block truncate text-[11px] ${voiceStatus === "error" ? "text-rose-200" : "text-zinc-400"}`}>
                    {voiceStatus === "starting" ? "マイクを準備中..." : voiceStatus === "transcribing" ? "音声を文字起こし中..." : voiceError || "音声入力に失敗しました"}
                  </span>
                )}
              </span>
              {voiceStatus === "listening" && (
                <button
                  type="button"
                  aria-label="録音を停止して文字起こし"
                  title="録音を停止して文字起こし"
                  onClick={() => void stopAndTranscribeVoice()}
                  className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-zinc-100 text-zinc-950 transition-transform hover:scale-105"
                >
                  <Square size={10} fill="currentColor" />
                </button>
              )}
              {(voiceStatus === "listening" || voiceStatus === "error") && (
                <button
                  type="button"
                  aria-label={voiceStatus === "listening" ? "録音をキャンセル" : "音声エラーを閉じる"}
                  title={voiceStatus === "listening" ? "キャンセル" : "閉じる"}
                  onClick={cancelVoiceInput}
                  className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full text-zinc-500 transition-colors hover:bg-white/[0.06] hover:text-zinc-100"
                >
                  <X size={14} />
                </button>
              )}
            </div>
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
                  {steerStatus && <span className="truncate">{steerStatus}</span>}
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

          {!isNewConversation && visibleToolSelectionTargets.length > 0 && (
            <ToolOverrideChips
              targets={visibleToolSelectionTargets}
              labelForTarget={labelForToolTarget}
              onRemove={(target) => onToolSelectionTargetRemove?.(target)}
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
                      data-template-composer-input={templateComposerInputId || undefined}
                      onChange={(event) => {
                        resizeComposerTextarea(event.currentTarget);
                        handleInputChange(event.currentTarget.value);
                      }}
                      placeholder={effectiveComposerPlaceholder}
                      aria-label="Rumiにメッセージを送信"
                      aria-autocomplete="list"
                      aria-controls={activeComposerListboxId}
                      aria-activedescendant={activeComposerOptionId}
                      aria-expanded={atMentionOpen || showCommandSuggestions || Boolean(commandArgumentPalette)}
                      role="combobox"
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
                onFileRemove={onFileRemove}
                onPendingRemove={onPendingMentionAttachmentRemove}
                onTranscribe={transcribeAttachedAudio}
              />
              <div className={`grid min-w-0 items-end gap-1 px-2 ${conversationFileAttachWidget ? "grid-cols-[44px_minmax(0,1fr)]" : "grid-cols-1"}`}>
                {conversationFileAttachWidget && (
                  <div className="self-end pb-0.5">
                    <ComposerChromeWidget widget={conversationFileAttachWidget} />
                  </div>
                )}
                <div className="relative min-w-0">
                  {hasInlineMentions && (
                    <div
                      ref={inlineMentionLayerRef}
                      aria-hidden="true"
                      data-composer-inline-mentions
                      className={`rumi-composer-inline-mention-layer absolute inset-0 overflow-hidden whitespace-pre-wrap break-words px-2 pb-0 pt-2.5 text-[15px] leading-[22px] text-zinc-100 max-[640px]:pb-0 max-[640px]:pt-2.5 max-[640px]:text-[13px] ${textareaCanCollapse ? "pr-11 max-[640px]:pr-10" : ""}`}
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
                    data-template-composer-input={templateComposerInputId || undefined}
                    onChange={(event) => {
                      resizeComposerTextarea(event.currentTarget);
                      handleInputChange(event.currentTarget.value);
                    }}
                    placeholder={effectiveComposerPlaceholder}
                    aria-label="Rumiにメッセージを送信"
                    aria-autocomplete="list"
                    aria-controls={activeComposerListboxId}
                    aria-activedescendant={activeComposerOptionId}
                    aria-expanded={atMentionOpen || showCommandSuggestions || Boolean(commandArgumentPalette)}
                    role="combobox"
                    className={`rumi-composer-textarea relative min-h-[24px] w-full max-h-[240px] select-text resize-none overflow-x-hidden overflow-y-auto border-none bg-transparent px-2 pb-0 pt-2.5 text-[15px] leading-[22px] caret-zinc-100 outline-none placeholder:text-zinc-500/70 max-[640px]:min-h-[24px] max-[640px]:pb-0 max-[640px]:pt-2.5 max-[640px]:text-[13px] ${hasInlineMentions ? "rumi-composer-textarea-highlighted text-transparent" : "text-zinc-100"} ${textareaCanCollapse ? "pr-11 max-[640px]:pr-10" : ""}`}
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

          {!isSteerMode && (effectiveComposerHelp || templateComposerInfoItems.length > 0) && (
            <div className={`${isNewConversation ? "px-5 pt-1" : "px-5 pt-1 max-[640px]:px-3"} flex min-h-5 flex-wrap items-center gap-1.5 text-[10px] leading-none text-zinc-500`}>
              {effectiveComposerHelp && (
                <span className="min-w-[12rem] flex-1 break-words line-clamp-2" title={effectiveComposerHelp}>
                  {effectiveComposerHelp}
                </span>
              )}
              {!isNewConversation && templateComposerInfoItems.map((item) => (
                <span
                  key={item}
                  className="flex-shrink-0 rounded-full bg-white/[0.04] px-2 py-0.5 text-[9px] uppercase tracking-wide text-zinc-500"
                >
                  {item}
                </span>
              ))}
            </div>
          )}

          {isSteerMode && (
            <div className="flex min-h-6 flex-wrap items-center gap-x-2 gap-y-1 px-4 pt-1.5 text-[10px] leading-4 text-zinc-500 max-[640px]:px-3">
              <CornerDownRight size={12} className="flex-shrink-0" />
              <span className="min-w-[10rem] flex-1 break-words line-clamp-2">
                {effectiveComposerHelp}
              </span>
              {steerBusy && <Loader2 size={11} className="flex-shrink-0 animate-spin" />}
              {steerQueuedCount > 0 && (
                <span className="flex-shrink-0 rounded-full border border-zinc-700 px-1.5 py-0.5 text-[9px] leading-none">
                  {steerQueuedCount}件待機
                </span>
              )}
              {steerStatus && (
                <span className="min-w-[8rem] flex-1 break-words text-zinc-500">{steerStatus}</span>
              )}
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

          {!isNewConversation && (
            <div className="px-3 pb-2.5 pt-1 flex items-center justify-between gap-2 max-[640px]:gap-1.5 max-[640px]:px-2 max-[640px]:pb-1.5">
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
        </form>
      </div>
    </div>
  );
}
