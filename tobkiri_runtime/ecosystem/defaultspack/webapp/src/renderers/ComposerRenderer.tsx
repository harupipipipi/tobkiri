import {
  ArrowUp,
  BrainCircuit,
  ChevronDown,
  Code2,
  CornerDownRight,
  Cpu,
  File,
  FileText,
  Folder,
  GitBranch,
  KeyRound,
  Loader2,
  MessageSquare,
  MousePointerClick,
  PanelRightOpen,
  Search,
  SlidersHorizontal,
  Wrench,
  X,
} from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
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
import { ActionApprovalControl } from "../features/tools/ActionApprovalControl";
import { ToolOverrideChips } from "../features/tools/ToolOverrideChips";
import { ToolSelectionReviewCard } from "../features/tools/ToolSelectionReviewCard";
import { fileToAttachment } from "../lib/attachments";
import { composerFileMentionWidget, composerKnownMentionValues, composerServiceMentionWidget, composerSkillMentionDisplay, composerSkillMentionWidget, composerToolMentionDisplay, composerToolMentionWidget, filterComposerSkillMentions, filterComposerToolMentions, resolveComposerWidgetDrop, skillMentionIdsFromText, toolMentionIdsFromText } from "../lib/composerWidgets";
import { COMPOSER_REFERENCE_MIME, insertComposerReferencePaste, mergeComposerReferences, restoreComposerReferences, serializeComposerReferences, type ComposerEntityReference } from "../lib/composerReferences";
import { HISTORY_CHAT_DROP_MIME, parseHistoryChatDrop } from "../lib/historyComposer";
import { activeMentionAtCursor } from "../lib/mentionContract";
import { sortedToolGroups, toolGroupFor } from "../lib/toolUi";

export { composerSkillMentionDisplay, composerSkillMentionWidget, composerToolMentionDisplay, composerToolMentionWidget, filterComposerSkillMentions, filterComposerToolMentions, resolveComposerWidgetDrop, skillMentionIdsFromText, toolMentionIdsFromText } from "../lib/composerWidgets";

const THINKING_LABELS: Record<string, string> = {
  none: "なし",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "最高",
};

const RISK_BADGE_STYLES: Record<string, string> = {
  low: "border-emerald-500/20 text-emerald-300",
  medium: "border-amber-500/25 text-amber-300",
  high: "border-rose-500/30 text-rose-300",
};

const MODE_META: Record<AppMode, { label: string; icon: typeof MessageSquare; description: string }> = {
  chat: { label: "Chat", icon: MessageSquare, description: "通常チャット" },
  coding: { label: "Coding", icon: Code2, description: "コード編集・Git操作" },
  agent: { label: "Agent", icon: Cpu, description: "自律エージェント" },
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

type ComposerReferenceDescriptor = ComposerEntityReference & {
  label: string;
};

type SpeechRecognitionEventLike = {
  resultIndex: number;
  results: ArrayLike<{
    isFinal?: boolean;
    0?: { transcript?: string };
  }>;
};

type SpeechRecognitionLike = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};

const COMPOSER_CHROME_WIDTHS = {
  icon: { basis: "2.75rem", min: "2.75rem", max: "2.75rem" },
  mode: { basis: "auto", min: "2rem", max: "7rem", shrink: 1 },
  badge: { basis: "auto", min: "0", max: "11rem", shrink: 1 },
  thinking: { basis: "5.25rem", min: "5.25rem", max: "5.25rem", shrink: 0 },
  status: { basis: "auto", min: "2.5rem", shrink: 0 },
  send: { basis: "2.75rem", min: "2.75rem", max: "2.75rem" },
  sendLarge: { basis: "2.75rem", min: "2.75rem", max: "2.75rem" },
} satisfies Record<string, ComposerChromeWidth>;

const COMPOSER_CONTROL_SURFACE_CLASSNAME = "rumi-composer-control-surface flex h-11 min-w-0 items-center rounded-[1rem] border border-zinc-700/40 bg-zinc-800/40 px-2.5";
const AT_MENTION_LISTBOX_ID = "composer-at-mention-listbox";
const COMPOSER_MODEL_CONTROL_MIN_CH = 9;
const COMPOSER_MODEL_CONTROL_MAX_CH = 18;
const COMPOSER_MODEL_CONTROL_CHROME_CH = 6;
const NEW_CONVERSATION_TEXTAREA_MIN_HEIGHT = 22;
const NEW_CONVERSATION_TEXTAREA_MAX_HEIGHT = 150;
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

function SendButtonIcon({ className = "" }: { className?: string }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 100 100"
      className={className}
    >
      <rect width="100" height="100" fill="#212121" rx="8" />
      <circle cx="50" cy="50" r="40" fill="#ffffff" />
      <path
        d="M 50 35 L 50 65 M 35 50 L 50 35 L 65 50"
        fill="none"
        stroke="#000000"
        strokeWidth="6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
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
        className={`relative flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-lg transition-transform hover:scale-[1.04] ${tone.icon}`}
      >
        <span
          aria-hidden="true"
          className="block h-[18px] w-[18px]"
          dangerouslySetInnerHTML={{ __html: inlineSvgMarkup(indicator.svgMarkup) }}
        />
      </button>
      {!open && (
        <div className="pointer-events-none absolute bottom-full right-0 rumi-layer-local-popover mb-2 w-max max-w-[220px] rounded-lg border border-zinc-800 bg-zinc-950/95 px-2 py-1 text-[10px] leading-snug text-zinc-300 opacity-0 shadow-xl transition-opacity group-hover/status:opacity-100">
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

function renderComposerReferenceText(
  value: string,
  references: ComposerReferenceDescriptor[],
  keyPrefix = "",
): ReactNode[] {
  const nodes: ReactNode[] = [];
  const ranges = references
    .map((reference) => ({ reference, start: value.indexOf(reference.syntax) }))
    .filter((item) => item.start >= 0)
    .sort((left, right) => left.start - right.start);
  let lastIndex = 0;

  for (const { reference, start } of ranges) {
    if (start < lastIndex) continue;
    if (start > lastIndex) nodes.push(value.slice(lastIndex, start));
    const Icon = reference.kind === "tool" ? Wrench : reference.kind === "skill" ? BrainCircuit : FileText;
    nodes.push(
      <span
        key={`${keyPrefix}${reference.kind}:${reference.id}:${start}`}
        data-composer-inline-reference={`${reference.kind}:${reference.id}`}
        className="mx-[1px] inline-flex h-[20px] max-w-full translate-y-[2px] items-center gap-1 rounded bg-sky-500/15 px-1.5 text-sky-200 ring-1 ring-inset ring-sky-400/30"
      >
        <Icon aria-hidden="true" size={11} className="flex-shrink-0 text-sky-300" />
        <span className="truncate">{reference.label}</span>
      </span>,
    );
    lastIndex = start + reference.syntax.length;
  }

  if (lastIndex < value.length) nodes.push(value.slice(lastIndex));
  return nodes;
}

function ComposerReferenceOverlay({
  value,
  references,
  cursorPosition,
  showCaret,
  scrollTop,
  className,
}: {
  value: string;
  references: ComposerReferenceDescriptor[];
  cursorPosition: number;
  showCaret: boolean;
  scrollTop: number;
  className: string;
}) {
  if (!value || references.length === 0) return null;
  const caretIndex = Math.max(0, Math.min(cursorPosition, value.length));
  return (
    <div
      aria-hidden="true"
      className={`rumi-composer-reference-overlay pointer-events-none absolute inset-0 overflow-hidden whitespace-pre-wrap break-words text-zinc-100 ${className}`}
    >
      <div style={{ transform: `translateY(-${scrollTop}px)` }}>
        {renderComposerReferenceText(value.slice(0, caretIndex), references, "before-")}
        {showCaret && <span className="rumi-composer-fake-caret inline-block h-[1em] w-px translate-y-[2px] bg-zinc-100" />}
        {renderComposerReferenceText(value.slice(caretIndex), references, "after-")}
      </div>
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

function thinkingCommandMatch(input: string): { query: string } | null {
  const match = input.trimStart().match(/^\/(?:think|thinking|t)(?:\s+(\S*))?$/i);
  if (!match) return null;
  return { query: String(match[1] ?? "").toLowerCase() };
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

export function filterModelProfilesBySearch(profiles: ModelProfile[], search: string): ModelProfile[] {
  const rawTokens = search.trim().split(/\s+/).filter(Boolean);
  if (rawTokens.length === 0) return profiles;

  const providerTokens = rawTokens
    .filter((token) => token.startsWith("@"))
    .map(normalizeProviderSearchToken)
    .filter(Boolean);
  const textTokens = rawTokens
    .filter((token) => !token.startsWith("@"))
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

function FileChip({ file, onRemove }: { file: AttachedFile; onRemove?: (id: string) => void }) {
  const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
  const icon = /^(md|txt|json|yaml|yml|toml|ini|cfg)$/.test(ext) ? <FileText size={12} /> : <File size={12} />;
  return (
    <span className="inline-flex items-center gap-1 rounded-md border border-zinc-700/60 bg-zinc-800/70 px-2 py-0.5 text-[11px] text-zinc-300 max-w-[160px]">
      {icon}
      <span className="truncate">{file.name}</span>
      {onRemove && (
        <button
          type="button"
          aria-label={`${file.name} を削除`}
          onClick={() => onRemove(file.id)}
          className="relative -my-2.5 -mr-2 ml-0.5 flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-md text-zinc-500 hover:text-zinc-200 focus:outline focus:outline-2 focus:outline-offset-2 focus:outline-sky-400 focus-visible:ring-2 focus-visible:ring-sky-400"
        >
          <X size={10} />
        </button>
      )}
    </span>
  );
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
      className="inline-flex max-w-[220px] items-center gap-1.5 rounded-md border border-blue-500/30 bg-blue-500/10 px-2 py-1 text-[11px] text-blue-100"
    >
      <Loader2 size={12} className="flex-shrink-0 animate-spin" />
      <span className="truncate">{name} を読み込み中</span>
      {onRemove && (
        <button
          type="button"
          aria-label={`${name} の読み込みを取り消す`}
          onClick={() => onRemove(path)}
          className="relative -my-2.5 -mr-2 ml-0.5 flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-md text-blue-200/60 hover:text-blue-100 focus:outline focus:outline-2 focus:outline-offset-2 focus:outline-sky-300 focus-visible:ring-2 focus-visible:ring-sky-300"
        >
          <X size={10} />
        </button>
      )}
    </span>
  );
}

function FilePreviewCard({ file, onRemove }: { file: AttachedFile; onRemove?: (id: string) => void }) {
  const ext = file.name.split(".").pop()?.toUpperCase() || "FILE";
  const lineCount = file.content ? file.content.split(/\r\n|\r|\n/).length : null;
  const isImage = /^image\//.test(file.type ?? "");
  return (
    <div className="group/file relative h-[120px] w-[148px] flex-shrink-0 rounded-xl border border-zinc-600/70 bg-[#272728] p-3 shadow-sm">
      <div className="flex h-full flex-col justify-between">
        <div className="min-w-0">
          <p className="truncate text-[15px] font-medium text-zinc-100">{file.name}</p>
          <p className="mt-1 text-[12px] text-zinc-500">
            {lineCount ? `${lineCount}行` : `${Math.max(1, Math.ceil(file.size / 1024))} KB`}
          </p>
        </div>
        <div className="inline-flex h-7 w-fit items-center rounded-md border border-zinc-500/60 px-2 text-[13px] font-semibold text-zinc-300">
          {isImage ? "IMG" : ext.slice(0, 4)}
        </div>
      </div>
      {onRemove && (
        <button
          type="button"
          aria-label={`${file.name} を削除`}
          onClick={() => onRemove(file.id)}
          className="absolute right-0 top-0 flex h-11 w-11 items-center justify-center rounded-xl bg-zinc-950/80 text-zinc-400 transition-opacity hover:text-zinc-100 focus:outline focus:outline-2 focus:outline-offset-[-2px] focus:outline-sky-400 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-sky-400"
          title="削除"
        >
          <X size={12} />
        </button>
      )}
    </div>
  );
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
    return (
      <button
        type="button"
        title={widget.description ?? widget.label}
        onClick={() => onToggle?.(widget.id)}
        className={`inline-flex max-w-[220px] items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] transition-colors ${
          widget.enabled === false
            ? "border-zinc-700/60 bg-zinc-800/50 text-zinc-500"
            : "border-amber-500/30 bg-amber-500/10 text-amber-200 hover:bg-amber-500/15"
        }`}
      >
        <MessageSquare size={10} />
        <span className="truncate">{widget.label}</span>
      </button>
    );
  }

  if (widget.widgetKind !== "tool_toggle" && widget.type !== "tool") {
    const Icon = widget.widgetKind === "button"
      ? MousePointerClick
      : widget.widgetKind === "selector"
        ? SlidersHorizontal
        : PanelRightOpen;
    return (
      <button
        type="button"
        title={widget.description ?? widget.label}
        onClick={() => onAction?.(widget)}
        className="inline-flex max-w-[160px] items-center gap-1 rounded-md border border-zinc-700/60 bg-zinc-800/70 px-2 py-0.5 text-[11px] text-zinc-300 transition-colors hover:bg-zinc-800/80 hover:text-zinc-100"
      >
        <Icon size={10} />
        <span className="truncate">{widget.label}</span>
      </button>
    );
  }

  const toolToggleClassName = `inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] transition-colors ${
    widget.enabled
      ? "border-emerald-600/50 bg-emerald-900/30 text-emerald-300"
      : "border-zinc-700/60 bg-zinc-800/70 text-zinc-400"
  }`;
  const toolToggleContent = (
    <>
      <Wrench size={10} />
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
      className={`${toolToggleClassName} cursor-pointer hover:bg-emerald-900/40`}
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
          className="rounded-lg px-3 py-1.5 text-left transition-colors hover:bg-zinc-800/80 disabled:opacity-50 group"
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
      <div className="absolute bottom-full right-3 rumi-layer-global-overlay mb-2 w-[min(430px,calc(100vw-32px))] overflow-hidden rounded-xl border border-zinc-700/80 bg-zinc-950 shadow-2xl">
        <div className="border-b border-zinc-800 px-4 py-3">
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
            className="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
            autoFocus
          />
          {error && <p className="text-[11px] text-red-300">{error}</p>}
          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={onCancel}
              className="h-8 rounded-lg px-3 text-xs text-zinc-400 transition-colors hover:bg-zinc-900 hover:text-zinc-100"
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

function ModelDropdown({
  profiles,
  selectedProfile,
  isGenerating,
  placement = "above",
  onSelect,
  onClose,
}: {
  profiles: ModelProfile[];
  selectedProfile: ModelProfile | null;
  isGenerating: boolean;
  placement?: "above" | "below";
  onSelect: (profile: ModelProfile) => void;
  onClose: () => void;
}) {
  const [search, setSearch] = useState("");
  const [remoteProfiles, setRemoteProfiles] = useState<ModelProfile[]>([]);
  const searchRequestSeqRef = useRef(0);
  const trimmedSearch = search.trim();
  const filtered = useMemo(() => filterModelProfilesBySearch(profiles, search), [profiles, search]);

  useEffect(() => {
    searchRequestSeqRef.current += 1;
    const requestSeq = searchRequestSeqRef.current;
    if (!trimmedSearch) {
      setRemoteProfiles([]);
      return;
    }
    let disposed = false;
    const timer = window.setTimeout(() => {
      chatComposerResources.searchModels({ query: trimmedSearch, max_results: 30 })
        .then((result) => {
          if (disposed || requestSeq !== searchRequestSeqRef.current) return;
          setRemoteProfiles((result.models ?? []).map(modelSearchItemToProfile));
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
  }, [trimmedSearch]);

  const visibleProfiles = useMemo(() => {
    const byId = new Map<string, ModelProfile>();
    for (const profile of [...filtered, ...remoteProfiles]) {
      const key = profile.profile_id || profile.qualified_model_id || `${profile.provider_id ?? ""}/${profile.model_id ?? ""}`;
      if (!key || byId.has(key)) continue;
      byId.set(key, profile);
    }
    return [...byId.values()];
  }, [filtered, remoteProfiles]);

  const groupedByProvider = useMemo(() => {
    const map = new Map<string, ModelProfile[]>();
    for (const profile of visibleProfiles) {
      const provider = profile.provider_id ?? "other";
      const list = map.get(provider) ?? [];
      list.push(profile);
      map.set(provider, list);
    }
    return [...map.entries()];
  }, [visibleProfiles]);

  return (
    <>
      <button type="button" aria-label="close model dropdown" className="fixed inset-0 rumi-layer-local-popover cursor-default" onClick={onClose} />
      <div
        className={`absolute rumi-layer-command-palette w-[min(360px,calc(100vw-88px))] max-w-[calc(100vw-88px)] overflow-hidden rounded-xl border border-zinc-700/70 bg-zinc-950 shadow-2xl ${
          modelDropdownPlacementClassName(placement)
        }`}
      >
        <div className="border-b border-zinc-800 p-2">
          <div className="relative">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="モデルを検索... @google"
              className="w-full rounded-lg bg-zinc-900 border border-zinc-800 pl-8 pr-3 py-1.5 text-sm text-zinc-200 outline-none focus:border-zinc-600 placeholder:text-zinc-600"
              autoFocus
            />
          </div>
        </div>
        <div className="max-h-64 overflow-y-auto py-1">
          {groupedByProvider.map(([provider, profiles]) => (
            <div key={provider}>
              <div className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-600">{provider}</div>
              {profiles.map((profile) => {
                const needsKey = profileNeedsApiKey(profile);
                const badges = capabilityBadges(profile).slice(0, 4);
                return (
                  <button
                    key={profile.profile_id}
                    type="button"
                    draggable
                    disabled={isGenerating}
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
                    className={`w-full flex items-center justify-between gap-2 px-3 py-1.5 text-left transition-colors hover:bg-zinc-800/80 disabled:opacity-50 ${
                      selectedProfile?.profile_id === profile.profile_id ? "bg-zinc-800/60" : ""
                    }`}
                  >
                    <span className="min-w-0">
                      <span className="block truncate text-[13px] text-zinc-200">{compactProfileName(profileDisplayName(profile))}</span>
                      <span className="block truncate text-[10px] text-zinc-500">
                        {profile.provider_display_name ?? profile.provider_id} · {profile.provider_id}/{profile.model_id}
                      </span>
                      {badges.length > 0 && (
                        <span className="mt-1 flex flex-wrap gap-1">
                          {badges.map((badge) => (
                            <span key={badge} className="rounded border border-zinc-700 px-1.5 py-0.5 text-[9px] leading-none text-zinc-400">
                              {badge}
                            </span>
                          ))}
                        </span>
                      )}
                    </span>
                    {needsKey ? (
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
          {groupedByProvider.length === 0 && (
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
      <div className="absolute bottom-full left-0 mb-2 rumi-layer-modal w-[220px] overflow-hidden rounded-xl border border-zinc-700/70 bg-zinc-950 shadow-2xl">
        <div className="border-b border-zinc-800 px-3 py-2">
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
                  mode === id ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-200"
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

export function AtMentionMenu({
  candidates,
  activeIndex,
  onActiveIndexChange,
  onSelect,
  onClose,
  style,
}: {
  candidates: ComposerAtMentionCandidate[];
  activeIndex: number;
  onActiveIndexChange: (index: number) => void;
  onSelect: (candidate: ComposerAtMentionCandidate) => void;
  onClose: () => void;
  style?: CSSProperties;
}) {
  return (
    <>
      <button type="button" tabIndex={-1} aria-label="close mention menu" className="fixed inset-0 rumi-layer-local-popover cursor-default" onClick={onClose} />
      <div
        id={AT_MENTION_LISTBOX_ID}
        role="listbox"
        aria-label="Composer mentions"
        data-testid="composer-at-mention-candidates"
        style={style}
        className="fixed rumi-layer-modal w-[min(440px,calc(100vw-32px))] overflow-hidden rounded-xl border border-zinc-700/70 bg-zinc-950 shadow-2xl"
      >
        <div className="border-b border-zinc-800 px-3 py-2 flex items-center justify-between gap-2">
          <span className="inline-flex min-w-0 items-center gap-2">
            <Wrench size={13} className="text-zinc-500" />
            <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">Mentions</span>
          </span>
          <span className="text-[10px] text-zinc-600">{candidates.length}</span>
        </div>
        <div className="max-h-56 overflow-y-auto py-1">
          {candidates.length === 0 && (
            <div
              role="status"
              aria-live="polite"
              data-testid="composer-at-mention-empty"
              className="px-3 py-4 text-sm leading-relaxed text-zinc-400"
            >
              一致する候補はありません。Enterで本文を送信、Tabで次の操作へ移動できます。
            </div>
          )}
          {candidates.map((candidate, index) => {
            const Icon = candidate.kind === "tool" || candidate.kind === "service" ? Wrench : candidate.kind === "skill" ? BrainCircuit : FileText;
            return (
            <button
              key={candidate.id}
              id={`composer-at-mention-option-${index}`}
              type="button"
              role="option"
              aria-selected={index === activeIndex}
              tabIndex={-1}
              onMouseEnter={() => onActiveIndexChange(index)}
              onClick={() => onSelect(candidate)}
              className={`flex min-h-11 w-full items-center justify-between gap-3 px-3 py-2 text-left transition-colors ${
                index === activeIndex ? "bg-zinc-800 text-zinc-100" : "hover:bg-zinc-900"
              }`}
            >
              <span className="flex min-w-0 items-center gap-2">
                <Icon size={13} className="text-zinc-500 flex-shrink-0" />
                <span className="min-w-0">
                  <span className="block truncate text-[13px] text-zinc-200">@{candidate.label}</span>
                  {candidate.description && <span className="block truncate text-[10px] text-zinc-500">{candidate.description}</span>}
                </span>
              </span>
              <span className={`flex-shrink-0 rounded-full border px-1.5 py-0.5 text-[9px] leading-none ${
                candidate.kind === "tool"
                  ? "border-emerald-500/25 text-emerald-300"
                  : candidate.kind === "service"
                    ? "border-cyan-500/25 text-cyan-300"
                  : candidate.kind === "skill"
                    ? "border-violet-500/25 text-violet-300"
                    : "border-sky-500/25 text-sky-300"
              }`}>
                {candidate.kind}
              </span>
            </button>
            );
          })}
        </div>
      </div>
    </>
  );
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
      className="fixed rumi-layer-modal w-[min(460px,calc(100vw-32px))] overflow-hidden rounded-xl border border-zinc-700/70 bg-zinc-950 shadow-2xl"
    >
      <div className="flex items-center justify-between gap-2 border-b border-zinc-800 px-3 py-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Models</span>
        {onClose && (
          <button
            type="button"
            aria-label="close model candidates"
            onClick={onClose}
            className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-900 hover:text-zinc-200"
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
                index === activeIndex ? "bg-zinc-800 text-zinc-100" : "hover:bg-zinc-900"
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
  yoloMode = false,
  modelStatusIndicators = [],
  voiceInputEnabled = true,
  voiceInputUseAi = false,
  mode = "chat",
  codingContext = null,
  codingWorkspaces = [],
  selectedCodingWorkspaceId = null,
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
}: ComposerRendererProps) {
  const [menuOpen, setMenuOpen] = useState(false);
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
  const [isVoiceListening, setIsVoiceListening] = useState(false);
  const [textareaSelection, setTextareaSelection] = useState({ start: input.length, end: input.length });
  const [textareaFocused, setTextareaFocused] = useState(false);
  const [textareaScrollTop, setTextareaScrollTop] = useState(0);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuButtonRef = useRef<HTMLButtonElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const recognitionRef = useRef<{ stop: () => void } | null>(null);
  const chromeWidgetNodeMapRef = useRef<Map<string, HTMLDivElement>>(new Map());
  const submitPointerHandledRef = useRef(false);
  const lastModelPickerRequestIdRef = useRef(modelPickerRequestId);
  const chromeButtonTabIndex = keyboardButtonNavigation ? undefined : -1;
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
	  const selectableProfiles = modelProfiles.length > 0 ? modelProfiles : favoriteProfiles;
	  const selectedToolIdSet = useMemo(() => new Set(selectedToolIds), [selectedToolIds]);
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
  const inlineReferences = useMemo<ComposerReferenceDescriptor[]>(() => {
    const toolsById = new Map(toolItems.map((item) => [item.id, item]));
    const skillsById = new Map(skillExtensions.map((item) => [item.id, item]));
    return entityReferences.flatMap((reference) => {
      if (!input.includes(reference.syntax)) return [];
      if (reference.kind === "tool") {
        const item = toolsById.get(reference.id);
        return item ? [{ ...reference, label: item.ui?.composer_label ?? item.label ?? item.id }] : [];
      }
      if (reference.kind === "skill") {
        const skill = skillsById.get(reference.id);
        return skill ? [{ ...reference, label: skill.label || skill.id }] : [];
      }
      const label = reference.id.split("/").filter(Boolean).pop() || reference.id;
      return [{ ...reference, label }];
    });
  }, [entityReferences, input, skillExtensions, toolItems]);
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
  const hasSlashCommandPrefix = templateAllowsSlashCommands && !isSteerMode && input.startsWith("/") && !isEscapedSlash;
  const slashText = hasSlashCommandPrefix ? input.slice(1) : "";
  const slashCommandName = slashText.trimStart().split(/\s+/, 1)[0] ?? "";
  const slashQuery = slashCommandName.toLowerCase();
  const thinkingCommand = commands.find((command) => command.id === "think");
  const thinkingMatch = hasSlashCommandPrefix ? thinkingCommandMatch(input) : null;
  const matchedCommands = hasSlashCommandPrefix
    ? thinkingMatch && thinkingCommand && levels.length > 0
      ? levels
          .filter((level) => !thinkingMatch.query || level.toLowerCase().includes(thinkingMatch.query))
          .map((level) => ({
            ...thinkingCommand,
            id: `think:${level}`,
            name: `think ${level}`,
            label: `Thinking ${THINKING_LABELS[level] ?? level}`,
            description: `思考レベルを ${THINKING_LABELS[level] ?? level} に変更`,
          }))
      : commands.filter((command) => {
          const haystack = `${command.id} ${command.name} ${(command.aliases ?? []).join(" ")} ${command.label} ${command.description ?? ""}`.toLowerCase();
          return !slashQuery || haystack.includes(slashQuery);
        })
    : [];
  const showThinkingLevelChips = Boolean(thinkingMatch && thinkingCommand && levels.length > 0);
  const hasModelCommandCandidates = !isSteerMode && modelCommandCandidates.length > 0;
  const showCommandSuggestions = templateAllowsSlashCommands && !hasModelCommandCandidates && matchedCommands.length > 0;
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

	  const needsApiKey = useCallback(
    (profile: ModelProfile | null | undefined) => (
      profileNeedsApiKey(profile) && !locallyConfiguredProviders.has(profileProviderId(profile))
    ),
    [locallyConfiguredProviders],
  );

  const updateComposerPopoverAnchor = useCallback(() => {
    if (typeof window === "undefined") return;
    const modelPickerNode = chromeWidgetNodeMapRef.current.get("model-picker");
    const anchorRect = modelPickerNode?.getBoundingClientRect() ?? textareaRef.current?.getBoundingClientRect() ?? null;
    setComposerPopoverStyle(modelCandidatePopupStyleForAnchor(anchorRect, window.innerWidth));
  }, []);

  const resizeNewConversationTextarea = useCallback(
    (textarea: HTMLTextAreaElement | null = textareaRef.current) => {
      if (!textarea) return;
      if (!isNewConversation) {
        textarea.style.height = "";
        textarea.style.overflowY = "";
        return;
      }
      fitComposerTextareaHeight(
        textarea,
        NEW_CONVERSATION_TEXTAREA_MIN_HEIGHT,
        NEW_CONVERSATION_TEXTAREA_MAX_HEIGHT,
        textarea.parentElement?.querySelector<HTMLElement>(".rumi-composer-reference-overlay")?.scrollHeight ?? 0,
      );
    },
    [isNewConversation],
  );

  const syncTextareaVisualState = useCallback((textarea: HTMLTextAreaElement | null = textareaRef.current) => {
    if (!textarea) return;
    setTextareaSelection({ start: textarea.selectionStart, end: textarea.selectionEnd });
    setTextareaScrollTop(textarea.scrollTop);
  }, []);

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
    resizeNewConversationTextarea();
  }, [input, resizeNewConversationTextarea]);

  useEffect(() => {
    if (!isNewConversation || typeof window === "undefined") return;
    const handleResize = () => resizeNewConversationTextarea();
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [isNewConversation, resizeNewConversationTextarea]);

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
      if (!templateAllowsSlashCommands || isGenerating || !shouldFocusComposerForSlashKey(event, event.target)) return;
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
  }, [input, isGenerating, onInputChange, templateAllowsSlashCommands]);

  const chooseCommand = (commandId: string, rawInput = input) => {
    if (!templateAllowsSlashCommands) return;
    const thinkingLevelMatch = commandId.match(/^think:(.+)$/);
    if (thinkingLevelMatch) {
      onCommandSelect?.("think", `/think ${thinkingLevelMatch[1]}`);
      onInputChange("");
      return;
    }

    const command = commands.find((item) => item.id === commandId);
    const action = command?.execution.type === "frontend" ? command.execution.action : "";
    const rawHasArgs = rawInput.trim().includes(" ");
    if (command?.id === "think") {
      onInputChange("/think ");
      window.setTimeout(() => textareaRef.current?.focus({ preventScroll: true }), 0);
      return;
    }
    if (action === "open_model_picker" && !rawHasArgs) {
      setModelDropdownOpen(true);
      setMenuOpen(false);
    } else if (action === "open_tool_picker" && !rawHasArgs) {
      setOpenFolder("tools");
      setMenuOpen(true);
    } else if (action === "open_command_help") {
      setOpenFolder("commands");
      setMenuOpen(true);
    }
    onCommandSelect?.(commandId, rawInput);
    if (!(command?.id === "model" && rawHasArgs)) {
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
      if (!textarea || suppressPopovers || !templateAllowsAtMentions) {
        setAtMentionOpen(false);
        setAtMentionQuery("");
        setAtMentionStart(null);
        return;
      }
      const cursorPos = textarea.selectionStart ?? value.length;
      const activeMention = activeMentionAtCursor(value, cursorPos, atMentionKnownValues);

      if (activeMention && !isSteerMode) {
        setAtMentionOpen(true);
        setAtMentionQuery(activeMention.query);
        setAtMentionStart(activeMention.start);
        updateComposerPopoverAnchor();
      } else {
        setAtMentionOpen(false);
        setAtMentionQuery("");
        setAtMentionStart(null);
      }
    },
    [atMentionKnownValues, isSteerMode, suppressPopovers, templateAllowsAtMentions, updateComposerPopoverAnchor],
  );

  useEffect(() => {
    updateAtMentionStateFromInput(input);
  }, [input, updateAtMentionStateFromInput]);

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
        syncTextareaVisualState(textarea);
      }, 0);
    },
	    [atMentionKnownValues, atMentionQuery.length, atMentionStart, entityReferences, input, mode, onAtFileAttach, onDropWidget, onEntityReferencesChange, onInputChange, syncTextareaVisualState],
	  );

  const handleCopy = useCallback((event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const textarea = event.currentTarget;
    const selectedText = input.slice(textarea.selectionStart, textarea.selectionEnd);
    const serialized = serializeComposerReferences(selectedText, entityReferences);
    if (!serialized) return;
    event.preventDefault();
    event.clipboardData.setData("text/plain", selectedText);
    event.clipboardData.setData(COMPOSER_REFERENCE_MIME, serialized);
  }, [entityReferences, input]);

  const handlePaste = useCallback((event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const raw = event.clipboardData.getData(COMPOSER_REFERENCE_MIME);
    if (!raw) return;
    const restored = restoreComposerReferences(raw, {
      tools: toolItems,
      skills: skillExtensions,
      files: mode === "coding" ? codingContext?.files ?? [] : [],
    });
    if (!restored) return;
    event.preventDefault();
    const textarea = event.currentTarget;
    const next = insertComposerReferencePaste(input, textarea.selectionStart, textarea.selectionEnd, restored);
    onInputChange(next.value);
    onEntityReferencesChange?.(mergeComposerReferences(entityReferences, next.references, next.value));
    for (const reference of next.references) {
      if (reference.kind === "file" && mode === "coding") onAtFileAttach?.(reference.id);
    }
    setTimeout(() => {
      textarea.setSelectionRange(next.cursor, next.cursor);
      textarea.focus();
      syncTextareaVisualState(textarea);
    }, 0);
  }, [codingContext?.files, entityReferences, input, mode, onAtFileAttach, onEntityReferencesChange, onInputChange, skillExtensions, syncTextareaVisualState, toolItems]);

  const attachFiles = useCallback(async (files: FileList | null) => {
    if (!files?.length) return;
    if (!templateAllowsFileAttachments) return;
    const newFiles: AttachedFile[] = await Promise.all(Array.from(files).map(fileToAttachment));
    onFileAttach?.(newFiles);
  }, [onFileAttach, templateAllowsFileAttachments]);

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
      if (isGenerating) {
        event.preventDefault();
        const prompt = input.trim();
        if (prompt && !steerBusy) {
          onSteerSubmit?.(prompt);
        } else if (!prompt) {
          onStopGenerating?.();
        }
        return;
      }
      if (pendingMentionAttachmentPaths.length > 0) {
        event.preventDefault();
        return;
      }
      if (needsApiKey(selectedProfile)) {
        event.preventDefault();
        if (selectedProfile) setApiKeyPromptProfile(selectedProfile);
        return;
      }
      onSubmit(event);
    },
    [input, isGenerating, needsApiKey, onStopGenerating, onSteerSubmit, onSubmit, pendingMentionAttachmentPaths.length, selectedProfile, steerBusy],
  );

  const handleSendButtonPointerDown = useCallback(
    (event: React.PointerEvent<HTMLButtonElement>) => {
      if (event.button !== 0) return;
      if (!isGenerating && pendingMentionAttachmentPaths.length > 0) return;
      if (!isGenerating && !input.trim() && attachedFiles.length === 0) return;
      if (isGenerating && !input.trim()) return;
      submitPointerHandledRef.current = true;
      handleSubmitWithApiKeyGuard(event);
    },
    [attachedFiles.length, handleSubmitWithApiKeyGuard, input, isGenerating, pendingMentionAttachmentPaths.length],
  );

  const handleSendButtonClick = useCallback(
    (event: React.MouseEvent<HTMLButtonElement>) => {
      if (submitPointerHandledRef.current) {
        submitPointerHandledRef.current = false;
        event.preventDefault();
        return;
      }
      if (isGenerating) {
        event.preventDefault();
        if (input.trim()) {
          handleSubmitWithApiKeyGuard(event);
        } else {
          onStopGenerating?.();
        }
        return;
      }
      handleSubmitWithApiKeyGuard(event);
    },
    [handleSubmitWithApiKeyGuard, input, isGenerating, onStopGenerating],
  );

  const toggleVoiceInput = useCallback(() => {
    if (!voiceInputEnabled || !templateAllowsVoiceInput || isGenerating) return;
    if (isVoiceListening) {
      recognitionRef.current?.stop();
      setIsVoiceListening(false);
      return;
    }
    const recognitionCtor = (window as unknown as {
      SpeechRecognition?: new () => SpeechRecognitionLike;
      webkitSpeechRecognition?: new () => SpeechRecognitionLike;
    }).SpeechRecognition ?? (window as unknown as { webkitSpeechRecognition?: new () => SpeechRecognitionLike }).webkitSpeechRecognition;
    if (!recognitionCtor) return;
    const recognition = new recognitionCtor();
    recognition.lang = "ja-JP";
    recognition.continuous = false;
    recognition.interimResults = true;
    let finalTranscript = "";
    recognition.onresult = (event: SpeechRecognitionEventLike) => {
      let interim = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        const text = result?.[0]?.transcript ?? "";
        if (result?.isFinal) finalTranscript += text;
        else interim += text;
      }
      const transcript = `${finalTranscript}${interim}`.trim();
      if (!transcript) return;
      const prefix = voiceInputUseAi ? "文字起こしして: " : "";
      const base = input.trimEnd();
      onInputChange(`${base}${base ? "\n" : ""}${prefix}${transcript}`);
    };
    recognition.onend = () => {
      recognitionRef.current = null;
      setIsVoiceListening(false);
      window.setTimeout(() => textareaRef.current?.focus({ preventScroll: true }), 0);
    };
    recognitionRef.current = recognition;
    setIsVoiceListening(true);
    recognition.start();
  }, [input, isGenerating, isVoiceListening, onInputChange, templateAllowsVoiceInput, voiceInputEnabled, voiceInputUseAi]);

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
        event.stopPropagation();
        return;
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

      const modelCandidateAction = isSteerMode
        ? { handled: false as const }
        : modelCandidateMenuKeyAction(
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

      if (event.key === "Enter" && !event.shiftKey && isSteerMode) {
        event.preventDefault();
        handleSubmitWithApiKeyGuard(event);
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
          chooseCommand(matchedCommands[selectedCommandIndex]?.id ?? matchedCommands[0].id);
          return;
        }
      }

      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        handleSubmitWithApiKeyGuard(event);
      }
    },
    [
      atMentionCandidates,
      atMentionOpen,
      chooseModelCommandCandidate,
      handleAtMentionSelect,
      handleSubmitWithApiKeyGuard,
      isSteerMode,
      matchedCommands,
      modelCommandCandidates,
      onModelCommandCandidatesClose,
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
      id: "file-attach",
      slot: "leading",
      homeSlot: "editor-leading",
      order: 20,
      visible: templateAllowsFileAttachments,
      width: COMPOSER_CHROME_WIDTHS.icon,
      render: () => (
        <button
          type="button"
          tabIndex={chromeButtonTabIndex}
          aria-label="ファイルを添付"
          disabled={isGenerating || !templateAllowsFileAttachments}
          title="ファイル添付（複数選択可）"
          onClick={() => fileInputRef.current?.click()}
          className={`${
            isNewConversation
              ? "text-zinc-200 hover:bg-zinc-800/60 hover:text-white"
              : "text-zinc-400 hover:text-zinc-100 hover:bg-zinc-700/60"
          } h-11 w-11 flex flex-shrink-0 items-center justify-center rounded-lg transition-colors disabled:opacity-50`}
        >
          <WarmActionIcon kind="attach" size="md" />
        </button>
      ),
    },
    {
      id: "voice-input",
      slot: "leading",
      homeSlot: "editor-trailing",
      order: 30,
      visible: templateAllowsVoiceInput,
      mobile: "hide",
      width: COMPOSER_CHROME_WIDTHS.icon,
      render: () => (
	        <button
	          type="button"
	          tabIndex={chromeButtonTabIndex}
	          aria-label={isVoiceListening ? "音声入力を停止" : "音声入力を開始"}
	          disabled={isGenerating || !voiceInputEnabled || !templateAllowsVoiceInput}
	          title={isVoiceListening ? "音声入力を停止" : voiceInputUseAi ? "音声入力（AI文字起こし）" : "音声入力"}
	          onClick={toggleVoiceInput}
	          className={`h-11 w-11 flex flex-shrink-0 items-center justify-center rounded-lg transition-colors disabled:opacity-50 ${
	            isVoiceListening ? "bg-rose-500/15 text-rose-300 hover:bg-rose-500/25" : "text-zinc-400 hover:text-zinc-100 hover:bg-zinc-700/60"
	          }`}
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
      id: "yolo-status",
      slot: "leading",
      homeSlot: "toolbar-leading",
      order: 55,
      visible: yoloMode && visibleModelStatusIndicators.length === 0,
      width: COMPOSER_CHROME_WIDTHS.badge,
      className: "overflow-hidden",
      render: () => (
        <span className="rounded-full border border-orange-500/30 px-2 py-0.5 text-[11px] text-orange-300">
          YOLO
        </span>
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
              className="flex h-full w-full min-w-0 items-center gap-1 text-[12px] font-medium text-zinc-300 hover:text-zinc-100 transition-colors disabled:opacity-50"
            >
              <span className="min-w-0 flex-1 truncate" title={profileName}>モデル: {compactSelectedProfileName}</span>
              <ChevronDown size={12} className={`flex-shrink-0 transition-transform ${modelDropdownOpen ? "rotate-180" : ""}`} />
            </button>
            {modelDropdownOpen && (
              <ModelDropdown
                profiles={selectableProfiles}
                selectedProfile={selectedProfile}
                isGenerating={isGenerating}
                placement={isNewConversation ? "below" : "above"}
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
      homeSlot: "editor-trailing",
      order: 40,
      width: isNewConversation ? COMPOSER_CHROME_WIDTHS.sendLarge : COMPOSER_CHROME_WIDTHS.send,
      render: () => (
        <button
          type="button"
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
          onPointerDown={handleSendButtonPointerDown}
          onClick={handleSendButtonClick}
          title={isGenerating
            ? (input.trim() ? "追加指示を送る" : "停止")
            : pendingMentionAttachmentPaths.length > 0
              ? "ファイルを読み込み中"
              : "送信"}
          className={`rumi-send-button h-11 w-11 flex flex-shrink-0 items-center justify-center rounded-lg disabled:opacity-30 disabled:cursor-not-allowed transition-opacity ${
            isGenerating
              ? "bg-zinc-200 text-black hover:bg-white shadow-sm transition-colors"
              : "bg-transparent p-0 hover:opacity-90"
          }`}
        >
          {isGenerating && !input.trim() ? (
            <WarmActionIcon kind="stop" size={isNewConversation ? "lg" : "md"} className="shadow-none ring-0" />
          ) : isGenerating ? (
            <CornerDownRight size={15} strokeWidth={2.4} />
          ) : (
            <SendButtonIcon className="h-full w-full" />
          )}
        </button>
      ),
    },
  ];

  const leadingChromeWidgets = composerChromeWidgetsForSlot(chromeWidgets, "leading");
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
      className={`${isNewConversation ? "w-full px-5" : "px-5 pb-5 pt-2 bg-[#09090b] flex-shrink-0 max-[640px]:px-2 max-[640px]:pb-2"}`}
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
              : "rounded-xl border-zinc-700/30 bg-[#2b2b2d] shadow-2xl shadow-black/20 focus-within:border-zinc-500/60 max-[640px]:rounded-xl"
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
            showThinkingLevelChips ? (
              <div className="absolute bottom-full left-4 rumi-layer-global-overlay mb-2 flex w-[min(520px,calc(100vw-32px))] flex-wrap items-center gap-2 rounded-xl border border-zinc-700/70 bg-zinc-950/95 px-3 py-2 shadow-2xl">
                <span className="mr-1 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Thinking</span>
                {matchedCommands.map((command, index) => {
                  const level = command.id.replace(/^think:/, "");
                          return (
                            <button
                              key={command.id}
                              type="button"
                              tabIndex={chromeButtonTabIndex}
                              onMouseEnter={() => setSelectedCommandIndex(index)}
                              onClick={() => chooseCommand(command.id)}
                      className={`h-8 rounded-lg border px-3 text-xs font-medium transition-colors ${
                        index === selectedCommandIndex
                          ? "border-zinc-400 bg-zinc-100 text-zinc-950"
                          : "border-zinc-700 bg-zinc-900 text-zinc-300 hover:border-zinc-500 hover:text-zinc-100"
                      }`}
                    >
                      {THINKING_LABELS[level] ?? level}
                    </button>
                  );
                })}
              </div>
            ) : (
              <div className="absolute bottom-full left-4 rumi-layer-global-overlay mb-2 w-[min(420px,calc(100vw-32px))] overflow-hidden rounded-xl border border-zinc-700/70 bg-zinc-950 shadow-2xl">
                <div className="border-b border-zinc-800 px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
                  Commands
                </div>
                <div className="max-h-56 overflow-y-auto py-1">
                          {matchedCommands.map((command, index) => (
                            <button
                              key={command.id}
                              type="button"
                              tabIndex={chromeButtonTabIndex}
                              onMouseEnter={() => setSelectedCommandIndex(index)}
                              onClick={() => chooseCommand(command.id)}
                      className={`flex w-full items-center justify-between gap-3 px-3 py-2 text-left ${
                        index === selectedCommandIndex ? "bg-zinc-800 text-zinc-100" : "hover:bg-zinc-900"
                      }`}
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-sm text-zinc-100">/{command.name ?? command.id}</span>
                        {command.description && (
                          <span className="block truncate text-[11px] text-zinc-500">{command.description}</span>
                        )}
                      </span>
                      <span className="flex flex-shrink-0 items-center gap-1">
                        {command.risk && (
                          <span className={`rounded-full border px-2 py-0.5 text-[10px] ${RISK_BADGE_STYLES[command.risk] ?? "border-zinc-700 text-zinc-400"}`}>
                            {command.risk}
                          </span>
                        )}
                        {(command.enabled || command.active) && (
                          <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[10px] text-emerald-300">on</span>
                        )}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            )
          )}

          {atMentionOpen && (
            <AtMentionMenu
              candidates={atMentionCandidates}
              activeIndex={selectedAtMentionIndex}
              onActiveIndexChange={setSelectedAtMentionIndex}
              onSelect={handleAtMentionSelect}
              onClose={() => setAtMentionOpen(false)}
            />
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
              <div ref={menuRef} className="absolute bottom-full left-4 rumi-layer-global-overlay mb-2 grid w-[min(480px,calc(100vw-32px))] grid-cols-[120px_minmax(0,1fr)] overflow-hidden rounded-xl border border-zinc-700/70 bg-zinc-950 shadow-2xl max-[640px]:left-2 max-[640px]:grid-cols-1">
                <div className="border-r border-zinc-800 bg-zinc-950/90 p-1.5 max-[640px]:flex max-[640px]:border-b max-[640px]:border-r-0">
                  {menuFolders.map(([id, label, Icon]) => (
                            <button
                              key={id}
                              type="button"
                              tabIndex={chromeButtonTabIndex}
                              onClick={() => setOpenFolder(id)}
                      className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-xs transition-colors ${
                        openFolder === id
                          ? "bg-zinc-800 text-zinc-100"
                          : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
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
                                ? "bg-zinc-800 text-zinc-100"
                                : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
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
                            className="flex items-center justify-between gap-2 rounded-lg px-3 py-1.5 text-left hover:bg-zinc-800/80 transition-colors"
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
                                  onClick={() => {
                                    chooseCommand(command.id);
                                    setMenuOpen(false);
                                  }}
                          className="flex items-center justify-between gap-3 rounded-lg px-3 py-1.5 text-left hover:bg-zinc-800/80 transition-colors"
                        >
                          <span className="min-w-0">
                            <span className="block truncate text-[13px] text-zinc-200">/{command.name ?? command.id}</span>
                            {command.description && (
                              <span className="block truncate text-[10px] text-zinc-500">{command.description}</span>
                            )}
                          </span>
                          <span className="flex flex-shrink-0 items-center gap-1">
                            {command.visibility === "advanced" && (
                              <span className="rounded-full border border-zinc-700 px-2 py-0.5 text-[10px] text-zinc-500">advanced</span>
                            )}
                            {(command.enabled || command.active) && (
                              <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[10px] text-emerald-300">on</span>
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

          {isNewConversation && (attachedFiles.length > 0 || pendingMentionAttachmentPaths.length > 0) && (
            <div className="flex gap-4 overflow-x-auto px-6 pb-2 pt-5">
              {pendingMentionAttachmentPaths.map((path) => (
                <PendingFileChip key={path} path={path} onRemove={onPendingMentionAttachmentRemove} />
              ))}
              {attachedFiles.map((file) => (
                <FilePreviewCard key={file.id} file={file} onRemove={onFileRemove} />
              ))}
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

          {!isSteerMode && (
            <StructuredComposerPanel
              composerInput={composerInput}
              values={structuredInputValues}
              onApply={(values) => onStructuredInputChange?.(values)}
            />
          )}

          {isNewConversation ? (
            <div className="grid gap-1.5">
              <div className="rumi-composer-main-panel flex flex-col gap-2 rounded-3xl border border-white/10 bg-[#20201f] p-3 shadow-xl focus-within:border-white/30 focus-within:bg-[#242423] focus-within:shadow-2xl transition-all duration-300">
                <div className="rumi-composer-editor-row grid min-h-11 grid-cols-[2.75rem_minmax(0,1fr)_auto] items-end gap-x-3">
                  <div className="flex items-center justify-center self-end">
                    {newConversationInlineLeadingWidgets.map((widget) => (
                      <ComposerChromeWidget key={widget.id} widget={widget} />
                    ))}
                  </div>
                  <div className="rumi-composer-editor relative min-w-0 self-end">
                    <ComposerReferenceOverlay
                      value={input}
                      references={inlineReferences}
                      cursorPosition={textareaSelection.start}
                      showCaret={textareaFocused && textareaSelection.start === textareaSelection.end}
                      scrollTop={textareaScrollTop}
                      className="min-h-[24px] max-h-[150px] px-0 pb-0 pt-0 text-[16px] font-medium leading-[24px]"
                    />
                    <textarea
                      ref={textareaRef}
                      autoFocus
                      rows={1}
                      value={input}
                      data-template-composer-input={templateComposerInputId || undefined}
                      onChange={(event) => {
                        resizeNewConversationTextarea(event.currentTarget);
                        handleInputChange(event.currentTarget.value);
                      }}
                      placeholder={effectiveComposerPlaceholder}
                      aria-label="Rumiにメッセージを送信"
                      aria-autocomplete="list"
                      aria-controls={AT_MENTION_LISTBOX_ID}
                      aria-activedescendant={atMentionOpen && atMentionCandidates.length > 0 ? `composer-at-mention-option-${selectedAtMentionIndex}` : undefined}
                      aria-expanded={atMentionOpen}
                      role="combobox"
                      className={`rumi-composer-input-new rumi-composer-textarea relative rumi-layer-panel block min-h-[24px] w-full max-h-[150px] select-text resize-none overflow-x-hidden overflow-y-auto border-none bg-transparent px-0 pb-0 pt-0 text-[16px] font-medium leading-[24px] outline-none placeholder:text-zinc-500/70 ${inlineReferences.length > 0 ? "text-transparent caret-transparent" : "text-zinc-100 caret-zinc-100"}`}
                      onFocus={(event) => {
                        setTextareaFocused(true);
                        syncTextareaVisualState(event.currentTarget);
                      }}
                      onBlur={() => setTextareaFocused(false)}
                      onClick={(event) => syncTextareaVisualState(event.currentTarget)}
                      onKeyUp={(event) => syncTextareaVisualState(event.currentTarget)}
                      onSelect={(event) => syncTextareaVisualState(event.currentTarget)}
                      onScroll={(event) => syncTextareaVisualState(event.currentTarget)}
                      onKeyDownCapture={(event) => {
                        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
                          event.stopPropagation();
                        }
                      }}
                      onKeyDown={handleKeyDown}
                      onCopy={handleCopy}
                      onPaste={handlePaste}
                    />
                  </div>
                  <div className="flex items-center justify-end gap-2 self-end">
                    {newConversationTopRightWidgets.map((widget) => (
                      <ComposerChromeWidget key={widget.id} widget={widget} />
                    ))}
                  </div>
                </div>

                <div className="rumi-composer-toolbar flex items-center justify-between border-t border-white/5 pt-2">
                  <div className="flex min-w-0 items-center gap-2 overflow-hidden">
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
            <div className="relative min-w-0">
              <ComposerReferenceOverlay
                value={input}
                references={inlineReferences}
                cursorPosition={textareaSelection.start}
                showCaret={textareaFocused && textareaSelection.start === textareaSelection.end}
                scrollTop={textareaScrollTop}
                className="min-h-[34px] max-h-[130px] px-5 pb-0 pt-3 text-[15px] leading-normal max-[640px]:min-h-[32px] max-[640px]:px-3 max-[640px]:pb-0 max-[640px]:pt-2.5 max-[640px]:text-[13px]"
              />
              <textarea
                ref={textareaRef}
                autoFocus
                value={input}
                data-template-composer-input={templateComposerInputId || undefined}
                onChange={(event) => handleInputChange(event.target.value)}
                placeholder={effectiveComposerPlaceholder}
                aria-label="Rumiにメッセージを送信"
                aria-autocomplete="list"
                aria-controls={AT_MENTION_LISTBOX_ID}
                aria-activedescendant={atMentionOpen && atMentionCandidates.length > 0 ? `composer-at-mention-option-${selectedAtMentionIndex}` : undefined}
                aria-expanded={atMentionOpen}
                role="combobox"
                className={`rumi-composer-textarea relative min-h-[34px] w-full max-h-[130px] select-text resize-none border-none bg-transparent px-5 pb-0 pt-3 text-[15px] outline-none max-[640px]:min-h-[32px] max-[640px]:px-3 max-[640px]:pb-0 max-[640px]:pt-2.5 max-[640px]:text-[13px] ${inlineReferences.length > 0 ? "text-transparent caret-transparent" : "text-zinc-100 caret-zinc-100"}`}
                onFocus={(event) => {
                  setTextareaFocused(true);
                  syncTextareaVisualState(event.currentTarget);
                }}
                onBlur={() => setTextareaFocused(false)}
                onClick={(event) => syncTextareaVisualState(event.currentTarget)}
                onKeyUp={(event) => syncTextareaVisualState(event.currentTarget)}
                onSelect={(event) => syncTextareaVisualState(event.currentTarget)}
                onScroll={(event) => syncTextareaVisualState(event.currentTarget)}
                onKeyDownCapture={(event) => {
                  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
                    event.stopPropagation();
                  }
                }}
                onKeyDown={handleKeyDown}
                onCopy={handleCopy}
                onPaste={handlePaste}
              />
            </div>
          )}

          {!isSteerMode && (effectiveComposerHelp || templateComposerInfoItems.length > 0) && (
            <div className={`${isNewConversation ? "px-5 pt-1" : "px-5 pt-1 max-[640px]:px-3"} flex min-h-5 flex-wrap items-center gap-1.5 text-[10px] leading-none text-zinc-500`}>
              {effectiveComposerHelp && (
                <span className="min-w-0 flex-1 truncate" title={effectiveComposerHelp}>
                  {effectiveComposerHelp}
                </span>
              )}
              {templateComposerInfoItems.map((item) => (
                <span
                  key={item}
                  className="flex-shrink-0 rounded-full border border-zinc-700/70 px-1.5 py-0.5 text-[9px] uppercase tracking-wide text-zinc-500"
                >
                  {item}
                </span>
              ))}
            </div>
          )}

          {!isNewConversation && toolSelectionTargets.length > 0 && (
            <ToolOverrideChips
              targets={toolSelectionTargets}
              labelForTarget={labelForToolTarget}
              onRemove={(target) => onToolSelectionTargetRemove?.(target)}
            />
          )}

          {isSteerMode && (
            <div className="flex min-h-5 items-center gap-2 px-5 pt-1 text-[10px] leading-none text-zinc-500 max-[640px]:px-3">
              <CornerDownRight size={12} className="flex-shrink-0" />
              <span className="truncate">
                {effectiveComposerHelp}
              </span>
              {steerBusy && <Loader2 size={11} className="flex-shrink-0 animate-spin" />}
              {steerQueuedCount > 0 && (
                <span className="flex-shrink-0 rounded-full border border-zinc-700 px-1.5 py-0.5 text-[9px] leading-none">
                  {steerQueuedCount}件待機
                </span>
              )}
              {steerStatus && (
                <span className="min-w-0 truncate text-zinc-600">{steerStatus}</span>
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

          {((!isNewConversation && (attachedFiles.length > 0 || pendingMentionAttachmentPaths.length > 0)) || droppedWidgets.length > 0) && (
            <div className="px-5 pt-1.5 pb-0.5 flex flex-wrap gap-1 max-[640px]:px-3">
              {!isNewConversation && pendingMentionAttachmentPaths.map((path) => (
                <PendingFileChip key={path} path={path} onRemove={onPendingMentionAttachmentRemove} />
              ))}
              {!isNewConversation && attachedFiles.map((file) => (
                <FileChip key={file.id} file={file} onRemove={onFileRemove} />
              ))}
              {droppedWidgets.map((widget) => (
                <DroppedWidgetChip
                  key={widget.id}
                  widget={widget.type === "tool" ? { ...widget, enabled: selectedToolIdSet.has(widget.sourceItemId || widget.id) } : widget}
                  onAction={onWidgetAction}
                  onToggle={onWidgetToggle}
                />
              ))}
            </div>
          )}

          {!isNewConversation && (
            <div className="px-4 pb-2 pt-0 flex items-center justify-between gap-2 max-[640px]:gap-1.5 max-[640px]:px-2 max-[640px]:pb-1.5">
              <div className="flex min-w-0 items-center gap-1 overflow-hidden">
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
