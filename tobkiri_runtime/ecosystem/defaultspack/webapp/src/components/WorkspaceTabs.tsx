import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  AppWindow,
  CalendarDays,
  Code2,
  GalleryVerticalEnd,
  Globe,
  KanbanSquare,
  LayoutGrid,
  MessageSquareText,
  Monitor,
  Plus,
  UsersRound,
  X,
  type LucideIcon,
} from "lucide-react";

import type { KanbanBoardScope, SidebarItem } from "../lib/api";
import { cn } from "../lib/cn";

export type WorkspaceTabKind =
  | "chat"
  | "coding"
  | "calendar"
  | "kanban"
  | "desktops"
  | "subagents"
  | "canvas"
  | "tools"
  | "browser";

export type WorkspaceTab = {
  id: string;
  kind: WorkspaceTabKind;
  title: string;
  conversationId?: string | null;
  kanbanScope?: KanbanBoardScope | null;
  kanbanScopeLabel?: string | null;
  createdAt: number;
};

export type WorkspaceTabCreateOption = {
  kind: WorkspaceTabKind;
  label: string;
  description: string;
  icon: LucideIcon;
  disabled?: boolean;
  badge?: string;
};

export const DEFAULT_WORKSPACE_TAB_ID = "workspace-tab-chat-home";

export const WORKSPACE_TAB_CREATE_OPTIONS: WorkspaceTabCreateOption[] = [
  {
    kind: "chat",
    label: "AI Chat",
    description: "Conversation workspace",
    icon: MessageSquareText,
  },
  {
    kind: "coding",
    label: "Coding",
    description: "Workspace cockpit",
    icon: Code2,
  },
  {
    kind: "calendar",
    label: "Calendar",
    description: "Schedule board",
    icon: CalendarDays,
  },
  {
    kind: "kanban",
    label: "Kanban",
    description: "Task and agent board",
    icon: KanbanSquare,
  },
  {
    kind: "desktops",
    label: "Desktops",
    description: "Managed seats",
    icon: Monitor,
  },
  {
    kind: "subagents",
    label: "Subagents / Teams",
    description: "Channels, DMs, approvals",
    icon: UsersRound,
  },
  {
    kind: "canvas",
    label: "Canvas",
    description: "Tool preview space",
    icon: GalleryVerticalEnd,
  },
  {
    kind: "tools",
    label: "Tools",
    description: "Launch tools and widgets",
    icon: LayoutGrid,
  },
  {
    kind: "browser",
    label: "Browser",
    description: "Coming soon",
    icon: Globe,
    disabled: true,
    badge: "soon",
  },
];

let workspaceTabCounter = 0;

export function workspaceTabOption(kind: WorkspaceTabKind): WorkspaceTabCreateOption {
  return WORKSPACE_TAB_CREATE_OPTIONS.find((option) => option.kind === kind) ?? WORKSPACE_TAB_CREATE_OPTIONS[0];
}

export function createWorkspaceTab(
  kind: WorkspaceTabKind,
  overrides: Partial<WorkspaceTab> = {},
  now = Date.now(),
): WorkspaceTab {
  const option = workspaceTabOption(kind);
  workspaceTabCounter += 1;
  const tab: WorkspaceTab = {
    id: overrides.id ?? `workspace-tab-${kind}-${now}-${workspaceTabCounter}`,
    kind,
    title: overrides.title ?? option.label,
    conversationId: overrides.conversationId ?? null,
    createdAt: overrides.createdAt ?? now,
  };
  if ("kanbanScope" in overrides) tab.kanbanScope = overrides.kanbanScope ?? null;
  if ("kanbanScopeLabel" in overrides) tab.kanbanScopeLabel = overrides.kanbanScopeLabel ?? null;
  return tab;
}

export function workspaceTabDisplayTitle(tab: WorkspaceTab): string {
  const title = tab.title.trim();
  if (title) return title;
  return workspaceTabOption(tab.kind).label;
}

function iconForKind(kind: WorkspaceTabKind): LucideIcon {
  return workspaceTabOption(kind).icon;
}

function NewTabMenu({
  options,
  onCreate,
}: {
  options: WorkspaceTabCreateOption[];
  onCreate: (kind: WorkspaceTabKind) => void;
}) {
  return (
    <div
      id="rumi-new-workspace-tab-menu"
      role="menu"
      aria-label="Create workspace tab"
      className="rumi-workspace-new-tab-menu absolute left-0 top-[calc(100%+6px)] rumi-layer-modal w-[min(420px,calc(100vw-24px))] overflow-hidden rounded-xl border border-zinc-700/70 bg-zinc-950 py-2 shadow-2xl"
    >
      <div className="grid grid-cols-2 gap-1.5 px-2">
        {options.map((option) => {
          const Icon = option.icon;
          return (
            <button
              key={option.kind}
              type="button"
              role="menuitem"
              disabled={option.disabled}
              onClick={() => !option.disabled && onCreate(option.kind)}
              className={cn(
                "flex min-h-16 min-w-0 items-start gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors",
                option.disabled
                  ? "cursor-not-allowed border-zinc-900 bg-zinc-950/40 text-zinc-700"
                  : "border-zinc-800 bg-zinc-950/70 text-zinc-300 hover:border-zinc-700 hover:bg-zinc-900 hover:text-zinc-100",
              )}
            >
              <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-zinc-900 text-zinc-400">
                <Icon size={16} />
              </span>
              <span className="min-w-0">
                <span className="flex min-w-0 items-center gap-1.5">
                  <span className="break-words text-[12px] font-medium leading-4">{option.label}</span>
                  {option.badge && <span className="shrink-0 rounded bg-zinc-800 px-1 py-px text-[8px] text-zinc-500">{option.badge}</span>}
                </span>
                <span className="mt-0.5 block text-[10px] leading-4 text-zinc-500">{option.description}</span>
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function WorkspaceTabBar({
  tabs,
  activeTabId,
  createOptions = WORKSPACE_TAB_CREATE_OPTIONS,
  onSelect,
  onClose,
  onCreate,
}: {
  tabs: WorkspaceTab[];
  activeTabId: string;
  createOptions?: WorkspaceTabCreateOption[];
  onSelect: (tabId: string) => void;
  onClose: (tabId: string) => void;
  onCreate: (kind: WorkspaceTabKind) => void;
}) {
  const [isMenuOpen, setIsMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!isMenuOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node) || menuRef.current?.contains(target)) return;
      setIsMenuOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setIsMenuOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isMenuOpen]);

  const handleCreate = (kind: WorkspaceTabKind) => {
    onCreate(kind);
    setIsMenuOpen(false);
  };

  return (
    <div className="rumi-workspace-tabbar flex h-10 shrink-0 items-end gap-1 border-b border-zinc-800/60 bg-[#09090b] px-2 pt-1">
      <div role="tablist" aria-label="Open workspaces" className="flex min-w-0 flex-1 items-end gap-1 overflow-x-auto overflow-y-hidden pb-0.5 scrollbar-none">
        {tabs.map((tab) => {
          const Icon = iconForKind(tab.kind);
          const isActive = tab.id === activeTabId;
          const title = workspaceTabDisplayTitle(tab);
          return (
            <div
              key={tab.id}
              className={cn(
                "group/tab flex h-9 max-w-52 min-w-24 items-center gap-1.5 rounded-t-lg border px-1.5 text-left text-[12px] transition-colors",
                isActive
                  ? "border-zinc-700 border-b-[#09090b] bg-[#111116] text-zinc-100"
                  : "border-transparent bg-zinc-950/40 text-zinc-500 hover:bg-zinc-900 hover:text-zinc-200",
              )}
              title={title}
            >
              <button
                type="button"
                role="tab"
                aria-selected={isActive}
                aria-current={isActive ? "page" : undefined}
                onClick={() => onSelect(tab.id)}
                className="flex h-full min-w-0 flex-1 items-center gap-1.5 rounded-md px-0.5 text-left"
              >
                <Icon size={13} className="shrink-0" />
                <span className="min-w-0 flex-1 truncate">{title}</span>
              </button>
              {tabs.length > 1 && (
                <button
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation();
                    onClose(tab.id);
                  }}
                  className={cn(
                    "flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-zinc-600 transition-[opacity,color,background-color] hover:bg-zinc-800 hover:text-zinc-200 group-hover/tab:opacity-100 group-focus-within/tab:opacity-100",
                    isActive ? "opacity-60" : "opacity-0",
                  )}
                  title="Close tab"
                  aria-label={`Close ${title}`}
                >
                  <X size={12} />
                </button>
              )}
            </div>
          );
        })}
      </div>
      <div ref={menuRef} className="relative pb-0.5">
        <button
          type="button"
          onClick={() => setIsMenuOpen((value) => !value)}
          className={cn(
            "flex h-9 w-9 items-center justify-center rounded-lg text-zinc-500 transition-colors hover:bg-zinc-900 hover:text-zinc-100",
            isMenuOpen && "bg-zinc-900 text-zinc-100",
          )}
          title="New tab"
          aria-label="New tab"
          aria-haspopup="menu"
          aria-expanded={isMenuOpen}
          aria-controls="rumi-new-workspace-tab-menu"
        >
          <Plus size={16} />
        </button>
        {isMenuOpen && <NewTabMenu options={createOptions} onCreate={handleCreate} />}
      </div>
    </div>
  );
}

export function WorkspaceTabRailPanel({
  tabs,
  activeTabId,
  createOptions = WORKSPACE_TAB_CREATE_OPTIONS,
  onSelect,
  onClose,
  onCreate,
}: {
  tabs: WorkspaceTab[];
  activeTabId: string;
  createOptions?: WorkspaceTabCreateOption[];
  onSelect: (tabId: string) => void;
  onClose: (tabId: string) => void;
  onCreate: (kind: WorkspaceTabKind) => void;
}) {
  return (
    <section className="space-y-3">
      <div className="space-y-1">
        {tabs.map((tab) => {
          const Icon = iconForKind(tab.kind);
          const isActive = tab.id === activeTabId;
          const title = workspaceTabDisplayTitle(tab);
          return (
            <div
              key={tab.id}
              className={cn(
                "group flex w-full min-w-0 items-center gap-2 rounded-lg border px-2 py-2 text-left transition-colors",
                isActive
                  ? "border-zinc-700 bg-zinc-900 text-zinc-100"
                  : "border-zinc-800 bg-zinc-950/45 text-zinc-400 hover:bg-zinc-900/70 hover:text-zinc-100",
              )}
            >
              <button
                type="button"
                aria-current={isActive ? "page" : undefined}
                onClick={() => onSelect(tab.id)}
                className="flex min-h-9 min-w-0 flex-1 items-center gap-2 rounded-md text-left"
              >
                <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-zinc-950 text-zinc-400">
                  <Icon size={15} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[12px] font-medium">{title}</span>
                  <span className="block truncate text-[10px] text-zinc-600">{workspaceTabOption(tab.kind).label}</span>
                </span>
              </button>
              {tabs.length > 1 && (
                <button
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation();
                    onClose(tab.id);
                  }}
                  className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-zinc-600 opacity-0 hover:bg-zinc-800 hover:text-zinc-200 group-hover:opacity-100"
                  title="Close tab"
                  aria-label={`Close ${title}`}
                >
                  <X size={12} />
                </button>
              )}
            </div>
          );
        })}
      </div>
      <div className="grid grid-cols-2 gap-1.5">
        {createOptions.map((option) => {
          const Icon = option.icon;
          return (
            <button
              key={option.kind}
              type="button"
              role="menuitem"
              disabled={option.disabled}
              onClick={() => !option.disabled && onCreate(option.kind)}
              className={cn(
                "flex min-w-0 items-center gap-1.5 rounded-md border px-2 py-1.5 text-left text-[11px] transition-colors",
                option.disabled
                  ? "cursor-not-allowed border-zinc-900 text-zinc-700"
                  : "border-zinc-800 bg-zinc-950/45 text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100",
              )}
            >
              <Icon size={13} className="shrink-0" />
              <span className="truncate">{option.label}</span>
            </button>
          );
        })}
      </div>
    </section>
  );
}

function sidebarLaunchItems(items: SidebarItem[]): SidebarItem[] {
  return items
    .filter((item) => item.category === "tool" || item.category === "widget" || item.category === "system")
    .slice(0, 18);
}

export function WorkspaceLaunchpad({
  createOptions = WORKSPACE_TAB_CREATE_OPTIONS,
  sidebarItems,
  onCreate,
  onOpenSidebarItem,
  footer,
}: {
  createOptions?: WorkspaceTabCreateOption[];
  sidebarItems: SidebarItem[];
  onCreate: (kind: WorkspaceTabKind) => void;
  onOpenSidebarItem: (itemId: string) => void;
  footer?: ReactNode;
}) {
  const launchItems = useMemo(() => sidebarLaunchItems(sidebarItems), [sidebarItems]);

  return (
    <div className="flex min-h-0 flex-1 overflow-y-auto px-5 py-5">
      <div className="mx-auto w-full max-w-5xl space-y-5">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {createOptions.map((option) => {
            const Icon = option.icon;
            return (
              <button
                key={option.kind}
                type="button"
                disabled={option.disabled}
                onClick={() => !option.disabled && onCreate(option.kind)}
                className={cn(
                  "flex min-h-20 min-w-0 items-start gap-3 rounded-lg border px-3 py-3 text-left transition-colors",
                  option.disabled
                    ? "cursor-not-allowed border-zinc-900 bg-zinc-950/30 text-zinc-700"
                    : "border-zinc-800 bg-zinc-950/55 text-zinc-300 hover:border-zinc-700 hover:bg-zinc-900 hover:text-zinc-100",
                )}
              >
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-zinc-900 text-zinc-400">
                  <Icon size={18} />
                </span>
                <span className="min-w-0">
                  <span className="flex min-w-0 items-center gap-2">
                    <span className="truncate text-sm font-medium">{option.label}</span>
                    {option.badge && <span className="shrink-0 rounded bg-zinc-800 px-1 py-px text-[9px] text-zinc-500">{option.badge}</span>}
                  </span>
                  <span className="mt-1 block truncate text-[11px] text-zinc-500">{option.description}</span>
                </span>
              </button>
            );
          })}
        </div>
        {launchItems.length > 0 && (
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {launchItems.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => onOpenSidebarItem(item.id)}
                className="flex min-w-0 items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-950/45 px-3 py-2 text-left text-zinc-400 transition-colors hover:border-zinc-700 hover:bg-zinc-900 hover:text-zinc-100"
              >
                <AppWindow size={15} className="shrink-0 text-zinc-500" />
                <span className="min-w-0">
                  <span className="block truncate text-[12px] font-medium">{item.label}</span>
                  <span className="block truncate text-[10px] text-zinc-600">{item.category}</span>
                </span>
              </button>
            ))}
          </div>
        )}
        {footer}
      </div>
    </div>
  );
}
