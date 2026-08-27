import { cloneElement, memo, useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent, type PointerEvent as ReactPointerEvent, type ReactElement, type ReactNode } from "react";
import {
  Blocks,
  BrainCircuit,
  Building2,
  ChevronDown,
  Cpu,
  Database,
  FileText,
  FilePenLine,
  FilePlus2,
  FileSearch,
  Globe,
  GripVertical,
  Hammer,
  Image,
  KeyRound,
  Languages,
  LayoutGrid,
  Layers,
  ListTodo,
  Music,
  Newspaper,
  NotebookPen,
  Power,
  Route,
  Search,
  Settings,
  ShieldAlert,
  SlidersHorizontal,
  Star,
  Tag,
  Terminal,
  TestTubeDiagonal,
  Trash2,
  Wrench,
  GitBranch,
  GitCommit,
  GitCompare,
  ShieldCheck,
  Download,
  Share2,
  Play,
  CalendarClock,
  MessageSquareText,
  Monitor,
  AppWindow,
  MousePointerClick,
  Archive,
  Code2,
  MoreVertical,
  Pin,
  PinOff,
  FolderCheck,
  FolderX,
  Plus,
  X,
} from "lucide-react";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

import type {
  ModelProfile,
  PromptUsageSummary,
  SettingsSection,
  SidebarAction,
  SidebarCategory,
  SidebarField,
  SidebarItem,
} from "../lib/api";
import { toolResources } from "../features/tools/resources/toolResources";
import type { RuntimeCapabilitySnapshot, ToolFilterEntry } from "../lib/toolStatus";
import { toolFilterBlockedSummary } from "../lib/toolStatus";
import { buildBuiltinPlacementManifests, filterPlacementCandidates, normalizePinnedPlacements, togglePinnedPlacement } from "../lib/placement";
import { compareToolUiItems, sortedToolGroups, sortedToolUiItems, supportedComposerDropKind, supportsComposerDrop, toolGroupFor } from "../lib/toolUi";
import { resolveCatalogDisplay } from "../lib/catalogDisplay";
import { PlacementHtmlRenderer } from "./PlacementHtmlRenderer";
import { ToolFilterLogWidget, ToolManagerWidget } from "./ToolStatusWidgets";
import { WorkspaceTabRailPanel, type WorkspaceTab, type WorkspaceTabKind } from "./WorkspaceTabs";
import { LayerPortal } from "../ui/layers/LayerPortal";
import { PromptSidebarWidget } from "./prompts/PromptSidebarWidget";
import type { ContextUsageInfo } from "../renderers/types";

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// Keep the persistent rail on a stable compositing layer. WKWebView can drop an
// inline SVG for a frame when a transformed rail button opens a fixed portal or
// when currentColor is interpolated while that portal is mounted. Keep feedback
// on the button surface and never animate the glyph's inherited color.
const RAIL_BUTTON_CLASS = "relative isolate flex h-10 min-h-10 w-10 min-w-10 shrink-0 items-center justify-center overflow-visible rounded-lg transition-[background-color,border-color,box-shadow] duration-150 ease-out [-webkit-tap-highlight-color:transparent]";
const PANEL_WIDTH_STORAGE_KEY = "rumi-right-sidebar-panel-width";
const PLACEMENT_PANEL_PREFIX = "__placement__:";
const DEFAULT_TOOL_GROUP_RAIL_LIMIT = 8;
const RAIL_MENU_GAP = 8;

type ToolSelectionScope = "turn" | "conversation";

type ToolServiceCard = {
  id: string;
  label: string;
  description: string;
  items: SidebarItem[];
};

const TOOL_SERVICE_LABELS: Record<string, { label: string; description: string }> = {
  web: { label: "Web検索", description: "Web、検索、オンライン情報" },
  github: { label: "GitHub", description: "リポジトリ、Issue、Pull Request" },
  files: { label: "Files", description: "ローカルファイルやドキュメント" },
  coding: { label: "Coding", description: "コード編集、ビルド、開発作業" },
  terminal: { label: "Terminal", description: "コマンド実行やジョブ操作" },
  browser: { label: "Browser", description: "ブラウザ、ページ、ダウンロード" },
  computer: { label: "PC操作", description: "画面上のアプリやPC操作" },
  calendar: { label: "Calendar", description: "予定やカレンダー" },
  gmail: { label: "Gmail", description: "メール検索や下書き" },
  slack: { label: "Slack", description: "Slackメッセージ" },
  google_drive: { label: "Google Drive", description: "Drive、Docs、Sheets、Slides" },
  notion: { label: "Notion", description: "NotionページやDB" },
  memory: { label: "Memory", description: "記憶、知識、会話コンテキスト" },
  artifacts: { label: "Artifacts", description: "成果物ファイルとプレビュー" },
  mcp: { label: "MCP", description: "MCP接続と外部サーバー機能" },
  system: { label: "System", description: "Rumi内部のシステム機能" },
  other: { label: "Other", description: "その他の機能" },
};

export function getRailFloatingMenuPosition(
  rect: Pick<DOMRect, "left" | "top">,
  options: {
    width?: number;
    height?: number;
    gap?: number;
    viewportWidth?: number;
    viewportHeight?: number;
  } = {},
): { top: number; right: number } {
  const width = options.width ?? 224;
  const height = options.height ?? 320;
  const gap = options.gap ?? RAIL_MENU_GAP;
  const viewportWidth = options.viewportWidth ?? (typeof window === "undefined" ? width + gap * 2 : window.innerWidth);
  const viewportHeight = options.viewportHeight ?? (typeof window === "undefined" ? height + gap * 2 : window.innerHeight);
  const maxTop = Math.max(gap, viewportHeight - height - gap);
  const maxRight = Math.max(gap, viewportWidth - width - gap);

  return {
    top: Math.max(gap, Math.min(rect.top, maxTop)),
    right: Math.max(gap, Math.min(viewportWidth - rect.left + gap, maxRight)),
  };
}

function clampPanelWidth(value: unknown): number {
  const numeric = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(numeric)) return 270;
  return Math.max(220, Math.min(520, numeric));
}

function requestedPanelIdFromActiveItemId(activeItemId?: string | null): string | null {
  return activeItemId?.split(":").slice(0, -1).join(":") || activeItemId || null;
}

function readStoredPanelWidth(): number {
  try {
    const raw = localStorage.getItem(PANEL_WIDTH_STORAGE_KEY);
    return raw ? clampPanelWidth(JSON.parse(raw)) : 270;
  } catch {
    return 270;
  }
}

function readStoredStringArray(key: string): string[] {
  try {
    const raw = localStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.map((item) => String(item)).filter(Boolean) : [];
  } catch {
    return [];
  }
}

function settingStringArray(value: unknown, fallbackKey?: string): string[] {
  if (Array.isArray(value)) {
    return [...new Set(value.map((item) => String(item)).filter(Boolean))];
  }
  return fallbackKey ? readStoredStringArray(fallbackKey) : [];
}

function settingStringArrayRecord(value: unknown, fallbackKey?: string): Record<string, string[]> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return fallbackKey ? readStoredStringArrayRecord(fallbackKey) : {};
  }
  return Object.fromEntries(Object.entries(value).map(([id, values]) => [
    id,
    Array.isArray(values) ? [...new Set(values.map((item) => normalizeTag(String(item))).filter(Boolean))] : [],
  ]).filter(([, values]) => values.length > 0));
}

function readStoredStringArrayRecord(key: string): Record<string, string[]> {
  try {
    const raw = localStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : {};
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(Object.entries(parsed).map(([id, values]) => [
      id,
      Array.isArray(values) ? [...new Set(values.map((value) => normalizeTag(String(value))).filter(Boolean))] : [],
    ]).filter(([, values]) => values.length > 0));
  } catch {
    return {};
  }
}

const CATEGORY_META: Record<SidebarCategory | "all", { label: string; icon: ReactElement }> = {
  all: { label: "All", icon: <Layers size={16} /> },
  activity: { label: "Activities", icon: <Route size={16} /> },
  tool: { label: "Advanced Tools", icon: <Wrench size={16} /> },
  widget: { label: "Widgets", icon: <LayoutGrid size={16} /> },
  system: { label: "System", icon: <Settings size={16} /> },
  integration: { label: "Integrations", icon: <Blocks size={16} /> },
  capability: { label: "Capabilities", icon: <ShieldCheck size={16} /> },
};

const TOOL_GROUP_ICONS: Record<string, ReactElement> = {
  agent: <Cpu size={16} />,
  browser: <Monitor size={16} />,
  build: <Hammer size={16} />,
  coding: <Code2 size={16} />,
  computer: <Monitor size={16} />,
  file: <FileText size={16} />,
  git: <GitBranch size={16} />,
  planning: <ListTodo size={16} />,
  research: <Search size={16} />,
  operate: <Monitor size={16} />,
  manage: <Cpu size={16} />,
  terminal: <Terminal size={16} />,
  other: <Wrench size={16} />,
};

const TOOL_GROUP_LABELS: Record<string, string> = {
  coding: "Coding",
  research: "調べる",
  operate: "操作する",
  manage: "管理",
  workspace_files: "Files",
  workspace_git: "Git",
  workspace_terminal: "Terminal",
  other: "その他",
};

const ITEM_ICONS: Record<string, ReactElement> = {
  agent: <Cpu size={18} />,
  artifacts: <Archive size={18} />,
  browser: <Monitor size={18} />,
  browser_companion: <AppWindow size={18} />,
  browser_use: <AppWindow size={18} />,
  browser_computer: <MousePointerClick size={18} />,
  browser_open_url: <Globe size={18} />,
  browser_screenshot: <Monitor size={18} />,
  calculator: <BrainCircuit size={18} />,
  coding_context: <Code2 size={18} />,
  coding_file_create: <FilePlus2 size={18} />,
  coding_file_delete: <Trash2 size={18} />,
  coding_file_list: <FileSearch size={18} />,
  coding_file_read: <FileText size={18} />,
  coding_file_search: <FileSearch size={18} />,
  coding_file_write: <FilePenLine size={18} />,
  coding_git_branch_create: <GitBranch size={18} />,
  coding_git_branch_get: <GitBranch size={18} />,
  coding_git_commit: <GitCommit size={18} />,
  coding_git_diff: <GitCompare size={18} />,
  coding_git_push: <Share2 size={18} />,
  coding_git_status: <GitBranch size={18} />,
  coding_terminal_exec: <Terminal size={18} />,
  coding_terminal_stream: <Terminal size={18} />,
  computer_click: <Monitor size={18} />,
  computer_key: <KeyRound size={18} />,
  computer_screenshot: <Monitor size={18} />,
  computer_scroll: <Monitor size={18} />,
  computer_type: <Monitor size={18} />,
  computer_use: <Monitor size={18} />,
  code: <Terminal size={18} />,
  file: <FileText size={18} />,
  file_reader: <FileText size={18} />,
  files: <FileText size={18} />,
  git: <GitBranch size={18} />,
  image: <Image size={18} />,
  inspector: <Cpu size={18} />,
  knowledge: <Search size={18} />,
  knowledge_index: <Database size={18} />,
  knowledge_search: <Search size={18} />,
  media_image_analyze: <Image size={18} />,
  media_pdf_parse: <FileText size={18} />,
  memory: <Cpu size={18} />,
  "mouse-pointer-click": <MousePointerClick size={18} />,
  music: <Music size={18} />,
  news: <Newspaper size={18} />,
  notebook: <NotebookPen size={18} />,
  provider: <Blocks size={18} />,
  providers: <Blocks size={18} />,
  research_local_search: <Search size={18} />,
  research_reddit_search: <Search size={18} />,
  research_search_sources: <Search size={18} />,
  research_web_search: <Globe size={18} />,
  search: <Globe size={18} />,
  tool_invoke: <Wrench size={18} />,
  tool_list: <ListTodo size={18} />,
  tool_schema: <Route size={18} />,
  tool_web_search: <Globe size={18} />,
  tool_reddit_search: <Search size={18} />,
  translate: <Languages size={18} />,
  web: <Globe size={18} />,
  web_search: <Search size={18} />,
  reddit_search: <Search size={18} />,
};

const ACTION_ICONS: Record<string, ReactElement> = {
  artifacts: <Archive size={13} />,
  browser: <Monitor size={13} />,
  channels: <MessageSquareText size={13} />,
  export: <Download size={13} />,
  play: <Play size={13} />,
  reddit: <Search size={13} />,
  schedules: <CalendarClock size={13} />,
  share: <Share2 size={13} />,
  web: <Globe size={13} />,
};

const SIDEBAR_CATEGORY_ORDER: Record<SidebarCategory, number> = {
  widget: 0,
  activity: 1,
  capability: 2,
  integration: 3,
  system: 4,
  tool: 5,
};

function compareText(left: string, right: string): number {
  return left.localeCompare(right, undefined, { numeric: true, sensitivity: "base" });
}

function normalizeTag(value: string): string {
  return value.trim().toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9_:-]/g, "").slice(0, 32);
}

function sidebarItemMatchesSearch(item: SidebarItem, tags: string[], query: string): boolean {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return true;
  const group = item.category === "tool" ? toolGroupFor(item) : null;
  const haystack = [
    item.id,
    item.label,
    item.category,
    item.description ?? "",
    item.badge ?? "",
    ...(item.tags ?? []),
    ...tags,
    item.ui?.composer_label ?? "",
    item.ui?.composer_description ?? "",
    item.ui?.group_id ?? "",
    item.ui?.group_label ?? "",
    group?.id ?? "",
    group?.label ?? "",
    ...(group?.path ?? []),
  ].join(" ").toLowerCase();
  return terms.every((term) => haystack.includes(term));
}

export function toolManagerBaseItemsForNameSearch(
  items: SidebarItem[],
  searchFilteredItems: SidebarItem[],
  searchQuery: string,
): SidebarItem[] {
  const sourceItems = searchQuery.trim() ? searchFilteredItems : items;
  return sortedToolUiItems(sourceItems.filter((item) => item.category === "tool"));
}

export function shouldShowToolManagerEmptyState({
  toolCount,
  sidebarSearchQuery,
  toolManagerSearchQuery,
  activeTagFilter,
  showStarredOnly,
}: {
  toolCount: number;
  sidebarSearchQuery: string;
  toolManagerSearchQuery: string;
  activeTagFilter: string | null;
  showStarredOnly: boolean;
}): boolean {
  if (toolCount > 0) return false;
  return Boolean(sidebarSearchQuery.trim() || toolManagerSearchQuery.trim() || activeTagFilter || showStarredOnly);
}

function baseTagsForItem(item: SidebarItem): string[] {
  const tags = [...(item.tags ?? [])].map((tag) => normalizeTag(String(tag))).filter(Boolean);
  const risk = normalizeTag(String(item.risk ?? ""));
  if (risk) tags.push(`risk:${risk}`);
  if (risk === "high") tags.push("danger");
  const haystack = `${item.id} ${item.label} ${item.description ?? ""} ${tags.join(" ")}`.toLowerCase();
  if (/(write|delete|patch|restore|terminal|shell|push|commit|exec|approval|danger)/.test(haystack)) {
    tags.push("danger");
  }
  return [...new Set(tags)];
}

function serviceIdForTool(item: SidebarItem): string {
  const ui = item.ui as Record<string, unknown> | undefined;
  const explicit = String(ui?.service_id ?? "").trim().toLowerCase();
  if (explicit && TOOL_SERVICE_LABELS[explicit]) return explicit;
  const haystack = `${item.id} ${item.label} ${item.description ?? ""} ${(item.tags ?? []).join(" ")} ${item.ui?.group_id ?? ""}`.toLowerCase();
  const rules: Array<[string, RegExp]> = [
    ["github", /github|pull_request|pr_|issue/],
    ["gmail", /gmail|email|mail/],
    ["slack", /slack/],
    ["google_drive", /google_drive|drive|slides|sheet|doc_/],
    ["calendar", /calendar/],
    ["notion", /notion/],
    ["browser", /browser|html_preview|webapp_preview/],
    ["computer", /computer_use|screen|mouse|keyboard|click/],
    ["terminal", /terminal|sandbox_exec|python_exec|node_exec|command|shell/],
    ["coding", /coding|workspace|git_|webapp_build|webapp_lint|project_scaffold|package_install/],
    ["files", /file|pdf|doc|ocr|audio_transcribe|image_convert|image_resize/],
    ["artifacts", /artifact|export|zip|preview/],
    ["memory", /memory|knowledge|source_rank|source_extract/],
    ["web", /web_search|reddit|research|wide_research/],
    ["mcp", /mcp__/],
    ["system", /workflow|job_|tts_generate|image_generate|tool_search/],
  ];
  for (const [serviceId, pattern] of rules) {
    if (pattern.test(haystack)) return serviceId;
  }
  return "other";
}

function toolServiceCards(items: SidebarItem[]): ToolServiceCard[] {
  const groups = new Map<string, SidebarItem[]>();
  for (const item of items) {
    const serviceId = serviceIdForTool(item);
    groups.set(serviceId, [...(groups.get(serviceId) ?? []), item]);
  }
  return [...groups.entries()]
    .map(([id, groupItems]) => ({
      id,
      label: TOOL_SERVICE_LABELS[id]?.label ?? id,
      description: TOOL_SERVICE_LABELS[id]?.description ?? "追加機能",
      items: sortedToolUiItems(groupItems),
    }))
    .sort((left, right) => right.items.length - left.items.length || compareText(left.label, right.label));
}

function targetList(value: unknown): Array<{ kind: string; id: string }> {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    if (typeof item === "string") return { kind: "tool", id: item };
    if (item && typeof item === "object") {
      const record = item as Record<string, unknown>;
      return {
        kind: String(record.kind ?? record.type ?? "tool"),
        id: String(record.id ?? record.tool_id ?? record.service_id ?? ""),
      };
    }
    return { kind: "tool", id: "" };
  }).filter((item) => item.id.trim());
}

function compareSidebarItems(left: SidebarItem, right: SidebarItem): number {
  if (left.category === "tool" && right.category === "tool") {
    return compareToolUiItems(left, right);
  }
  return (
    SIDEBAR_CATEGORY_ORDER[left.category] - SIDEBAR_CATEGORY_ORDER[right.category]
    || compareText(left.label || left.id, right.label || right.id)
    || compareText(left.id, right.id)
  );
}

function actionIcon(action: SidebarAction) {
  const key = action.icon || action.id.split(".")[0] || "play";
  return ACTION_ICONS[key] ?? <Play size={13} />;
}

export function sidebarActionDisabledReason(action: SidebarAction, activeConversationId?: string | null): string {
  if (activeConversationId) return "";
  if (action.id === "conversation.export") {
    return "エクスポートする会話がありません。会話を保存してから実行してください。";
  }
  if (action.id === "conversation.share") {
    return "共有する会話がありません。会話を保存してから実行してください。";
  }
  return "";
}

function iconForItem(item: SidebarItem) {
  const display = resolveCatalogDisplay(item, "sidebar");
  if (display.image) {
    return (
      <img
        src={display.image}
        alt=""
        loading="lazy"
        decoding="async"
        draggable={false}
        className="h-[18px] w-[18px] rounded object-cover"
      />
    );
  }
  const declaredIcon = display.icon;
  if (declaredIcon && ITEM_ICONS[declaredIcon]) return ITEM_ICONS[declaredIcon];

  // Legacy fallback for pre-ui metadata tools.
  const direct = item.id.toLowerCase();
  if (ITEM_ICONS[direct]) return ITEM_ICONS[direct];
  const normalized = item.label.toLowerCase().replace(/\s+/g, "_");
  if (ITEM_ICONS[normalized]) return ITEM_ICONS[normalized];
  const byCategory: Record<SidebarCategory, ReactElement> = {
    activity: <Route size={18} />,
    tool: <Wrench size={18} />,
    widget: <LayoutGrid size={18} />,
    system: <Cpu size={18} />,
    integration: <Blocks size={18} />,
    capability: <ShieldCheck size={18} />,
  };
  return byCategory[item.category];
}

function railIcon(item: ReactElement, size = 18): ReactElement {
  const props = item.props as Record<string, unknown>;
  return cloneElement(item as ReactElement<Record<string, unknown>>, {
    size,
    className: cn("h-5 w-5 shrink-0", typeof props.className === "string" ? props.className : undefined),
  });
}

const StableToolGroupRailGlyph = memo(function StableToolGroupRailGlyph({
  iconName,
  groupId,
}: {
  iconName?: string;
  groupId: string;
}) {
  const icon = (iconName && TOOL_GROUP_ICONS[iconName]) || TOOL_GROUP_ICONS[groupId] || TOOL_GROUP_ICONS.other;
  return (
    <span
      aria-hidden="true"
      className="rumi-rail-stable-glyph pointer-events-none flex h-5 w-5 shrink-0 items-center justify-center text-zinc-400 [backface-visibility:hidden] [transform:translateZ(0)] [will-change:transform] [&>svg]:block"
    >
      {railIcon(icon, 20)}
    </span>
  );
});

function categoryColor(cat: SidebarCategory, variant: "bg" | "indicator" | "dot" | "badge") {
  const map: Record<SidebarCategory, Record<string, string>> = {
    activity: { bg: "bg-fuchsia-500", indicator: "bg-fuchsia-500", dot: "bg-fuchsia-500/60", badge: "bg-fuchsia-500/20 text-fuchsia-300" },
    tool: { bg: "bg-emerald-500", indicator: "bg-emerald-500", dot: "bg-emerald-500/60", badge: "bg-emerald-500/20 text-emerald-400" },
    widget: { bg: "bg-blue-500", indicator: "bg-blue-500", dot: "bg-blue-500/60", badge: "bg-blue-500/20 text-blue-400" },
    system: { bg: "bg-amber-500", indicator: "bg-amber-500", dot: "bg-amber-500/60", badge: "bg-amber-500/20 text-amber-400" },
    integration: { bg: "bg-violet-500", indicator: "bg-violet-500", dot: "bg-violet-500/60", badge: "bg-violet-500/20 text-violet-400" },
    capability: { bg: "bg-cyan-500", indicator: "bg-cyan-500", dot: "bg-cyan-500/60", badge: "bg-cyan-500/20 text-cyan-300" },
  };
  return map[cat]?.[variant] ?? "";
}

function ToggleSwitch({ value, label, onChange }: { value: boolean; label: string; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={value}
      aria-label={label}
      onClick={() => onChange(!value)}
      className={cn("relative h-5 w-9 flex-shrink-0 rounded-full transition-colors", value ? "bg-emerald-500" : "bg-zinc-700")}
    >
      <span aria-hidden="true" className={cn("absolute top-1 h-3 w-3 rounded-full bg-white transition-transform", value ? "translate-x-5" : "translate-x-1")} />
    </button>
  );
}

function FieldControl({
  field,
  value,
  onChange,
}: {
  field: SidebarField;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  if (field.type === "toggle") {
    return <ToggleSwitch value={Boolean(value)} label={field.label} onChange={onChange} />;
  }

  if (field.type === "select") {
    return (
      <select
        value={String(value ?? field.default ?? "")}
        onChange={(event) => onChange(event.target.value)}
        className="bg-zinc-800 border border-zinc-700 text-zinc-200 text-xs rounded px-2 py-1 outline-none"
      >
        {(field.options ?? []).map((option) => (
          <option key={String(option.value)} value={String(option.value)}>
            {option.label}
          </option>
        ))}
      </select>
    );
  }

  if (field.type === "number") {
    return (
      <input
        type="number"
        value={Number(value ?? field.default ?? 0)}
        min={field.min}
        max={field.max}
        onChange={(event) => onChange(Number(event.target.value))}
        className="bg-zinc-800 border border-zinc-700 text-zinc-200 text-xs rounded px-2 py-1 w-20 outline-none text-right"
      />
    );
  }

  if (field.type === "color") {
    const rawValue = String(value ?? field.default ?? "#FFFFFF");
    const colorValue = /^#[0-9a-fA-F]{6}$/.test(rawValue) ? rawValue : "#FFFFFF";
    return (
      <div className="flex min-w-0 items-center gap-2">
        <input
          type="color"
          value={colorValue}
          onChange={(event) => onChange(event.target.value.toUpperCase())}
          className="h-8 w-9 shrink-0 cursor-pointer rounded border border-zinc-700 bg-zinc-800 p-0.5"
          aria-label={field.label}
        />
        <input
          type="text"
          value={rawValue}
          onChange={(event) => onChange(event.target.value)}
          className="bg-zinc-800 border border-zinc-700 text-zinc-200 text-xs rounded px-2 py-1 outline-none min-w-0 w-24 font-mono"
        />
      </div>
    );
  }

  if (field.type === "readonly") {
    return <span className="text-xs text-zinc-300 font-mono">{String(value ?? field.default ?? "")}</span>;
  }

  if (field.type === "textarea") {
    return (
      <textarea
        value={String(value ?? field.default ?? "")}
        onChange={(event) => onChange(event.target.value)}
        className="w-full h-24 bg-zinc-900 border border-zinc-800 rounded-lg p-2 text-xs text-zinc-300 resize-none focus:border-zinc-600 outline-none"
      />
    );
  }

  return (
    <input
      type="text"
      value={String(value ?? field.default ?? "")}
      onChange={(event) => onChange(event.target.value)}
      className="bg-zinc-800 border border-zinc-700 text-zinc-200 text-xs rounded px-2 py-1 outline-none min-w-0"
    />
  );
}

function SidebarPanel({
  item,
  settingsValues,
  activeConversationId,
  onSettingChange,
  onPanelAction,
}: {
  item: SidebarItem;
  settingsValues: Record<string, Record<string, unknown>>;
  activeConversationId?: string | null;
  onSettingChange: (sectionId: string, fieldId: string, value: unknown) => void;
  onPanelAction?: (item: SidebarItem, action: SidebarAction) => void;
}) {
  const panel = item.panel;
  const fields = panel?.fields ?? [];
  const primaryFields = fields.filter((field) => !field.advanced);
  const advancedFields = fields.filter((field) => field.advanced);
  const actions = panel?.actions ?? [];
  const renderField = (field: SidebarField) => {
    const value =
      settingsValues[item.id]?.[field.id] ??
      settingsValues.tools?.[`${item.id}.${field.id}`] ??
      field.default;
    return (
      <div key={field.id} className="space-y-1">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[11px] text-zinc-400">{field.label}</span>
          <FieldControl
            field={field}
            value={value}
            onChange={(nextValue) => onSettingChange(item.id, field.id, nextValue)}
          />
        </div>
        {field.help && <p className="text-[9px] text-zinc-600 leading-relaxed">{field.help}</p>}
      </div>
    );
  };

  return (
    <div className="space-y-3">
      {item.origin && (
        <div className="p-2.5 rounded-lg border border-zinc-800/60 bg-zinc-900/30 space-y-1">
          <p className="text-[9px] uppercase tracking-wider text-zinc-500">Origin</p>
          <div className="text-[11px] text-zinc-300">{item.origin.kind}</div>
          {item.origin.path && (
            <div className="text-[10px] text-zinc-500 font-mono break-all">{item.origin.path}</div>
          )}
        </div>
      )}

      {primaryFields.length > 0 && (
        <div className="space-y-2.5">
          {primaryFields.map(renderField)}
        </div>
      )}

      {advancedFields.length > 0 && (
        <details className="rounded-lg border border-zinc-800/70 bg-zinc-950/35 px-2.5 py-2">
          <summary className="cursor-pointer select-none text-[10px] font-medium text-zinc-500 hover:text-zinc-300">
            高度な設定
          </summary>
          <div className="mt-2 space-y-2.5">{advancedFields.map(renderField)}</div>
        </details>
      )}

      {panel?.models && panel.models.length > 0 && (
        <div>
          <h4 className="text-[10px] font-semibold text-zinc-500 uppercase tracking-wider mb-1.5">Models</h4>
          <div className="space-y-1 max-h-48 overflow-y-auto pr-1">
            {panel.models.map((model) => (
              <div
                key={model.id}
                draggable
                onDragStart={(e) => {
                  e.dataTransfer.setData("application/rumi-widget", JSON.stringify({ id: model.id, type: "model", label: model.name ?? model.id }));
                  e.dataTransfer.effectAllowed = "copy";
                }}
                className="p-1.5 rounded border border-zinc-800/60 bg-zinc-900/30 cursor-grab active:cursor-grabbing hover:border-zinc-700/60 transition-colors"
              >
                <p className="text-[10px] text-zinc-300 font-mono">{model.id}</p>
                {model.name && <p className="text-[9px] text-zinc-500 mt-0.5">{model.name}</p>}
              </div>
            ))}
          </div>
        </div>
      )}

      {actions.length > 0 && (
        <div>
          <h4 className="text-[10px] font-semibold text-zinc-500 uppercase tracking-wider mb-1.5">Actions</h4>
          <div className="grid grid-cols-1 gap-1">
            {actions.map((action) => {
              const disabledReason = sidebarActionDisabledReason(action, activeConversationId);
              const disabled = Boolean(disabledReason);
              return (
                <button
                  key={action.id}
                  disabled={disabled}
                  aria-disabled={disabled}
                  onClick={() => {
                    if (disabled) return;
                    onPanelAction?.(item, action);
                  }}
                  className={cn(
                    "h-7 px-2 rounded border transition-colors flex items-center gap-1.5 text-[11px] text-left",
                    disabled
                      ? "cursor-not-allowed border-zinc-900/80 bg-zinc-950/50 text-zinc-600"
                      : "border-zinc-800/70 bg-zinc-900/40 text-zinc-300 hover:bg-zinc-800/70 hover:text-zinc-100",
                  )}
                  title={disabledReason || action.label}
                >
                  <span className={cn("flex-shrink-0", disabled ? "text-zinc-700" : "text-zinc-500")}>{actionIcon(action)}</span>
                  <span className="truncate">{action.label}</span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {panel?.notes && panel.notes.length > 0 && (
        <div>
          <h4 className="text-[10px] font-semibold text-zinc-500 uppercase tracking-wider mb-1.5">Notes</h4>
          <div className="space-y-1">
            {panel.notes.map((note) => (
              <div key={note} className="p-1.5 rounded border border-zinc-800/60 bg-zinc-900/30 text-[10px] text-zinc-400 leading-relaxed">
                {note}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function CategorySwitcher({
  active,
  counts,
  onChange,
  keyboardButtonNavigation = true,
}: {
  active: "all" | SidebarCategory;
  counts: Record<string, number>;
  onChange: (id: "all" | SidebarCategory) => void;
  keyboardButtonNavigation?: boolean;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const current = CATEGORY_META[active];
  const hasActiveFilter = active !== "all";
  const buttonTabIndex = keyboardButtonNavigation ? undefined : -1;
  const rect = buttonRef.current?.getBoundingClientRect();
  const menuPosition = rect ? getRailFloatingMenuPosition(rect, { width: 150, height: 220 }) : null;

  return (
    <div className="relative">
      <button
        ref={buttonRef}
        type="button"
        tabIndex={buttonTabIndex}
        onClick={() => setIsOpen((value) => !value)}
        aria-expanded={isOpen}
        aria-pressed={hasActiveFilter}
        className={cn(
          RAIL_BUTTON_CLASS,
          hasActiveFilter
            ? "bg-zinc-800 text-zinc-100 ring-1 ring-zinc-600/70"
            : "text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800/50",
          isOpen && "bg-zinc-800 text-zinc-100",
        )}
        title={`Filter: ${current.label}`}
      >
        {railIcon(current.icon, 18)}
        {hasActiveFilter && (
          <span className="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-r-full bg-sky-400" />
        )}
      </button>

      {isOpen && (
        <LayerPortal layer="modal">
          <div className="fixed inset-0 rumi-layer-global-overlay" onClick={() => setIsOpen(false)} />
          <div
            className="fixed rumi-layer-modal bg-zinc-900 border border-zinc-800 rounded-lg shadow-xl overflow-hidden min-w-[150px]"
            style={menuPosition ? { top: `${menuPosition.top}px`, right: `${menuPosition.right}px` } : undefined}
          >
            <div className="px-2 py-1.5 border-b border-zinc-800/60">
              <p className="text-[9px] font-semibold text-zinc-500 uppercase tracking-wider">表示フィルター</p>
            </div>
            <div className="py-0.5">
              {(["all", "widget", "activity", "capability", "integration", "system", "tool"] as const).map((filterId) => {
                const count = counts[filterId] ?? 0;
                if (filterId !== "all" && count === 0) return null;
                return (
                          <button
                            key={filterId}
                            type="button"
                            tabIndex={buttonTabIndex}
                            onClick={() => {
                      onChange(filterId);
                      setIsOpen(false);
                    }}
                    className={cn("w-full flex items-center gap-2 px-2.5 py-1.5 text-left transition-colors", active === filterId ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-200")}
                  >
                    <span className="flex-shrink-0">{CATEGORY_META[filterId].icon}</span>
                    <span className="text-[11px] font-medium flex-1">{CATEGORY_META[filterId].label}</span>
                    <span className="text-[9px] text-zinc-600 bg-zinc-800 px-1 py-0.5 rounded">{count}</span>
                  </button>
                );
              })}
            </div>
          </div>
        </LayerPortal>
      )}
    </div>
  );
}

function SidebarSearchControl({
  query,
  resultCount,
  totalCount,
  onQueryChange,
  keyboardButtonNavigation = true,
}: {
  query: string;
  resultCount: number;
  totalCount: number;
  onQueryChange: (value: string) => void;
  keyboardButtonNavigation?: boolean;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const hasQuery = query.trim().length > 0;
  const buttonTabIndex = keyboardButtonNavigation ? undefined : -1;
  const rect = buttonRef.current?.getBoundingClientRect();
  const menuPosition = rect ? getRailFloatingMenuPosition(rect, { width: 256, height: 100 }) : null;

  useEffect(() => {
    if (isOpen) {
      window.setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [isOpen]);

  return (
    <div className="relative">
      <button
        ref={buttonRef}
        type="button"
        tabIndex={buttonTabIndex}
        onClick={() => setIsOpen((value) => !value)}
        aria-expanded={isOpen}
        aria-pressed={hasQuery}
        className={cn(
          RAIL_BUTTON_CLASS,
          hasQuery || isOpen
            ? "bg-zinc-800 text-zinc-100 ring-1 ring-zinc-600/70"
            : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50",
        )}
        title="名前検索"
      >
        <Search size={16} className="h-4 w-4 shrink-0" />
        {hasQuery && (
          <span className="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-r-full bg-cyan-400" />
        )}
      </button>

      {isOpen && (
        <LayerPortal layer="modal">
          <div className="fixed inset-0 rumi-layer-global-overlay" onClick={() => setIsOpen(false)} />
          <div
            className="fixed rumi-layer-modal w-64 rounded-xl border border-zinc-700/70 bg-zinc-950 p-2 shadow-2xl"
            style={menuPosition ? { top: `${menuPosition.top}px`, right: `${menuPosition.right}px` } : undefined}
          >
            <label className="relative block">
              <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-600" />
              <input
                ref={inputRef}
                type="text"
                value={query}
                onChange={(event) => onQueryChange(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Escape") {
                    if (query) {
                      onQueryChange("");
                    } else {
                      setIsOpen(false);
                    }
                  }
                }}
                placeholder="名前で検索"
                className="h-9 w-full rounded-lg border border-zinc-800 bg-zinc-900/80 pl-8 pr-8 text-[12px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
              />
              {hasQuery && (
                <button
                          type="button"
                          tabIndex={buttonTabIndex}
                          onClick={() => onQueryChange("")}
                  className="absolute right-1.5 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"
                  title="検索をクリア"
                >
                  <X size={13} />
                </button>
              )}
            </label>
            <div className="mt-2 flex items-center justify-between px-1 text-[10px] text-zinc-600">
              <span>matches</span>
              <span>{resultCount} / {totalCount}</span>
            </div>
          </div>
        </LayerPortal>
      )}
    </div>
  );
}

export function RightSidebar({
  items,
  activeItemId,
  settingsValues,
  settingsSections,
  selectedToolIds = [],
  companyPanel,
  codingPanel,
  keyboardButtonNavigation = true,
  selectedProfile = null,
  toolFilterEntries = [],
  runtimeCapabilitySnapshot = null,
  contextUsage = null,
  promptUsage = null,
  promptProfileId,
  conversationId = null,
  showChatPromptUsage = true,
  yoloMode = false,
  workspaceTabs = [],
  activeWorkspaceTabId = null,
  activeConversationId = null,
  onSettingChange,
  onOpenSettings,
  onOpenSettingsSection,
  onToggleYolo,
  onWorkspaceTabSelect,
  onWorkspaceTabClose,
  onWorkspaceTabCreate,
  onLoadPromptActive,
  onTogglePromptEdge,
  onToggleChatPromptUsage,
  onToolToggle,
  onToolBatchSet,
  onPanelAction,
}: {
  items: SidebarItem[];
  activeItemId?: string | null;
  settingsValues: Record<string, Record<string, unknown>>;
  settingsSections: SettingsSection[];
  selectedToolIds?: string[];
  companyPanel?: ReactNode;
  codingPanel?: ReactNode;
  keyboardButtonNavigation?: boolean;
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
  onSettingChange: (sectionId: string, fieldId: string, value: unknown) => void;
  onOpenSettings: () => void;
  onOpenSettingsSection?: (sectionId: string) => void;
  onToggleYolo?: () => void;
  onWorkspaceTabSelect?: (tabId: string) => void;
  onWorkspaceTabClose?: (tabId: string) => void;
  onWorkspaceTabCreate?: (kind: WorkspaceTabKind) => void;
  onLoadPromptActive?: (params: { profile_id?: string; conversation_id?: string; include_text?: boolean; model_profile_id?: string; model?: string }) => Promise<PromptUsageSummary>;
  onTogglePromptEdge?: (payload: { profile_id?: string; conversation_id?: string; edge_id: string; enabled: boolean; model_profile_id?: string; model?: string }) => Promise<PromptUsageSummary>;
  onToggleChatPromptUsage?: (visible: boolean) => void;
  onToolToggle?: (item: SidebarItem) => void;
  onToolBatchSet?: (toolIds: string[], enabled: boolean) => void;
  onPanelAction?: (item: SidebarItem, action: SidebarAction) => void;
}) {
  const [activePanel, setActivePanel] = useState<string | null>(() => requestedPanelIdFromActiveItemId(activeItemId));
  const [categoryFilter, setCategoryFilter] = useState<"all" | SidebarCategory>("activity");
  const [searchQuery, setSearchQuery] = useState("");
  const [toolManagerSearchQuery, setToolManagerSearchQuery] = useState("");
  const [isToolManagerSearchOpen, setIsToolManagerSearchOpen] = useState(false);
  const [toolSelectionScope, setToolSelectionScope] = useState<ToolSelectionScope>("turn");
  const [conversationToolPreferences, setConversationToolPreferences] = useState<Record<string, unknown>>({});
  const [openToolGroupMenu, setOpenToolGroupMenu] = useState<string | null>(null);
  const [toolGroupMenuPosition, setToolGroupMenuPosition] = useState<{ top: number; right: number } | null>(null);
  const [panelWidth, setPanelWidth] = useState(readStoredPanelWidth);
  const [placementMenuOpen, setPlacementMenuOpen] = useState(false);
  const [placementMenuPosition, setPlacementMenuPosition] = useState<{ top: number; right: number } | null>(null);
  const sidebarSettings = settingsValues.sidebar ?? {};
  const toolsSettings = settingsValues.tools ?? {};
  const pinnedItemIds = useMemo(
    () => settingStringArray(sidebarSettings.pinned_item_ids, "rumi-sidebar-pinned-item-ids"),
    [sidebarSettings.pinned_item_ids],
  );
  const pinnedPlacements = useMemo(
    () => normalizePinnedPlacements(sidebarSettings.ui_placements),
    [sidebarSettings.ui_placements],
  );
  const starredItemIds = useMemo(
    () => settingStringArray(sidebarSettings.starred_item_ids, "rumi-sidebar-starred-item-ids"),
    [sidebarSettings.starred_item_ids],
  );
  const disabledToolIds = useMemo(
    () => settingStringArray(toolsSettings.disabled_tool_ids),
    [toolsSettings.disabled_tool_ids],
  );
  const hiddenToolIds = useMemo(
    () => settingStringArray(toolsSettings.hidden_tool_ids),
    [toolsSettings.hidden_tool_ids],
  );
  const customTagMap = useMemo(
    () => settingStringArrayRecord(sidebarSettings.custom_tool_tags, "rumi-tool-custom-tags"),
    [sidebarSettings.custom_tool_tags],
  );
  const [showStarredOnly, setShowStarredOnly] = useState(false);
  const [activeTagFilter, setActiveTagFilter] = useState<string | null>(null);
  const [tagDraftByItemId, setTagDraftByItemId] = useState<Record<string, string>>({});
  const [contextMenu, setContextMenu] = useState<{ itemId: string; x: number; y: number } | null>(null);
  const toolManagerSearchRef = useRef<HTMLDivElement | null>(null);
  const toolGroupMenuRef = useRef<HTMLDivElement | null>(null);
  const toolGroupFloatingMenuRef = useRef<HTMLDivElement | null>(null);
  const placementMenuRef = useRef<HTMLDivElement | null>(null);
  const contextMenuRef = useRef<HTMLDivElement | null>(null);
  const buttonTabIndex = keyboardButtonNavigation ? undefined : -1;
  const selectedToolIdSet = useMemo(() => new Set(selectedToolIds), [selectedToolIds]);
  const pinnedItemIdSet = useMemo(() => new Set(pinnedItemIds), [pinnedItemIds]);
  const starredItemIdSet = useMemo(() => new Set(starredItemIds), [starredItemIds]);
  const panelWidthPx = clampPanelWidth(panelWidth);
  const hasPromptWidget = Boolean(onLoadPromptActive && onTogglePromptEdge);
  const promptRailCount = Number(promptUsage?.active_count ?? promptUsage?.segments?.filter((segment) => segment.status === "active").length ?? promptUsage?.active_segments?.length ?? 0);
  const disabledToolIdSet = useMemo(() => new Set(disabledToolIds), [disabledToolIds]);
  const hiddenToolIdSet = useMemo(() => new Set(hiddenToolIds), [hiddenToolIds]);
  const placementManifestMap = useMemo(
    () => new Map(buildBuiltinPlacementManifests(settingsSections).map((manifest) => [manifest.id, manifest])),
    [settingsSections],
  );
  const rightSidebarPlacementCandidates = useMemo(() => (
    filterPlacementCandidates([...placementManifestMap.values()], {
      surface: "right_sidebar",
      orientation: "vertical",
      configurableOnly: true,
    }).filter((manifest) => !pinnedPlacements.some((placement) => (
      placement.id === manifest.id && placement.surface === "right_sidebar"
    )))
  ), [pinnedPlacements, placementManifestMap]);

  useEffect(() => {
    try {
      localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, JSON.stringify(panelWidthPx));
    } catch {
      // Storage may be unavailable in restricted browser contexts.
    }
  }, [panelWidthPx]);

  useEffect(() => {
    if (!activeConversationId) {
      setConversationToolPreferences({});
      setToolSelectionScope("turn");
      return;
    }
    let cancelled = false;
    toolResources.getConversationToolPreferences(activeConversationId)
      .then((result) => {
        if (!cancelled) setConversationToolPreferences(result.preferences ?? {});
      })
      .catch(() => {
        if (!cancelled) setConversationToolPreferences({});
      });
    return () => {
      cancelled = true;
    };
  }, [activeConversationId]);

  const startPanelResize = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      event.preventDefault();
      const startX = event.clientX;
      const startWidth = panelWidthPx;
      const handlePointerMove = (moveEvent: PointerEvent) => {
        setPanelWidth(clampPanelWidth(startWidth + (startX - moveEvent.clientX)));
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
    [panelWidthPx],
  );

  const resizePanelWithKeyboard = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 48 : 16;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      setPanelWidth(clampPanelWidth(panelWidthPx + step));
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      setPanelWidth(clampPanelWidth(panelWidthPx - step));
    } else if (event.key === "Home") {
      event.preventDefault();
      setPanelWidth(clampPanelWidth(220));
    } else if (event.key === "End") {
      event.preventDefault();
      setPanelWidth(clampPanelWidth(520));
    }
  }, [panelWidthPx]);

  useEffect(() => {
    const requestedId = requestedPanelIdFromActiveItemId(activeItemId);
    const specialPanelIds = new Set([
      "__tool_manager__",
      "__tool_filter_log__",
      "__runtime_status__",
      "__context_usage__",
      "__company_workspace__",
      "__coding_widget__",
      "__workspace_tabs__",
      "__prompt_usage__",
    ]);
    if (requestedId && (items.some((item) => item.id === requestedId) || (requestedId !== "__prompt_usage__" && specialPanelIds.has(requestedId)) || (requestedId === "__prompt_usage__" && hasPromptWidget))) {
      setActivePanel(requestedId);
    }
  }, [activeItemId, hasPromptWidget, items]);

  useEffect(() => {
    if (!activePanel) return;
    if (activePanel === "__tool_manager__") return;
    if (activePanel === "__tool_filter_log__") return;
    if (activePanel === "__runtime_status__") return;
    if (activePanel === "__context_usage__") return;
    if (activePanel === "__prompt_usage__" && hasPromptWidget) return;
    if (activePanel === "__company_workspace__" && companyPanel) return;
    if (activePanel === "__coding_widget__" && codingPanel) return;
    if (activePanel === "__workspace_tabs__" && workspaceTabs.length > 0) return;
    if (activePanel.startsWith(PLACEMENT_PANEL_PREFIX) && placementManifestMap.has(activePanel.slice(PLACEMENT_PANEL_PREFIX.length))) {
      return;
    }
    if (!items.some((item) => item.id === activePanel)) {
      setActivePanel(null);
    }
  }, [activePanel, codingPanel, companyPanel, hasPromptWidget, items, placementManifestMap, workspaceTabs.length]);

  useEffect(() => {
    if (!activePanel || categoryFilter === "all") return;
    const active = items.find((item) => item.id === activePanel);
    if (active && active.category !== categoryFilter && !pinnedItemIdSet.has(active.id)) {
      setActivePanel(null);
    }
  }, [activePanel, categoryFilter, items, pinnedItemIdSet]);

  useEffect(() => {
    if (!openToolGroupMenu) return;

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (toolGroupMenuRef.current?.contains(target)) return;
      if (toolGroupFloatingMenuRef.current?.contains(target)) return;
      setOpenToolGroupMenu(null);
      setContextMenu(null);
    };

    const handleDocumentKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpenToolGroupMenu(null);
        setContextMenu(null);
      }
    };

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleDocumentKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleDocumentKeyDown);
    };
  }, [openToolGroupMenu]);

  useEffect(() => {
    if (!placementMenuOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && placementMenuRef.current?.contains(target)) return;
      setPlacementMenuOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPlacementMenuOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [placementMenuOpen]);

  useEffect(() => {
    if (!contextMenu) return;

    const close = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && contextMenuRef.current?.contains(target)) return;
      setContextMenu(null);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setContextMenu(null);
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [contextMenu]);

  useEffect(() => {
    if (!toolManagerSearchQuery.trim()) {
      setIsToolManagerSearchOpen(false);
    }
  }, [toolManagerSearchQuery]);

  useEffect(() => {
    if (activePanel !== "__tool_manager__") {
      setIsToolManagerSearchOpen(false);
    }
  }, [activePanel]);

  useEffect(() => {
    if (!isToolManagerSearchOpen) return;

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (toolManagerSearchRef.current?.contains(target)) return;
      setIsToolManagerSearchOpen(false);
    };

    const handleDocumentKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setIsToolManagerSearchOpen(false);
      }
    };

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleDocumentKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleDocumentKeyDown);
    };
  }, [isToolManagerSearchOpen]);

  const tagMap = useMemo(() => {
    const next = new Map<string, string[]>();
    for (const item of items) {
      next.set(item.id, [...new Set([...baseTagsForItem(item), ...(customTagMap[item.id] ?? [])])]);
    }
    return next;
  }, [customTagMap, items]);
  const searchFilteredItems = useMemo(
    () => items
      .filter((item) => item.category !== "tool" || !hiddenToolIdSet.has(item.id))
      .filter((item) => sidebarItemMatchesSearch(item, tagMap.get(item.id) ?? [], searchQuery)),
    [hiddenToolIdSet, items, searchQuery, tagMap],
  );
  useEffect(() => {
    if (!activePanel || activePanel === "__tool_manager__" || activePanel === "__tool_filter_log__" || activePanel === "__runtime_status__" || activePanel === "__context_usage__" || activePanel === "__prompt_usage__" || activePanel === "__company_workspace__" || activePanel === "__coding_widget__" || activePanel === "__workspace_tabs__" || !searchQuery.trim()) return;
    if (!searchFilteredItems.some((item) => item.id === activePanel)) {
      setActivePanel(null);
    }
  }, [activePanel, searchFilteredItems, searchQuery]);
  const counts = useMemo(() => {
    const next: Record<string, number> = { all: searchFilteredItems.length };
    for (const item of searchFilteredItems) {
      next[item.category] = (next[item.category] ?? 0) + 1;
    }
    return next;
  }, [searchFilteredItems]);
  const allToolItems = useMemo(() => sortedToolUiItems(searchFilteredItems.filter((item) => item.category === "tool")), [searchFilteredItems]);
  const toolManagerBaseItems = useMemo(
    () => toolManagerBaseItemsForNameSearch(items, searchFilteredItems, searchQuery),
    [items, searchFilteredItems, searchQuery],
  );
  const toolManagerSearchItems = useMemo(
    () => toolManagerBaseItems.filter((item) => sidebarItemMatchesSearch(item, tagMap.get(item.id) ?? [], toolManagerSearchQuery)),
    [tagMap, toolManagerBaseItems, toolManagerSearchQuery],
  );
  const toolManagerItems = useMemo(() => sortedToolUiItems(toolManagerSearchItems.filter((item) => (
    (!showStarredOnly || starredItemIdSet.has(item.id))
    && (!activeTagFilter || tagMap.get(item.id)?.includes(activeTagFilter))
  ))), [activeTagFilter, showStarredOnly, starredItemIdSet, tagMap, toolManagerSearchItems]);
  const showToolManagerEmptyState = shouldShowToolManagerEmptyState({
    toolCount: toolManagerItems.length,
    sidebarSearchQuery: searchQuery,
    toolManagerSearchQuery,
    activeTagFilter,
    showStarredOnly,
  });
  const serviceCards = useMemo(() => toolServiceCards(toolManagerItems).slice(0, 10), [toolManagerItems]);
  const conversationServiceTargets = useMemo(() => new Set(
    targetList(conversationToolPreferences.include)
      .filter((target) => target.kind === "service")
      .map((target) => target.id),
  ), [conversationToolPreferences.include]);
  const toolManagerCandidates = useMemo(() => toolManagerSearchItems.slice(0, 8), [toolManagerSearchItems]);
  const allToolTags = useMemo(() => {
    const counts = new Map<string, number>();
    for (const item of toolManagerSearchItems) {
      for (const tag of tagMap.get(item.id) ?? []) {
        counts.set(tag, (counts.get(tag) ?? 0) + 1);
      }
    }
    return [...counts.entries()].sort((left, right) => right[1] - left[1] || compareText(left[0], right[0]));
  }, [tagMap, toolManagerSearchItems]);
  const toolItems = useMemo(() => sortedToolUiItems(allToolItems.filter((item) => (
    (!showStarredOnly || starredItemIdSet.has(item.id))
    && (!activeTagFilter || tagMap.get(item.id)?.includes(activeTagFilter))
  ))), [activeTagFilter, allToolItems, showStarredOnly, starredItemIdSet, tagMap]);
  const groupedToolIds = useMemo(() => new Set(toolItems.map((item) => item.id)), [toolItems]);
  const toolGroups = useMemo(() => {
    const groups = new Map<string, SidebarItem[]>();
    for (const item of toolItems) {
      const gid = toolGroupFor(item).id;
      const list = groups.get(gid) ?? [];
      list.push(item);
      groups.set(gid, list);
    }
    return sortedToolGroups([...groups.entries()].map(([id, groupItems]) => {
      const meta = toolGroupFor(groupItems[0]);
      return { id, label: meta.label, icon: meta.icon, path: meta.path, items: groupItems, count: groupItems.length };
    }));
  }, [toolItems]);

  const showToolGroups = categoryFilter === "tool" && toolGroups.length > 0;

  const visibleItems = useMemo(() => {
    const base = (categoryFilter === "all" ? searchFilteredItems : searchFilteredItems.filter((item) => item.category === categoryFilter))
      .filter((item) => !showStarredOnly || starredItemIdSet.has(item.id))
      .filter((item) => item.category !== "tool" || !activeTagFilter || tagMap.get(item.id)?.includes(activeTagFilter));
    return [...base]
      .sort(compareSidebarItems)
      .filter((item) => item.category !== "tool" || !showToolGroups || !groupedToolIds.has(item.id) || pinnedItemIdSet.has(item.id));
  }, [activeTagFilter, searchFilteredItems, categoryFilter, groupedToolIds, pinnedItemIdSet, showStarredOnly, showToolGroups, starredItemIdSet, tagMap]);
  const pinnedRailItems = useMemo(() => (
    pinnedItemIds
      .map((itemId) => searchFilteredItems.find((item) => item.id === itemId))
      .filter((item): item is SidebarItem => Boolean(item))
  ), [searchFilteredItems, pinnedItemIds]);
  const unpinnedVisibleItems = useMemo(
    () => visibleItems.filter((item) => (
      !pinnedItemIdSet.has(item.id)
      && (item.category !== "tool" || categoryFilter === "tool")
    )),
    [categoryFilter, pinnedItemIdSet, visibleItems],
  );

  const activeItem = items.find((item) => item.id === activePanel) ?? null;
  const activePlacementManifest = activePanel?.startsWith(PLACEMENT_PANEL_PREFIX)
    ? placementManifestMap.get(activePanel.slice(PLACEMENT_PANEL_PREFIX.length)) ?? null
    : null;
  const isToolManagerActive = activePanel === "__tool_manager__";
  const isToolFilterLogActive = activePanel === "__tool_filter_log__";
  const isRuntimeStatusActive = activePanel === "__runtime_status__";
  const isContextUsageActive = activePanel === "__context_usage__";
  const isPromptUsageActive = activePanel === "__prompt_usage__" && hasPromptWidget;
  const isCompanyPanelActive = activePanel === "__company_workspace__" && Boolean(companyPanel);
  const isCodingPanelActive = activePanel === "__coding_widget__" && Boolean(codingPanel);
  const isWorkspaceTabsActive = activePanel === "__workspace_tabs__" && workspaceTabs.length > 0;
  const isPlacementPanelActive = Boolean(activePlacementManifest);
  const activeToolGroupId = activeItem?.category === "tool" ? toolGroupFor(activeItem).id : null;
  const shouldCompactToolRail = categoryFilter === "tool" && !searchQuery.trim() && !activeTagFilter && !showStarredOnly;
  const railToolGroups = useMemo(() => {
    if (!showToolGroups) return [];
    if (!shouldCompactToolRail || toolGroups.length <= DEFAULT_TOOL_GROUP_RAIL_LIMIT) return toolGroups;
    const visible = toolGroups.slice(0, DEFAULT_TOOL_GROUP_RAIL_LIMIT);
    if (!activeToolGroupId || visible.some((group) => group.id === activeToolGroupId)) return visible;
    const activeGroup = toolGroups.find((group) => group.id === activeToolGroupId);
    if (!activeGroup) return visible;
    return [...visible.slice(0, DEFAULT_TOOL_GROUP_RAIL_LIMIT - 1), activeGroup];
  }, [activeToolGroupId, shouldCompactToolRail, showToolGroups, toolGroups]);
  const hiddenToolGroupCount = showToolGroups ? Math.max(0, toolGroups.length - new Set(railToolGroups.map((group) => group.id)).size) : 0;
  const pinnedRightSidebarPlacements = useMemo(
    () => pinnedPlacements
      .filter((placement) => placement.surface === "right_sidebar")
      .map((placement) => placementManifestMap.get(placement.id))
      .filter((manifest): manifest is NonNullable<typeof manifest> => Boolean(manifest)),
    [pinnedPlacements, placementManifestMap],
  );

  const updatePinnedPlacements = (updater: (current: ReturnType<typeof normalizePinnedPlacements>) => ReturnType<typeof normalizePinnedPlacements>) => {
    onSettingChange("sidebar", "ui_placements", updater(pinnedPlacements));
  };

  const triggerPlacement = (placementId: string) => {
    const manifest = placementManifestMap.get(placementId);
    const action = manifest?.renderer.action;
    if (!manifest) return;
    if (!action) {
      setActivePanel(`${PLACEMENT_PANEL_PREFIX}${placementId}`);
      setPlacementMenuOpen(false);
      return;
    }
    if (action.type === "open_panel" && action.target) {
      const target = action.target;
      setActivePanel((current) => current === target ? null : target);
      setPlacementMenuOpen(false);
      return;
    }
    if (action.type === "open_settings_section" && action.target) {
      setPlacementMenuOpen(false);
      onOpenSettingsSection?.(action.target);
      return;
    }
    if (action.type === "toggle_yolo") {
      setPlacementMenuOpen(false);
      onToggleYolo?.();
      return;
    }
    setActivePanel(`${PLACEMENT_PANEL_PREFIX}${placementId}`);
    setPlacementMenuOpen(false);
  };

  const renderPlacementPanel = (placementId: string) => {
    const manifest = placementManifestMap.get(placementId);
    if (!manifest) return null;
    const action = manifest.renderer.action;
    const settingsTarget = action?.type === "open_settings_section" ? action.target ?? "" : "";
    return (
      <section className="space-y-3">
        <div className="rounded-lg border border-zinc-800 bg-zinc-950/45 p-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h4 className="text-sm font-medium text-zinc-100">{manifest.label}</h4>
              {manifest.description && (
                <p className="mt-1 text-xs leading-5 text-zinc-400">{manifest.description}</p>
              )}
            </div>
            <button
              type="button"
              onClick={() => updatePinnedPlacements((current) => togglePinnedPlacement(current, { id: placementId, surface: "right_sidebar" }))}
              className="rounded-md border border-zinc-800 px-2 py-1 text-[11px] text-zinc-400 hover:border-zinc-600 hover:text-zinc-200"
            >
              Unpin
            </button>
          </div>
          {manifest.renderer.kind === "html" && (
            <div className="mt-3 h-48 overflow-hidden">
              <PlacementHtmlRenderer manifest={manifest} />
            </div>
          )}
          {manifest.renderer.kind !== "html" && action?.type === "open_settings_section" && settingsTarget && (
            <button
              type="button"
              onClick={() => onOpenSettingsSection?.(settingsTarget)}
              className="mt-3 rounded-lg border border-zinc-700 bg-zinc-950/35 px-3 py-2 text-xs text-zinc-200 hover:border-zinc-500"
            >
              設定セクションを開く
            </button>
          )}
          {manifest.renderer.kind !== "html" && action?.type === "toggle_yolo" && (
            <button
              type="button"
              onClick={onToggleYolo}
              className="mt-3 rounded-lg border border-zinc-700 bg-zinc-950/35 px-3 py-2 text-xs text-zinc-200 hover:border-zinc-500"
            >
              YOLO を切り替える
            </button>
          )}
          {manifest.renderer.kind !== "html" && !action && (
            <p className="mt-3 text-xs text-zinc-500">
              この配置は現在の surface で追加表示できます。
            </p>
          )}
        </div>
      </section>
    );
  };

  const handleDragStart = (event: DragEvent, item: SidebarItem) => {
    const kind = supportedComposerDropKind(item);
    if (!kind) return;
    const type = kind === "tool_toggle" ? "tool" : kind;
    const display = resolveCatalogDisplay(item, "composer");
    event.dataTransfer.setData(
      "application/rumi-widget",
      JSON.stringify({
        id: item.id,
        type,
        label: item.ui?.composer_label ?? item.label,
        description: item.ui?.composer_description ?? item.description,
        ...display,
        widgetKind: kind,
        action: item.ui?.composer_action,
        sourceItemId: item.id,
        enabled: selectedToolIdSet.has(item.id),
      }),
    );
    event.dataTransfer.effectAllowed = "copy";
  };

  const handleShortcutDragStart = (event: DragEvent, item: SidebarItem) => {
    handleDragStart(event, item);
    event.dataTransfer.setData("application/rumi-sidebar-shortcut", item.id);
  };

  const handleShortcutDrop = (event: DragEvent) => {
    const itemId = event.dataTransfer.getData("application/rumi-sidebar-shortcut");
    if (!itemId || pinnedItemIdSet.has(itemId) || !items.some((item) => item.id === itemId)) return;
    event.preventDefault();
    updatePinnedItemIds((current) => [...current, itemId]);
    setOpenToolGroupMenu(null);
  };

  const updateSidebarStringArray = (
    fieldId: "pinned_item_ids" | "starred_item_ids",
    current: string[],
    updater: (current: string[]) => string[],
    legacyKey: string,
  ) => {
    const next = [...new Set(updater(current).filter(Boolean))];
    try {
      localStorage.setItem(legacyKey, JSON.stringify(next));
    } catch {
      // Keep local fallback best-effort only; user_data is the source of truth.
    }
    onSettingChange("sidebar", fieldId, next);
  };

  const updatePinnedItemIds = (updater: (current: string[]) => string[]) => {
    updateSidebarStringArray("pinned_item_ids", pinnedItemIds, updater, "rumi-sidebar-pinned-item-ids");
  };

  const updateStarredItemIds = (updater: (current: string[]) => string[]) => {
    updateSidebarStringArray("starred_item_ids", starredItemIds, updater, "rumi-sidebar-starred-item-ids");
  };

  const updateCustomTagMap = (updater: (current: Record<string, string[]>) => Record<string, string[]>) => {
    const next = Object.fromEntries(Object.entries(updater(customTagMap)).map(([id, values]) => [
      id,
      [...new Set(values.map((item) => normalizeTag(String(item))).filter(Boolean))],
    ]).filter(([, values]) => values.length > 0));
    try {
      localStorage.setItem("rumi-tool-custom-tags", JSON.stringify(next));
    } catch {
      // Keep local fallback best-effort only; user_data is the source of truth.
    }
    onSettingChange("sidebar", "custom_tool_tags", next);
  };

  const togglePin = (itemId: string) => {
    updatePinnedItemIds((current) => current.includes(itemId) ? current.filter((id) => id !== itemId) : [...current, itemId]);
  };

  const toggleStar = (itemId: string) => {
    updateStarredItemIds((current) => current.includes(itemId) ? current.filter((id) => id !== itemId) : [...current, itemId]);
  };

  const setToolsEnabled = (toolIds: string[], enabled: boolean) => {
    const uniqueToolIds = [...new Set(toolIds.filter((toolId) => items.some((item) => item.id === toolId && item.category === "tool")))];
    if (uniqueToolIds.length === 0) return;
    onToolBatchSet?.(uniqueToolIds, enabled);
  };

  const setServiceEnabled = (service: ToolServiceCard, enabled: boolean) => {
    if (toolSelectionScope !== "conversation" || !activeConversationId) {
      setToolsEnabled(service.items.map((item) => item.id), enabled);
      return;
    }
    const include = targetList(conversationToolPreferences.include)
      .filter((target) => !(target.kind === "service" && target.id === service.id));
    const nextPreferences = {
      ...conversationToolPreferences,
      mode: enabled ? "manual" : (conversationToolPreferences.mode ?? "auto"),
      include: enabled ? [...include, { kind: "service", id: service.id }] : include,
    };
    setConversationToolPreferences(nextPreferences);
    void toolResources.updateConversationToolPreferences(activeConversationId, nextPreferences).catch(() => {
      setConversationToolPreferences(conversationToolPreferences);
    });
  };

  const toolsWithTag = (tag: string) => allToolItems.filter((item) => tagMap.get(item.id)?.includes(tag)).map((item) => item.id);

  const addCustomTag = (itemId: string, rawTag: string) => {
    const tag = normalizeTag(rawTag);
    if (!tag) return;
    updateCustomTagMap((current) => ({
      ...current,
      [itemId]: [...new Set([...(current[itemId] ?? []), tag])],
    }));
    setTagDraftByItemId((current) => ({ ...current, [itemId]: "" }));
  };

  const removeCustomTag = (itemId: string, tag: string) => {
    updateCustomTagMap((current) => ({
      ...current,
      [itemId]: (current[itemId] ?? []).filter((candidate) => candidate !== tag),
    }));
    if (activeTagFilter === tag && toolsWithTag(tag).length <= 1) {
      setActiveTagFilter(null);
    }
  };

  const openItemContextMenu = (event: MouseEvent, item: SidebarItem) => {
    event.preventDefault();
    event.stopPropagation();
    setContextMenu({
      itemId: item.id,
      x: Math.max(8, Math.min(event.clientX, window.innerWidth - 190)),
      y: Math.max(8, Math.min(event.clientY, window.innerHeight - 230)),
    });
  };

  const openToolGroup = (groupId: string, button: HTMLButtonElement) => {
    const rect = button.getBoundingClientRect();
    setToolGroupMenuPosition(getRailFloatingMenuPosition(rect, { width: 224, height: 360 }));
    setOpenToolGroupMenu(groupId);
  };

  const renderRailItemButton = (item: SidebarItem, pinnedZone = false) => (
    <button
      key={item.id}
      draggable={supportsComposerDrop(item)}
      onDragStart={supportsComposerDrop(item) ? (e) => handleDragStart(e, item) : undefined}
      onContextMenu={(event) => openItemContextMenu(event, item)}
      onClick={() => setActivePanel((current) => (current === item.id ? null : item.id))}
      tabIndex={buttonTabIndex}
      className={cn(
        RAIL_BUTTON_CLASS,
        "duration-150 ease-out group/btn",
        activePanel === item.id
          ? "bg-zinc-800 text-zinc-100 ring-1 ring-zinc-600/70"
          : item.category === "tool" && !selectedToolIdSet.has(item.id)
            ? "text-zinc-700 hover:text-zinc-500 hover:bg-zinc-800/30"
            : pinnedZone
              ? "text-sky-300 hover:text-sky-100 hover:bg-sky-500/10"
              : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50",
      )}
      title={item.label}
    >
      {railIcon(iconForItem(item), pinnedZone ? 21 : 20)}

      {activePanel === item.id && (
        <div className={cn("absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-4 rounded-r-full", categoryColor(item.category, "indicator"))} />
      )}

      {item.category === "tool" && !selectedToolIdSet.has(item.id) && (
        <span className="absolute bottom-1 right-1 h-1.5 w-1.5 rounded-full bg-zinc-700 ring-1 ring-[#09090b]" />
      )}

      {item.badge && (
        <span className="absolute -top-0.5 -right-0.5 text-[6px] bg-emerald-500 text-black px-0.5 rounded-full font-bold leading-tight">
          {item.badge}
        </span>
      )}

      {(starredItemIdSet.has(item.id) || pinnedItemIdSet.has(item.id)) && !item.badge && (
        <span className="absolute -top-0.5 -right-0.5 flex items-center gap-px rounded-full bg-zinc-900 px-0.5 text-[7px] leading-tight ring-1 ring-zinc-700">
          {starredItemIdSet.has(item.id) && <Star size={8} className="fill-current text-amber-300" />}
          {pinnedItemIdSet.has(item.id) && <Pin size={8} className="text-sky-300" />}
        </span>
      )}

      {activePanel !== item.id && (
        <div
          className={cn(
            "absolute bottom-0.5 right-0.5 w-1 h-1 rounded-full opacity-0 group-hover/btn:opacity-100 transition-opacity",
            categoryColor(item.category, "dot"),
          )}
        />
      )}

      <span className="absolute right-full mr-2 px-2 py-1 bg-zinc-800 text-zinc-200 text-[10px] rounded-md opacity-0 group-hover/btn:opacity-100 pointer-events-none transition-opacity whitespace-nowrap border border-zinc-700 shadow-lg rumi-layer-modal">
        {item.label}
        <span className={cn("ml-1 text-[8px] px-1 py-px rounded", categoryColor(item.category, "badge"))}>
          {item.category}
        </span>
      </span>
    </button>
  );

  const placementIcon = (placementId: string) => {
    switch (placementId) {
      case "tool-filter-log":
        return <ListTodo size={18} />;
      case "runtime-status":
        return <ShieldCheck size={18} />;
      case "yolo-switch":
        return <Power size={18} />;
      case "model-manager":
      case "model-pack-switcher":
        return <Cpu size={18} />;
      case "webhook-endpoints":
        return <Globe size={18} />;
      default:
        return <Settings size={18} />;
    }
  };

  const renderPinnedPlacementButton = (placementId: string) => {
    const manifest = placementManifestMap.get(placementId);
    if (!manifest) return null;
    const isActive = (
      (manifest.renderer.action?.type === "open_panel" && activePanel === manifest.renderer.action.target)
      || activePanel === `${PLACEMENT_PANEL_PREFIX}${placementId}`
    );
    const toggled = placementId === "yolo-switch" && yoloMode;
    return (
      <button
        key={placementId}
        type="button"
        tabIndex={buttonTabIndex}
        onClick={() => triggerPlacement(placementId)}
        onContextMenu={(event) => {
          event.preventDefault();
          updatePinnedPlacements((current) => togglePinnedPlacement(current, { id: placementId, surface: "right_sidebar" }));
        }}
        className={cn(
          RAIL_BUTTON_CLASS,
          isActive || toggled
            ? "bg-zinc-800 text-zinc-100 ring-1 ring-zinc-600/70"
            : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50",
        )}
        title={manifest.label}
      >
        {railIcon(placementIcon(placementId), 20)}
      </button>
    );
  };

  return (
    <aside aria-label="Tools and utility panels" className="rumi-right-sidebar relative hidden h-full flex-shrink-0 border-l border-zinc-800/60 bg-[#09090b] transition-[width,opacity] duration-200 ease-out md:flex">
      {(activeItem || isPlacementPanelActive || isToolManagerActive || isToolFilterLogActive || isRuntimeStatusActive || isContextUsageActive || isPromptUsageActive || isCompanyPanelActive || isCodingPanelActive || isWorkspaceTabsActive) && (
        <div
          className="rumi-right-sidebar-panel rumi-layer-local-popover relative flex min-w-0 flex-col border-r border-zinc-800/40 bg-[#0a0a0c] shadow-2xl animate-in slide-in-from-right-2 duration-200"
          style={{ width: panelWidthPx }}
        >
          <div
            role="separator"
            tabIndex={0}
            aria-label="機能パネル幅を変更"
            aria-orientation="vertical"
            aria-valuemin={220}
            aria-valuemax={520}
            aria-valuenow={panelWidthPx}
            title="ドラッグまたは左右キーで機能パネル幅を変更"
            className="absolute left-0 top-0 rumi-layer-local-popover h-full w-2 cursor-col-resize bg-transparent transition-colors hover:bg-zinc-700/60 focus-visible:bg-indigo-400/50"
            onPointerDown={startPanelResize}
            onKeyDown={resizePanelWithKeyboard}
          />
          <div className="flex h-11 flex-shrink-0 items-center justify-between gap-2 border-b border-zinc-800/60 px-2.5">
            <div className="flex min-w-0 flex-1 items-center gap-2 overflow-hidden">
              <div className={cn("w-1.5 h-1.5 rounded-full flex-shrink-0", activeItem ? categoryColor(activeItem.category, "bg") : isPlacementPanelActive ? "bg-violet-300" : isCompanyPanelActive ? "bg-sky-400" : isCodingPanelActive ? "bg-zinc-300" : isWorkspaceTabsActive ? "bg-emerald-300" : isPromptUsageActive ? "bg-cyan-300" : isToolFilterLogActive ? "bg-amber-300" : isRuntimeStatusActive ? "bg-sky-300" : "bg-emerald-500")} />
              <h3 className="text-[13px] font-medium text-zinc-100 truncate">{activeItem?.label ?? activePlacementManifest?.label ?? (isCompanyPanelActive ? "Employees" : isCodingPanelActive ? "Coding widget" : isWorkspaceTabsActive ? "Workspace tabs" : isPromptUsageActive ? "Current prompts" : isToolFilterLogActive ? "選定ログ" : isContextUsageActive ? "Context usage" : isRuntimeStatusActive ? "Runtime status" : "機能")}</h3>
              {activeItem?.badge && (
                <span className="text-[8px] bg-emerald-500/20 text-emerald-400 px-1 py-0.5 rounded-full font-bold flex-shrink-0">
                  {activeItem.badge}
                </span>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-1">
              {activeItem?.category === "tool" && (
                <button
                  type="button"
                  onClick={() => onToolToggle?.(activeItem)}
                  className={cn(
                    "flex h-8 w-8 items-center justify-center rounded-md transition-colors",
                    selectedToolIdSet.has(activeItem.id) ? "text-emerald-400 hover:bg-emerald-500/10" : "text-zinc-600 hover:bg-zinc-800",
                  )}
                  title={selectedToolIdSet.has(activeItem.id) ? "今回の指定を解除" : "今回使う"}
                  aria-label={selectedToolIdSet.has(activeItem.id) ? "今回の指定を解除" : "今回使う"}
                >
                  <Power size={14} />
                </button>
              )}
              <button
                type="button"
                onClick={() => setActivePanel(null)}
                className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-100"
                title="Close panel"
                aria-label="Close panel"
              >
                <X size={15} />
              </button>
            </div>
          </div>

          {activeItem?.description && (
            <div className="px-2.5 py-1.5 border-b border-zinc-800/40">
              <p className="text-[10px] text-zinc-500">{activeItem.description}</p>
            </div>
          )}

          {activeItem?.category === "tool" && (
            <div className="border-b border-zinc-800/40 px-2.5 py-2">
              {activeItem.tool_info?.requires_approval && (
                <div
                  data-testid="tool-detail-needs-approval"
                  className="mb-2 rounded-md border border-sky-500/30 bg-sky-500/10 px-2 py-1.5 text-sky-100"
                >
                  <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-[10px] leading-4">
                    <ShieldAlert size={12} className="flex-shrink-0 text-sky-200" />
                    <span className="font-semibold">Needs approval</span>
                    {activeItem.tool_info.approval_policy && (
                      <span className="min-w-0 break-words text-sky-200/80">
                        Approval policy: {activeItem.tool_info.approval_policy}
                      </span>
                    )}
                  </div>
                </div>
              )}
              <div className="mb-2 flex flex-wrap gap-1">
                {(tagMap.get(activeItem.id) ?? []).map((tag) => {
                  const custom = (customTagMap[activeItem.id] ?? []).includes(tag);
                  return (
                    <button
                      key={tag}
                      type="button"
                      onClick={() => setActiveTagFilter((current) => current === tag ? null : tag)}
                      className={cn(
                        "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px]",
                        activeTagFilter === tag ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200" : "border-zinc-800 bg-zinc-950/60 text-zinc-500 hover:text-zinc-300",
                      )}
                      title={custom ? "クリックで絞り込み、xで削除" : "クリックで絞り込み"}
                    >
                      <Tag size={10} />
                      <span>{tag}</span>
                      {custom && (
                        <span
                          role="button"
                          tabIndex={0}
                          onClick={(event) => {
                            event.stopPropagation();
                            removeCustomTag(activeItem.id, tag);
                          }}
                          onKeyDown={(event) => {
                            if (event.key === "Enter" || event.key === " ") {
                              event.preventDefault();
                              event.stopPropagation();
                              removeCustomTag(activeItem.id, tag);
                            }
                          }}
                          className="ml-0.5 text-zinc-600 hover:text-zinc-200"
                        >
                          x
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
              <div className="flex items-center gap-1.5">
                <input
                  type="text"
                  value={tagDraftByItemId[activeItem.id] ?? ""}
                  onChange={(event) => setTagDraftByItemId((current) => ({ ...current, [activeItem.id]: event.target.value }))}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      addCustomTag(activeItem.id, tagDraftByItemId[activeItem.id] ?? "");
                    }
                  }}
                  placeholder="tagを追加"
                  className="min-w-0 flex-1 rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1 text-[11px] text-zinc-200 outline-none placeholder:text-zinc-700 focus:border-zinc-600"
                />
                <button
                  type="button"
                  onClick={() => addCustomTag(activeItem.id, tagDraftByItemId[activeItem.id] ?? "")}
                  className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 text-[11px] text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100"
                >
                  Add
                </button>
              </div>
            </div>
          )}

          <div className={cn("flex-1 overflow-y-auto", isCompanyPanelActive ? "p-0" : "p-2.5")}>
            {isCompanyPanelActive ? (
              companyPanel
            ) : isCodingPanelActive ? (
              codingPanel
            ) : isWorkspaceTabsActive ? (
              <WorkspaceTabRailPanel
                tabs={workspaceTabs}
                activeTabId={activeWorkspaceTabId ?? ""}
                onSelect={(tabId) => onWorkspaceTabSelect?.(tabId)}
                onClose={(tabId) => onWorkspaceTabClose?.(tabId)}
                onCreate={(kind) => onWorkspaceTabCreate?.(kind)}
              />
            ) : isPlacementPanelActive && activePlacementManifest ? (
              renderPlacementPanel(activePlacementManifest.id)
            ) : isPromptUsageActive && onLoadPromptActive && onTogglePromptEdge ? (
              <PromptSidebarWidget
                profileId={promptProfileId}
                conversationId={conversationId}
                modelProfileId={selectedProfile?.profile_id ?? selectedProfile?.qualified_model_id ?? selectedProfile?.model_id ?? undefined}
                modelLabel={selectedProfile?.display_name ?? selectedProfile?.model_id ?? undefined}
                initialUsage={promptUsage}
                loadPromptActive={onLoadPromptActive}
                togglePromptEdge={onTogglePromptEdge}
                showChatPromptUsage={showChatPromptUsage}
                onToggleChatPromptUsage={onToggleChatPromptUsage}
              />
            ) : isToolFilterLogActive ? (
              <ToolFilterLogWidget entries={toolFilterEntries} />
            ) : isContextUsageActive ? (
              <div className="space-y-3" data-testid="context-usage-panel">
                <div className="rounded-lg border border-zinc-800 bg-zinc-950/45 px-3 py-2.5">
                  <p className="text-[11px] uppercase tracking-wide text-zinc-600">Used tokens</p>
                  <p className="mt-1 text-sm text-zinc-100">{contextUsage?.usedTokens ?? 0}</p>
                </div>
                <div className="rounded-lg border border-zinc-800 bg-zinc-950/45 px-3 py-2.5">
                  <p className="text-[11px] uppercase tracking-wide text-zinc-600">Context limit</p>
                  <p className="mt-1 text-sm text-zinc-100">
                    {contextUsage?.maxContext === -1 ? "Unlimited" : contextUsage?.maxContext || "Unknown"}
                  </p>
                </div>
                <div className="rounded-lg border border-zinc-800 bg-zinc-950/45 px-3 py-2.5">
                  <p className="text-[11px] uppercase tracking-wide text-zinc-600">Utilization</p>
                  <p className="mt-1 text-sm text-zinc-100">{contextUsage?.label ?? "?"}</p>
                </div>
              </div>
            ) : isRuntimeStatusActive ? (
              <div className="space-y-3">
                <div className="rounded-lg border border-zinc-800 bg-zinc-950/45 px-3 py-2.5">
                  <p className="text-[11px] uppercase tracking-wide text-zinc-600">Model</p>
                  <p className="mt-1 text-sm text-zinc-100">{selectedProfile?.display_name ?? "Unknown"}</p>
                </div>
                <div className="rounded-lg border border-zinc-800 bg-zinc-950/45 px-3 py-2.5">
                  <p className="text-[11px] uppercase tracking-wide text-zinc-600">Capabilities</p>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {(runtimeCapabilitySnapshot?.model_capabilities ?? []).map((capability) => (
                      <span key={capability} className="rounded-full border border-zinc-700 px-2 py-0.5 text-[10px] text-zinc-300">
                        {capability}
                      </span>
                    ))}
                  </div>
                </div>
              </div>
            ) : activeItem ? (
              <SidebarPanel
                item={activeItem}
                settingsValues={settingsValues}
                activeConversationId={activeConversationId}
                onSettingChange={onSettingChange}
                onPanelAction={onPanelAction}
              />
                        ) : (
                          <div className="space-y-3">
                            <div className="px-1">
                              <p className="text-[10px] font-medium uppercase tracking-wider text-zinc-600">今回のおすすめ</p>
                              <p className="mt-0.5 text-[10px] leading-4 text-zinc-500">自動選定、承認待ち、利用不可の状態をまとめます。</p>
                            </div>
                            {!showToolManagerEmptyState && (
                              <ToolManagerWidget
                                tools={toolManagerBaseItems}
                                disabledToolIds={disabledToolIds}
                                hiddenToolIds={hiddenToolIds}
                                filterEntries={toolFilterEntries}
                              />
                            )}
                            <div ref={toolManagerSearchRef} className="relative">
                              <label className="relative block">
                                <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-600" />
                                <input
                                  type="text"
                                  value={toolManagerSearchQuery}
                                  onChange={(event) => {
                                    const nextQuery = event.target.value;
                                    setToolManagerSearchQuery(nextQuery);
                                    setIsToolManagerSearchOpen(Boolean(nextQuery.trim()));
                                  }}
                                  onFocus={() => {
                                    if (toolManagerSearchQuery.trim()) {
                                      setIsToolManagerSearchOpen(true);
                                    }
                                  }}
                                  placeholder="機能を検索"
                                  aria-expanded={Boolean(toolManagerSearchQuery.trim() && isToolManagerSearchOpen)}
                                  className="h-9 w-full rounded-lg border border-zinc-800 bg-zinc-950 pl-8 pr-8 text-[12px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
                                />
                                {toolManagerSearchQuery.trim() && (
                                  <button
                                    type="button"
                                    onClick={() => {
                                      setToolManagerSearchQuery("");
                                      setIsToolManagerSearchOpen(false);
                                    }}
                                    className="absolute right-1.5 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"
                                    title="検索をクリア"
                                  >
                                    <X size={13} />
                                  </button>
                                )}
                              </label>
                              {toolManagerSearchQuery.trim() && isToolManagerSearchOpen && (
                                <div data-testid="tool-manager-candidates" className="absolute left-0 right-0 top-[calc(100%+6px)] rumi-layer-global-overlay overflow-hidden rounded-xl border border-zinc-700/70 bg-zinc-950 py-1 shadow-2xl">
                                  <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2 text-[10px] text-zinc-600">
                                    <span>候補</span>
                                    <span>{toolManagerSearchItems.length} / {toolManagerBaseItems.length}</span>
                                  </div>
                                  {toolManagerCandidates.length > 0 ? (
                                    <div className="max-h-64 overflow-y-auto py-1">
                                      {toolManagerCandidates.map((item) => (
                                        <button
                                          key={item.id}
                                          type="button"
                                          onClick={() => {
                                            setActivePanel(item.id);
                                            setToolManagerSearchQuery("");
                                            setIsToolManagerSearchOpen(false);
                                          }}
                                          className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-zinc-300 transition-colors hover:bg-zinc-800/80 hover:text-zinc-100"
                                        >
                                          <span className="flex min-w-0 items-center gap-2">
                                            <span className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-md bg-zinc-900 text-zinc-500">
                                              {iconForItem(item)}
                                            </span>
                                            <span className="min-w-0">
                                              <span className="block truncate text-[12px]">{item.label}</span>
                                              {item.description && <span className="block truncate text-[10px] text-zinc-500">{item.description}</span>}
                                            </span>
                                          </span>
                                          <span className={cn("h-1.5 w-1.5 flex-shrink-0 rounded-full", selectedToolIdSet.has(item.id) ? "bg-emerald-400" : "bg-zinc-700")} />
                                        </button>
                                      ))}
                                    </div>
                                  ) : (
                                    <p className="px-3 py-3 text-[11px] text-zinc-500">一致する機能がありません。</p>
                                  )}
                                </div>
                              )}
                            </div>
                            {showToolManagerEmptyState ? (
                              <div data-testid="tool-manager-empty-state" className="rounded-lg border border-zinc-800/70 bg-zinc-950/45 px-3 py-6 text-center">
                                <div className="mx-auto flex h-9 w-9 items-center justify-center rounded-lg bg-zinc-900 text-zinc-500">
                                  <Search size={16} />
                                </div>
                                <p className="mt-3 text-[12px] font-medium text-zinc-200">一致する機能がありません。</p>
                                <p className="mt-1 text-[10px] leading-4 text-zinc-500">
                                  名前検索やフィルタを変更すると候補が表示されます。
                                </p>
                              </div>
                            ) : (
                              <>
                            {(pinnedRailItems.length > 0 || pinnedRightSidebarPlacements.length > 0) && (
                              <div className="rounded-lg border border-zinc-800/70 bg-zinc-950/45 p-2">
                                <div className="mb-2 flex items-center justify-between gap-2">
                                  <p className="text-[10px] font-medium uppercase tracking-wider text-zinc-600">ピン留め</p>
                                  <span className="text-[10px] text-zinc-600">{pinnedRailItems.length + pinnedRightSidebarPlacements.length}</span>
                                </div>
                                <div className="grid grid-cols-2 gap-1.5">
                                  {pinnedRailItems.map((item) => (
                                    <button
                                      key={item.id}
                                      type="button"
                                      onClick={() => setActivePanel(item.id)}
                                      className="flex min-w-0 items-center gap-2 rounded-md border border-zinc-800 bg-zinc-950/55 px-2 py-1.5 text-left text-zinc-300 transition-colors hover:bg-zinc-900 hover:text-zinc-100"
                                    >
                                      <span className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-md bg-zinc-900 text-zinc-500">
                                        {iconForItem(item)}
                                      </span>
                                      <span className="min-w-0">
                                        <span className="block truncate text-[11px] font-medium">{item.label}</span>
                                        <span className="block truncate text-[9px] text-zinc-500">機能</span>
                                      </span>
                                    </button>
                                  ))}
                                  {pinnedRightSidebarPlacements.map((placement) => (
                                    <button
                                      key={placement.id}
                                      type="button"
                                      onClick={() => triggerPlacement(placement.id)}
                                      className="flex min-w-0 items-center gap-2 rounded-md border border-zinc-800 bg-zinc-950/55 px-2 py-1.5 text-left text-zinc-300 transition-colors hover:bg-zinc-900 hover:text-zinc-100"
                                    >
                                      <span className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-md bg-zinc-900 text-zinc-500">
                                        {railIcon(placementIcon(placement.id), 15)}
                                      </span>
                                      <span className="min-w-0">
                                        <span className="block truncate text-[11px] font-medium">{placement.label}</span>
                                        <span className="block truncate text-[9px] text-zinc-500">{placement.description ?? "ウィジェット"}</span>
                                      </span>
                                    </button>
                                  ))}
                                </div>
                              </div>
                            )}
                            <div className="rounded-lg border border-zinc-800/70 bg-zinc-950/45 p-2">
                              <div className="mb-2 flex items-center justify-between gap-2">
                                <p className="text-[10px] font-medium uppercase tracking-wider text-zinc-600">接続</p>
                                <div className="inline-grid grid-cols-2 rounded-md border border-zinc-800 bg-zinc-950 p-0.5">
                                  {(["turn", "conversation"] as const).map((scope) => (
                                    <button
                                      key={scope}
                                      type="button"
                                      disabled={scope === "conversation" && !activeConversationId}
                                      onClick={() => setToolSelectionScope(scope)}
                                      className={cn(
                                        "rounded px-2 py-1 text-[10px] transition-colors disabled:cursor-not-allowed disabled:opacity-40",
                                        toolSelectionScope === scope ? "bg-zinc-700 text-zinc-100" : "text-zinc-500 hover:text-zinc-200",
                                      )}
                                    >
                                      {scope === "turn" ? "今回" : "この会話で使う"}
                                    </button>
                                  ))}
                                </div>
                              </div>
                              <div className="grid gap-1.5">
                                {serviceCards.map((service) => {
                                  const serviceToolIds = service.items.map((item) => item.id);
                                  const turnSelected = serviceToolIds.some((id) => selectedToolIdSet.has(id));
                                  const conversationSelected = conversationServiceTargets.has(service.id);
                                  const selected = toolSelectionScope === "conversation" ? conversationSelected : turnSelected;
                                  const pinnedCount = service.items.filter((item) => pinnedItemIdSet.has(item.id) || starredItemIdSet.has(item.id)).length;
                                  return (
                                    <div key={service.id} className="rounded-md border border-zinc-800 bg-zinc-950/55 p-2">
                                      <div className="flex items-start gap-2">
                                        <div className="mt-0.5 flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-md bg-zinc-900 text-zinc-500">
                                          <Wrench size={14} />
                                        </div>
                                        <div className="min-w-0 flex-1">
                                          <div className="flex items-center justify-between gap-2">
                                            <p className="truncate text-[12px] font-medium text-zinc-200">{service.label}</p>
                                            <span className={cn("h-1.5 w-1.5 flex-shrink-0 rounded-full", selected ? "bg-emerald-400" : "bg-zinc-700")} />
                                          </div>
                                          <p className="mt-0.5 truncate text-[10px] text-zinc-500">{service.description}</p>
                                          <div className="mt-1 flex flex-wrap gap-1 text-[9px] text-zinc-500">
                                            <span className="rounded bg-zinc-900 px-1.5 py-0.5">{service.items.length} 機能</span>
                                            {pinnedCount > 0 && <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-amber-200">{pinnedCount} ピン留め</span>}
                                            {conversationSelected && <span className="rounded bg-sky-500/10 px-1.5 py-0.5 text-sky-200">会話固定</span>}
                                          </div>
                                        </div>
                                        <button
                                          type="button"
                                          onClick={() => setServiceEnabled(service, !selected)}
                                          className={cn("flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-md transition-colors", selected ? "text-emerald-300 hover:bg-emerald-500/10" : "text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300")}
                                          title={selected ? "サービス指定を解除" : "サービスを使う"}
                                        >
                                          <Power size={15} />
                                        </button>
                                      </div>
                                    </div>
                                  );
                                })}
                              </div>
                            </div>
                            <div className="grid grid-cols-3 gap-2">
                              <div className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-2">
                                <p className="text-[9px] uppercase tracking-wider text-zinc-600">機能</p>
                                <p className="mt-1 text-lg font-semibold text-zinc-100">{toolManagerItems.length}</p>
                              </div>
                  <div className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-2">
                    <p className="text-[9px] uppercase tracking-wider text-zinc-600">今回</p>
                    <p className="mt-1 text-lg font-semibold text-emerald-300">{selectedToolIds.length}</p>
                  </div>
                  <div className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-2">
                    <p className="text-[9px] uppercase tracking-wider text-zinc-600">ピン留め</p>
                    <p className="mt-1 text-lg font-semibold text-amber-300">{starredItemIds.length}</p>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-1.5">
                              <button
                                type="button"
                                onClick={() => setToolsEnabled(toolManagerItems.map((item) => item.id), true)}
                    className="flex items-center justify-center gap-1 rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-2 py-1.5 text-[11px] font-medium text-emerald-200 hover:bg-emerald-500/15"
                  >
                    <FolderCheck size={13} />
                    表示中を今回使う
                  </button>
                              <button
                                type="button"
                                onClick={() => setToolsEnabled(toolManagerItems.map((item) => item.id), false)}
                    className="flex items-center justify-center gap-1 rounded-lg border border-zinc-800 bg-zinc-950/60 px-2 py-1.5 text-[11px] font-medium text-zinc-300 hover:bg-zinc-900"
                  >
                    <FolderX size={13} />
                    今回指定を解除
                  </button>
                </div>
                {allToolTags.length > 0 && (
                  <div className="rounded-lg border border-zinc-800/70 bg-zinc-950/45 p-2">
                    <div className="mb-2 flex items-center justify-between gap-2">
                      <p className="text-[10px] font-medium uppercase tracking-wider text-zinc-600">タグ</p>
                      {activeTagFilter && (
                        <button
                          type="button"
                          onClick={() => setActiveTagFilter(null)}
                          className="text-[10px] text-zinc-500 hover:text-zinc-200"
                        >
                          解除
                        </button>
                      )}
                    </div>
                    <div className="flex max-h-24 flex-wrap gap-1 overflow-y-auto">
                      {allToolTags.map(([tag, count]) => (
                        <button
                          key={tag}
                          type="button"
                          onClick={() => setActiveTagFilter((current) => current === tag ? null : tag)}
                          className={cn(
                            "inline-flex items-center gap-1 rounded-md border px-1.5 py-1 text-[10px] transition-colors",
                            activeTagFilter === tag
                              ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
                              : tag === "danger"
                                ? "border-red-500/20 bg-red-500/10 text-red-200 hover:bg-red-500/15"
                                : "border-zinc-800 bg-zinc-950/60 text-zinc-500 hover:text-zinc-300",
                          )}
                        >
                          {tag === "danger" ? <ShieldAlert size={10} /> : <Tag size={10} />}
                          <span>{tag}</span>
                          <span className="text-zinc-600">{count}</span>
                        </button>
                      ))}
                    </div>
                    {activeTagFilter && (
                      <div className="mt-2 grid grid-cols-2 gap-1.5">
                        <button
                          type="button"
                          onClick={() => setToolsEnabled(toolsWithTag(activeTagFilter), true)}
                          className="rounded-md bg-emerald-500/10 px-2 py-1 text-[10px] font-medium text-emerald-200 hover:bg-emerald-500/15"
                        >
                          タグを今回使う
                        </button>
                        <button
                          type="button"
                          onClick={() => setToolsEnabled(toolsWithTag(activeTagFilter), false)}
                          className={cn(
                            "rounded-md px-2 py-1 text-[10px] font-medium",
                            activeTagFilter === "danger" ? "bg-red-500/10 text-red-200 hover:bg-red-500/15" : "bg-zinc-900 text-zinc-300 hover:bg-zinc-800",
                          )}
                        >
                          タグ指定を解除
                        </button>
                      </div>
                    )}
                  </div>
                )}
                <div className="rounded-lg border border-zinc-800/70 bg-zinc-950/45 p-2">
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <p className="text-[10px] font-medium uppercase tracking-wider text-zinc-600">最近使った</p>
                    <button
                      type="button"
                      onClick={() => setActivePanel("__tool_filter_log__")}
                      className="text-[10px] text-zinc-500 hover:text-zinc-200"
                    >
                      開く
                    </button>
                  </div>
                  <ToolFilterLogWidget entries={toolFilterEntries.slice(0, 4)} />
                </div>
                <div className="space-y-1">
                  {toolManagerItems.map((item) => {
                    const enabled = selectedToolIdSet.has(item.id);
                    const pinned = pinnedItemIdSet.has(item.id);
                    const starred = starredItemIdSet.has(item.id);
                    const globallyDisabled = disabledToolIdSet.has(item.id);
                    const hidden = hiddenToolIdSet.has(item.id);
                    const blockedEntry = toolFilterEntries.find((entry) => entry.tool_name === item.id && (entry.status === "blocked" || entry.status === "rejected"));
                    const itemTags = tagMap.get(item.id) ?? [];
                    return (
                      <div key={item.id} className="rounded-lg border border-zinc-800/70 bg-zinc-950/45 p-2">
                        <div className="flex items-start gap-2">
                          <button
                            type="button"
                            onClick={() => setActivePanel(item.id)}
                            onContextMenu={(event) => openItemContextMenu(event, item)}
                            className="mt-0.5 flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-md bg-zinc-900 text-zinc-500 hover:text-zinc-200"
                            title={item.label}
                          >
                            {iconForItem(item)}
                          </button>
                          <button
                            type="button"
                            onClick={() => setActivePanel(item.id)}
                            onContextMenu={(event) => openItemContextMenu(event, item)}
                            className="min-w-0 flex-1 text-left"
                          >
                            <span className="block truncate text-[12px] font-medium text-zinc-200">{item.label}</span>
                            {item.description && <span className="block truncate text-[10px] text-zinc-500">{item.description}</span>}
                          </button>
                          <div className="flex flex-shrink-0 items-center gap-0.5">
                                        <button
                                          type="button"
                                          onClick={() => toggleStar(item.id)}
                                          className={cn("flex h-7 w-7 items-center justify-center rounded-md transition-colors", starred ? "text-amber-300 hover:bg-amber-500/10" : "text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300")}
                                          title={starred ? "スター解除" : "スター"}
                                        >
                                          <Star size={16} strokeWidth={2.1} className={cn("h-4 w-4 flex-shrink-0", starred && "fill-current")} />
                                        </button>
                                        <button
                                          type="button"
                                          onClick={() => togglePin(item.id)}
                                          className={cn("flex h-7 w-7 items-center justify-center rounded-md transition-colors", pinned ? "text-sky-300 hover:bg-sky-500/10" : "text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300")}
                                          title={pinned ? "ピン留め解除" : "ピン留め"}
                                        >
                                          {pinned ? <PinOff size={15} /> : <Pin size={15} />}
                                        </button>
                                        <button
                                          type="button"
                                          onClick={() => onToolToggle?.(item)}
                                          className={cn("flex h-7 w-7 items-center justify-center rounded-md transition-colors", enabled ? "text-emerald-300 hover:bg-emerald-500/10" : "text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300")}
                                          title={enabled ? "今回の指定を解除" : "今回使う"}
                                        >
                                          <Power size={15} />
                                        </button>
                          </div>
                        </div>
                        {itemTags.length > 0 && (
                          <div className="mt-2 flex flex-wrap gap-1 pl-9">
                            {itemTags.slice(0, 5).map((tag) => (
                              <button
                                key={tag}
                                type="button"
                                onClick={() => setActiveTagFilter((current) => current === tag ? null : tag)}
                                className={cn(
                                  "rounded px-1.5 py-0.5 text-[9px]",
                                  activeTagFilter === tag ? "bg-emerald-500/15 text-emerald-200" : tag === "danger" ? "bg-red-500/10 text-red-200" : "bg-zinc-900 text-zinc-500 hover:text-zinc-300",
                                )}
                              >
                                {tag}
                              </button>
                            ))}
                          </div>
                        )}
                        <div className="mt-2 flex flex-wrap gap-1 pl-9">
                          {globallyDisabled && (
                            <span className="rounded px-1.5 py-0.5 text-[9px] bg-zinc-900 text-zinc-400">権限ブロック</span>
                          )}
                          {hidden && (
                            <span className="rounded px-1.5 py-0.5 text-[9px] bg-zinc-900 text-zinc-400">一覧から隠す</span>
                          )}
                          {item.tool_info?.requires_approval && (
                            <span className="rounded px-1.5 py-0.5 text-[9px] bg-sky-500/10 text-sky-200">承認が必要</span>
                          )}
                          {blockedEntry && (
                            <span className="rounded px-1.5 py-0.5 text-[9px] bg-amber-500/10 text-amber-200">
                              {toolFilterBlockedSummary(blockedEntry)}
                            </span>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
                              </>
                            )}
              </div>
            )}
          </div>
        </div>
      )}

              <div className="rumi-right-sidebar-rail flex w-13 flex-shrink-0 flex-col overflow-visible">
                <div
                  className="rumi-right-sidebar-rail-scroll flex w-full flex-1 flex-col items-center gap-1 overflow-x-visible overflow-y-auto py-2 scrollbar-none"
                  onDragOver={(event) => {
                    if (event.dataTransfer.types.includes("application/rumi-sidebar-shortcut")) {
                      event.preventDefault();
                      event.dataTransfer.dropEffect = "copy";
                    }
                  }}
                  onDrop={handleShortcutDrop}
                >
          {(pinnedRailItems.length > 0 || pinnedRightSidebarPlacements.length > 0) && (
            <div className="flex w-full flex-col items-center gap-1">
              {pinnedRailItems.map((item) => renderRailItemButton(item, true))}
              {pinnedRightSidebarPlacements.map((placement) => renderPinnedPlacementButton(placement.id))}
              <div className="w-5 h-px bg-sky-500/20 my-0.5" />
            </div>
          )}
                  <CategorySwitcher active={categoryFilter} counts={counts} keyboardButtonNavigation={keyboardButtonNavigation} onChange={(id) => { setCategoryFilter(id); setOpenToolGroupMenu(null); }} />
                  {workspaceTabs.length > 0 && (
                    <button
                      type="button"
                      tabIndex={buttonTabIndex}
                      onClick={() => setActivePanel((current) => (current === "__workspace_tabs__" ? null : "__workspace_tabs__"))}
                      className={cn(
                        RAIL_BUTTON_CLASS,
                        isWorkspaceTabsActive
                          ? "bg-emerald-500/15 text-emerald-200 ring-1 ring-emerald-500/30"
                          : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50",
                      )}
                      title="Workspace tabs"
                      aria-label="Workspace tabs"
                      aria-pressed={isWorkspaceTabsActive}
                    >
                      <LayoutGrid size={17} className="h-[17px] w-[17px] shrink-0" />
                      <span className="absolute -top-0.5 -right-0.5 rounded-full bg-emerald-500 px-0.5 text-[7px] font-bold leading-tight text-black">
                        {workspaceTabs.length}
                      </span>
                    </button>
                  )}
                  {hasPromptWidget && (
                    <button
                      type="button"
                      tabIndex={buttonTabIndex}
                      onClick={() => setActivePanel((current) => (current === "__prompt_usage__" ? null : "__prompt_usage__"))}
                      className={cn(
                        RAIL_BUTTON_CLASS,
                        isPromptUsageActive
                          ? "bg-cyan-500/15 text-cyan-200 ring-1 ring-cyan-500/30"
                          : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50",
                      )}
                      title="Current prompts"
                      aria-label="Current prompts"
                      aria-pressed={isPromptUsageActive}
                    >
                      <FileText size={17} className="h-[17px] w-[17px] shrink-0" />
                      {promptRailCount > 0 && (
                        <span className="absolute -top-0.5 -right-0.5 rounded-full bg-cyan-400 px-0.5 text-[7px] font-bold leading-tight text-black">
                          {Math.min(promptRailCount, 99)}
                        </span>
                      )}
                    </button>
                  )}
                  <SidebarSearchControl
                    query={searchQuery}
                    resultCount={searchFilteredItems.length}
                    totalCount={items.length}
                    keyboardButtonNavigation={keyboardButtonNavigation}
                    onQueryChange={(value) => {
                      setSearchQuery(value);
                      setOpenToolGroupMenu(null);
                    }}
                  />
                  <button
                    type="button"
                    tabIndex={buttonTabIndex}
                    onClick={() => setShowStarredOnly((value) => !value)}
                    aria-pressed={showStarredOnly}
                    className={cn(
                      RAIL_BUTTON_CLASS,
                      showStarredOnly
                        ? "bg-amber-500/15 text-amber-300 ring-1 ring-amber-500/30"
                        : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50",
                    )}
                    title="ピン留めした機能"
                    aria-label={
                      starredItemIds.length > 0
                        ? `Starred tools (${starredItemIds.length})`
                        : "Starred tools"
                    }
                  >
            <Star
              size={18}
              strokeWidth={2.15}
              className={cn("h-[18px] w-[18px] flex-shrink-0", starredItemIds.length > 0 && "fill-current")}
            />
            {starredItemIds.length > 0 && (
              <span className="absolute -top-0.5 -right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-emerald-500 px-1 text-[7px] font-bold leading-none text-black">
                {starredItemIds.length}
              </span>
            )}
          </button>
          <button
            type="button"
            tabIndex={buttonTabIndex}
            onClick={() => setActivePanel((current) => (current === "__tool_manager__" ? null : "__tool_manager__"))}
                    className={cn(
                      RAIL_BUTTON_CLASS,
                      activePanel === "__tool_manager__"
                        ? "bg-zinc-800 text-zinc-100 ring-1 ring-zinc-600/70"
                        : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50",
                    )}
                    title="機能"
          >
            <SlidersHorizontal size={16} className="h-4 w-4 shrink-0" />
            {selectedToolIds.length > 0 && (
              <span className="absolute -top-0.5 -right-0.5 rounded-full bg-emerald-500 px-0.5 text-[7px] font-bold leading-tight text-black">
                {selectedToolIds.length}
              </span>
            )}
          </button>
          <div className="relative">
            <button
              type="button"
              tabIndex={buttonTabIndex}
              onPointerDown={(event) => event.stopPropagation()}
              onClick={(event) => {
                event.stopPropagation();
                const nextOpen = !placementMenuOpen;
                if (nextOpen) {
                  setPlacementMenuPosition(getRailFloatingMenuPosition(event.currentTarget.getBoundingClientRect(), { width: 224, height: 320 }));
                }
                setPlacementMenuOpen(nextOpen);
              }}
              className={cn(
                RAIL_BUTTON_CLASS,
                placementMenuOpen
                  ? "bg-zinc-800 text-zinc-100 ring-1 ring-zinc-600/70"
                  : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50",
              )}
              title="ウィジェットをピン留め"
            >
              <Plus size={16} className="h-4 w-4 shrink-0" />
            </button>
            {placementMenuOpen && (
              <LayerPortal layer="modal">
                <div
                  ref={placementMenuRef}
                  className="fixed rumi-layer-modal w-56 overflow-hidden rounded-xl border border-zinc-700/70 bg-zinc-950 py-1 shadow-2xl"
                  style={placementMenuPosition ? { top: `${placementMenuPosition.top}px`, right: `${placementMenuPosition.right}px` } : undefined}
                  onPointerDown={(event) => event.stopPropagation()}
                >
                  <div className="border-b border-zinc-800 px-3 py-2">
                    <p className="text-[11px] font-semibold text-zinc-200">サイドバーにピン留め</p>
                    <p className="text-[10px] text-zinc-500">縦表示 / 設定可</p>
                  </div>
                  <div className="max-h-64 overflow-y-auto py-1">
                    {rightSidebarPlacementCandidates.map((manifest) => (
                      <button
                        key={manifest.id}
                        type="button"
                        onClick={() => {
                          updatePinnedPlacements((current) => togglePinnedPlacement(current, { id: manifest.id, surface: "right_sidebar" }));
                          setPlacementMenuOpen(false);
                        }}
                        className="flex w-full items-center gap-2 px-3 py-2 text-left text-zinc-300 transition-colors hover:bg-zinc-800/80 hover:text-zinc-100"
                      >
                        <span className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-md bg-zinc-900 text-zinc-400">
                          {placementIcon(manifest.id)}
                        </span>
                        <span className="min-w-0">
                          <span className="block truncate text-[12px]">{manifest.label}</span>
                          {manifest.description && <span className="block truncate text-[10px] text-zinc-500">{manifest.description}</span>}
                        </span>
                      </button>
                    ))}
                    {rightSidebarPlacementCandidates.length === 0 && (
                      <p className="px-3 py-3 text-[11px] text-zinc-500">追加できる候補はありません。</p>
                    )}
                  </div>
                </div>
              </LayerPortal>
            )}
          </div>
          {companyPanel && (
            <button
              type="button"
              tabIndex={buttonTabIndex}
              onClick={() => setActivePanel((current) => (current === "__company_workspace__" ? null : "__company_workspace__"))}
              className={cn(
                RAIL_BUTTON_CLASS,
                activePanel === "__company_workspace__"
                  ? "bg-sky-500/15 text-sky-200 ring-1 ring-sky-500/30"
                  : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50",
              )}
              title="Employees"
            >
              <Building2 size={17} className="h-[17px] w-[17px] shrink-0" />
            </button>
          )}
          {codingPanel && (
            <button
              type="button"
              tabIndex={buttonTabIndex}
              onClick={() => setActivePanel((current) => (current === "__coding_widget__" ? null : "__coding_widget__"))}
              className={cn(
                RAIL_BUTTON_CLASS,
                activePanel === "__coding_widget__"
                  ? "bg-zinc-800 text-zinc-100 ring-1 ring-zinc-600/70"
                  : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50",
              )}
              title="Coding widget"
            >
              <Code2 size={17} className="h-[17px] w-[17px] shrink-0" />
            </button>
          )}
          <div className="w-5 h-px bg-zinc-800 my-1" />

          {showToolGroups && (
            <div ref={toolGroupMenuRef} className="flex flex-col items-center gap-px w-full">
              {railToolGroups.map((group) => {
                const isGroupActive = activeToolGroupId === group.id;
                const isGroupOpen = openToolGroupMenu === group.id;
                        return (
                          <div key={group.id} className="relative">
                            <button
                              type="button"
                              tabIndex={buttonTabIndex}
                              onClick={(event) => {
                                if (openToolGroupMenu === group.id) {
                                  setOpenToolGroupMenu(null);
                                  return;
                                }
                                openToolGroup(group.id, event.currentTarget);
                              }}
                              aria-label={`${group.path?.length ? group.path.join(" / ") : group.label || TOOL_GROUP_LABELS[group.id] || group.id} tool folder`}
                              aria-expanded={isGroupOpen}
                              className={cn(
                                RAIL_BUTTON_CLASS,
                                "group/group",
                                isGroupOpen || isGroupActive
                                  ? "bg-emerald-900/40 text-zinc-400 border border-emerald-500/30"
                                  : "text-zinc-400 hover:bg-zinc-800/50",
                              )}
                    title={`${group.path?.length ? group.path.join(" / ") : group.label || TOOL_GROUP_LABELS[group.id] || group.id} (${group.count})`}
                  >
                    <StableToolGroupRailGlyph iconName={group.icon} groupId={group.id} />
                    <span className="absolute -top-0.5 -right-0.5 text-[7px] bg-zinc-700 text-zinc-300 px-0.5 rounded-full leading-tight">
                      {group.count}
                    </span>
                    <span className="absolute right-full mr-2 px-2 py-1 bg-zinc-800 text-zinc-200 text-[10px] rounded-md opacity-0 group-hover/group:opacity-100 pointer-events-none transition-opacity whitespace-nowrap border border-zinc-700 shadow-lg rumi-layer-global-overlay">
                      {group.path?.length && group.path.length > 1 ? group.path.join(" / ") : group.label || TOOL_GROUP_LABELS[group.id] || group.id}
                    </span>
                    {(isGroupActive || isGroupOpen) && (
                      <div className="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-r-full bg-emerald-500" />
                    )}
                  </button>
                  {openToolGroupMenu === group.id && (
                    <LayerPortal layer="modal">
                      <div
                        ref={toolGroupFloatingMenuRef}
                        className="fixed rumi-layer-modal w-56 overflow-hidden rounded-xl border border-zinc-700/70 bg-zinc-950 py-1 text-left shadow-2xl"
                        style={toolGroupMenuPosition ? { top: `${toolGroupMenuPosition.top}px`, right: `${toolGroupMenuPosition.right}px` } : undefined}
                      >
                        <div className="border-b border-zinc-800 px-3 py-2">
                          <p className="truncate text-[11px] font-semibold text-zinc-200">{group.label || TOOL_GROUP_LABELS[group.id] || group.id}</p>
                          {group.path?.length && group.path.length > 1 && (
                            <p className="truncate text-[10px] text-zinc-500">{group.path.join(" / ")}</p>
                          )}
                          <p className="text-[10px] text-zinc-500">{group.count} 機能</p>
                        </div>
                              <div className="grid grid-cols-2 gap-1 border-b border-zinc-800 p-2">
                                <button
                                  type="button"
                                  tabIndex={buttonTabIndex}
                                  onClick={() => setToolsEnabled(group.items.map((item) => item.id), true)}
                                  className="flex items-center justify-center gap-1 rounded-md bg-emerald-500/10 px-2 py-1 text-[10px] font-medium text-emerald-200 hover:bg-emerald-500/15"
                                >
                          <FolderCheck size={11} />
                          今回使う
                        </button>
                                <button
                                  type="button"
                                  tabIndex={buttonTabIndex}
                                  onClick={() => setToolsEnabled(group.items.map((item) => item.id), false)}
                                  className="flex items-center justify-center gap-1 rounded-md bg-zinc-900 px-2 py-1 text-[10px] font-medium text-zinc-300 hover:bg-zinc-800"
                                >
                          <FolderX size={11} />
                          解除
                        </button>
                      </div>
                      <div className="max-h-64 overflow-y-auto py-1">
                                {group.items.map((item) => (
                                  <button
                                    key={item.id}
                                    type="button"
                                    tabIndex={buttonTabIndex}
                                    draggable={supportsComposerDrop(item)}
                            onDragStart={supportsComposerDrop(item) ? (event) => handleShortcutDragStart(event, item) : undefined}
                            onContextMenu={(event) => openItemContextMenu(event, item)}
                            onClick={(event) => {
                              event.stopPropagation();
                              setActivePanel((current) => (current === item.id ? null : item.id));
                              setOpenToolGroupMenu(null);
                            }}
                            className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-zinc-300 transition-colors hover:bg-zinc-800/80 hover:text-zinc-100"
                          >
                            <span className="flex min-w-0 items-center gap-2">
                              <span className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-md bg-zinc-900 text-zinc-500">
                                {iconForItem(item)}
                              </span>
                              <span className="min-w-0">
                                <span className="block truncate text-[12px]">{item.label}</span>
                                {item.description && <span className="block truncate text-[10px] text-zinc-500">{item.description}</span>}
                              </span>
                            </span>
                            {item.category === "tool" && (
                              <span className="flex flex-shrink-0 items-center gap-1">
                                {starredItemIdSet.has(item.id) && <Star size={10} className="fill-current text-amber-300" />}
                                {pinnedItemIdSet.has(item.id) && <Pin size={10} className="text-sky-300" />}
                                <span className={cn("h-1.5 w-1.5 rounded-full", selectedToolIdSet.has(item.id) ? "bg-emerald-400" : "bg-zinc-700")} />
                              </span>
                            )}
                          </button>
                        ))}
                      </div>
                    </div>
                    </LayerPortal>
                  )}
                </div>
              );
              })}
              {hiddenToolGroupCount > 0 && (
                <button
                  type="button"
                  tabIndex={buttonTabIndex}
                  onClick={() => {
                    setActivePanel((current) => (current === "__tool_manager__" ? null : "__tool_manager__"));
                    setOpenToolGroupMenu(null);
                  }}
                  className={cn(
                    RAIL_BUTTON_CLASS,
                    "group/btn",
                    isToolManagerActive
                      ? "bg-zinc-800 text-zinc-100 ring-1 ring-zinc-600/70"
                      : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50",
                  )}
                  title={`その他の機能 (${hiddenToolGroupCount} groups)`}
                >
                  <MoreVertical size={17} className="h-[17px] w-[17px] shrink-0" />
                  <span className="absolute -top-0.5 -right-0.5 text-[7px] bg-zinc-700 text-zinc-300 px-0.5 rounded-full leading-tight">
                    {hiddenToolGroupCount}
                  </span>
                  <span className="absolute right-full mr-2 px-2 py-1 bg-zinc-800 text-zinc-200 text-[10px] rounded-md opacity-0 group-hover/btn:opacity-100 pointer-events-none transition-opacity whitespace-nowrap border border-zinc-700 shadow-lg rumi-layer-global-overlay">
                    その他の機能
                  </span>
                </button>
              )}
            </div>
          )}

          {showToolGroups && unpinnedVisibleItems.length > 0 && (
            <div className="w-5 h-px bg-zinc-800 my-1" />
          )}

          {unpinnedVisibleItems.map((item) => renderRailItemButton(item))}

          {contextMenu && (() => {
            const item = items.find((candidate) => candidate.id === contextMenu.itemId);
            if (!item) return null;
            const pinned = pinnedItemIdSet.has(item.id);
            const starred = starredItemIdSet.has(item.id);
            const enabled = selectedToolIdSet.has(item.id);
            return (
              <LayerPortal layer="commandPalette">
                <div
                  ref={contextMenuRef}
                  className="fixed rumi-layer-command-palette w-44 overflow-hidden rounded-xl border border-zinc-700/70 bg-zinc-950 py-1 text-left shadow-2xl"
                  style={{ left: `${contextMenu.x}px`, top: `${contextMenu.y}px` }}
                  onPointerDown={(event) => event.stopPropagation()}
                >
                  <div className="border-b border-zinc-800 px-3 py-2">
                    <p className="truncate text-[11px] font-semibold text-zinc-200">{item.label}</p>
                    <p className="truncate text-[10px] text-zinc-500">{item.category}</p>
                  </div>
                        <button
                          type="button"
                          tabIndex={buttonTabIndex}
                          onClick={() => {
                            togglePin(item.id);
                            setContextMenu(null);
                          }}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] text-zinc-300 hover:bg-zinc-800/80 hover:text-zinc-100"
                >
                  {pinned ? <PinOff size={13} /> : <Pin size={13} />}
                  <span>{pinned ? "ピン留め解除" : "ピン留め"}</span>
                </button>
                        <button
                          type="button"
                          tabIndex={buttonTabIndex}
                          onClick={() => {
                            toggleStar(item.id);
                            setContextMenu(null);
                          }}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] text-zinc-300 hover:bg-zinc-800/80 hover:text-zinc-100"
                >
                  <Star size={13} className={cn(starred && "fill-current text-amber-300")} />
                  <span>{starred ? "スター解除" : "スター"}</span>
                </button>
                {item.category === "tool" && (
                          <button
                            type="button"
                            tabIndex={buttonTabIndex}
                            onClick={() => {
                              onToolToggle?.(item);
                              setContextMenu(null);
                            }}
                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] text-zinc-300 hover:bg-zinc-800/80 hover:text-zinc-100"
                  >
                    <Power size={13} className={enabled ? "text-emerald-300" : undefined} />
                    <span>{enabled ? "今回の指定を解除" : "今回使う"}</span>
                  </button>
                )}
                        <button
                          type="button"
                          tabIndex={buttonTabIndex}
                          onClick={() => {
                            setActivePanel(item.id);
                            setContextMenu(null);
                          }}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] text-zinc-300 hover:bg-zinc-800/80 hover:text-zinc-100"
                >
                  <MoreVertical size={13} />
                  <span>詳細を開く</span>
                </button>
              </div>
              </LayerPortal>
            );
          })()}

          <div className="mt-auto w-5 h-px bg-zinc-800 my-1" />

                  <button
                    type="button"
                    tabIndex={buttonTabIndex}
                    onClick={onOpenSettings}
                    className={cn(RAIL_BUTTON_CLASS, "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50 group/btn")}
                    title="Settings"
                  >
            <Settings size={16} className="h-4 w-4 shrink-0" />
            <span className="absolute right-full mr-2 px-2 py-1 bg-zinc-800 text-zinc-200 text-[10px] rounded-md opacity-0 group-hover/btn:opacity-100 pointer-events-none transition-opacity whitespace-nowrap border border-zinc-700 shadow-lg rumi-layer-modal">
              Settings
            </span>
          </button>
        </div>
      </div>
    </aside>
  );
}
