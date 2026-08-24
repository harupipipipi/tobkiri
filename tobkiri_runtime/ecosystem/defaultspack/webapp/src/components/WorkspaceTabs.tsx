import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
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
    label: "AIチャット",
    description: "会話ワークスペース",
    icon: MessageSquareText,
  },
  {
    kind: "coding",
    label: "コーディング",
    description: "開発ワークスペース",
    icon: Code2,
  },
  {
    kind: "calendar",
    label: "カレンダー",
    description: "予定とスケジュール",
    icon: CalendarDays,
  },
  {
    kind: "kanban",
    label: "カンバン",
    description: "タスクとエージェントのボード",
    icon: KanbanSquare,
  },
  {
    kind: "desktops",
    label: "デスクトップ",
    description: "管理中のデスクトップ",
    icon: Monitor,
  },
  {
    kind: "subagents",
    label: "サブエージェント / チーム",
    description: "チャンネル、DM、承認",
    icon: UsersRound,
  },
  {
    kind: "canvas",
    label: "Canvas",
    description: "ツールのプレビュー領域",
    icon: GalleryVerticalEnd,
  },
  {
    kind: "tools",
    label: "ツール",
    description: "ツールとウィジェットを起動",
    icon: LayoutGrid,
  },
  {
    kind: "browser",
    label: "ブラウザ",
    description: "近日公開",
    icon: Globe,
    disabled: true,
    badge: "近日公開",
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
  if (title === "AI Chat") return "AIチャット";
  if (title === "New Conversation") return "新しい会話";
  if (title) return title;
  return workspaceTabOption(tab.kind).label;
}

function iconForKind(kind: WorkspaceTabKind): LucideIcon {
  return workspaceTabOption(kind).icon;
}

/** Return the stable DOM id used by one tab and its controlled panel. */
export function workspaceTabDomId(tabId: string, part: "tab" | "panel"): string {
  return `workspace-${part}-${encodeURIComponent(tabId)}`;
}

export type WorkspaceTabNavigationKey = "ArrowLeft" | "ArrowRight" | "Home" | "End";
export type WorkspaceTabKeyboardAction = WorkspaceTabNavigationKey | "activate" | "close" | null;

/** Map supported tab keys to the action implemented by the tablist. */
export function workspaceTabKeyboardAction(key: string): WorkspaceTabKeyboardAction {
  if (key === "ArrowLeft" || key === "ArrowRight" || key === "Home" || key === "End") {
    return key;
  }
  if (key === "Enter" || key === " ") return "activate";
  if (key === "Delete") return "close";
  return null;
}

/** Resolve circular horizontal tab navigation for the automatic activation model. */
export function nextWorkspaceTabIndex(
  tabCount: number,
  currentIndex: number,
  key: WorkspaceTabNavigationKey,
): number {
  if (tabCount <= 0) return -1;
  if (key === "Home") return 0;
  if (key === "End") return tabCount - 1;
  if (key === "ArrowLeft") return (currentIndex - 1 + tabCount) % tabCount;
  return (currentIndex + 1) % tabCount;
}

/** Resolve contained keyboard navigation within the new-workspace dialog. */
export function nextWorkspaceCreateOptionIndex(
  optionCount: number,
  currentIndex: number,
  key: string,
  shiftKey = false,
): number | null {
  if (optionCount <= 0) return null;
  const safeIndex = Math.max(0, Math.min(currentIndex, optionCount - 1));
  if (key === "Home") return 0;
  if (key === "End") return optionCount - 1;
  if (key === "ArrowRight" || key === "ArrowDown" || (key === "Tab" && !shiftKey)) {
    return (safeIndex + 1) % optionCount;
  }
  if (key === "ArrowLeft" || key === "ArrowUp" || (key === "Tab" && shiftKey)) {
    return (safeIndex - 1 + optionCount) % optionCount;
  }
  return null;
}

/** Resolve the tab that remains active and receives focus after a close. */
export function workspaceTabIdAfterClose(
  tabs: WorkspaceTab[],
  activeTabId: string,
  closedTabId: string,
): string | null {
  if (tabs.length <= 1 || !tabs.some((tab) => tab.id === closedTabId)) return null;
  if (closedTabId !== activeTabId) return activeTabId;
  const closedIndex = tabs.findIndex((tab) => tab.id === closedTabId);
  const remaining = tabs.filter((tab) => tab.id !== closedTabId);
  return remaining[Math.max(0, closedIndex - 1)]?.id ?? remaining[0]?.id ?? null;
}

/** Render the focus-contained chooser opened by the new-tab trigger. */
export function NewTabDialog({
  dialogId,
  options,
  onCreate,
  onDismiss,
}: {
  dialogId: string;
  options: WorkspaceTabCreateOption[];
  onCreate: (kind: WorkspaceTabKind) => void;
  onDismiss: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const titleId = `${dialogId}-title`;

  const enabledButtons = (): HTMLButtonElement[] =>
    Array.from(
      dialogRef.current?.querySelectorAll<HTMLButtonElement>(
        "button[data-workspace-create-option]:not(:disabled)",
      ) ?? [],
    );

  useEffect(() => {
    enabledButtons()[0]?.focus();
  }, []);

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onDismiss();
      return;
    }
    const buttons = enabledButtons();
    if (buttons.length === 0) return;
    const currentIndex = Math.max(0, buttons.indexOf(document.activeElement as HTMLButtonElement));
    const nextIndex = nextWorkspaceCreateOptionIndex(
      buttons.length,
      currentIndex,
      event.key,
      event.shiftKey,
    );
    if (nextIndex === null) return;
    event.preventDefault();
    buttons[nextIndex]?.focus();
  };

  return (
    <div
      ref={dialogRef}
      id={dialogId}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      onKeyDown={handleKeyDown}
      className="rumi-workspace-new-tab-menu absolute left-0 top-[calc(100%+6px)] rumi-layer-modal w-[min(420px,calc(100vw-24px))] overflow-hidden rounded-xl border border-zinc-700/70 bg-zinc-950 py-2 shadow-2xl"
    >
      <h2 id={titleId} className="px-3 pb-2 text-xs font-semibold text-zinc-200">
        新しいワークスペース
      </h2>
      <div className="grid grid-cols-2 gap-1.5 px-2">
        {options.map((option) => {
          const Icon = option.icon;
          const contents = (
            <>
              <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-zinc-900 text-zinc-400">
                <Icon size={16} aria-hidden="true" />
              </span>
              <span className="min-w-0">
                <span className="flex min-w-0 items-center gap-1.5">
                  <span className="break-words text-[12px] font-medium leading-4">{option.label}</span>
                  {option.badge && <span className="shrink-0 rounded bg-zinc-800 px-1 py-px text-[8px] text-zinc-500">{option.badge}</span>}
                </span>
                <span className="mt-0.5 block text-[10px] leading-4 text-zinc-500">{option.description}</span>
              </span>
            </>
          );
          if (option.disabled) {
            return (
              <div
                key={option.kind}
                role="note"
                aria-disabled="true"
                data-workspace-disabled-option={option.kind}
                className="flex min-h-16 min-w-0 items-start gap-2 rounded-lg border border-zinc-900 bg-zinc-950/40 px-2.5 py-2 text-left text-zinc-700"
              >
                {contents}
              </div>
            );
          }
          return (
            <button
              key={option.kind}
              type="button"
              data-workspace-create-option={option.kind}
              onClick={() => onCreate(option.kind)}
              className="flex min-h-16 min-w-0 items-start gap-2 rounded-lg border border-zinc-800 bg-zinc-950/70 px-2.5 py-2 text-left text-zinc-300 transition-colors hover:border-zinc-700 hover:bg-zinc-900 hover:text-zinc-100"
            >
              {contents}
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
  const menuTriggerRef = useRef<HTMLButtonElement | null>(null);
  const tabRefs = useRef(new Map<string, HTMLButtonElement>());
  const pendingFocusTabIdRef = useRef<string | null>(null);
  const generatedMenuId = useId();
  const menuId = `rumi-new-workspace-tab-dialog-${generatedMenuId.replace(/:/g, "")}`;

  const closeMenu = (restoreFocus = true) => {
    setIsMenuOpen(false);
    if (restoreFocus) requestAnimationFrame(() => menuTriggerRef.current?.focus());
  };

  useEffect(() => {
    if (!isMenuOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node) || menuRef.current?.contains(target)) return;
      closeMenu();
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeMenu();
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isMenuOpen]);

  useEffect(() => {
    const pendingTabId = pendingFocusTabIdRef.current;
    if (!pendingTabId) return;
    const target = tabRefs.current.get(pendingTabId);
    if (!target) return;
    pendingFocusTabIdRef.current = null;
    target.focus();
    target.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [activeTabId, tabs]);

  const handleCreate = (kind: WorkspaceTabKind) => {
    onCreate(kind);
    closeMenu();
  };

  const registerTabRef = (tabId: string) => (element: HTMLButtonElement | null) => {
    if (element) tabRefs.current.set(tabId, element);
    else tabRefs.current.delete(tabId);
  };

  const focusAndActivateTab = (index: number) => {
    const tab = tabs[index];
    if (!tab) return;
    const target = tabRefs.current.get(tab.id);
    target?.focus();
    target?.scrollIntoView({ block: "nearest", inline: "nearest" });
    onSelect(tab.id);
  };

  const handleTabKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    index: number,
    tabId: string,
  ) => {
    const action = workspaceTabKeyboardAction(event.key);
    if (action === "ArrowLeft" || action === "ArrowRight" || action === "Home" || action === "End") {
      event.preventDefault();
      focusAndActivateTab(
        nextWorkspaceTabIndex(tabs.length, index, action),
      );
      return;
    }
    if (action === "activate") {
      event.preventDefault();
      onSelect(tabId);
      return;
    }
    if (action === "close" && tabs.length > 1) {
      event.preventDefault();
      pendingFocusTabIdRef.current = workspaceTabIdAfterClose(tabs, activeTabId, tabId);
      onClose(tabId);
    }
  };

  const handleClose = (tabId: string) => {
    pendingFocusTabIdRef.current = workspaceTabIdAfterClose(tabs, activeTabId, tabId);
    onClose(tabId);
  };

  return (
    <div className="rumi-workspace-tabbar flex h-10 shrink-0 items-end gap-1 border-b border-zinc-800/60 bg-[#09090b] px-2 pt-1">
      <div
        role="tablist"
        aria-label="開いているワークスペース"
        aria-orientation="horizontal"
        className="flex min-w-0 flex-1 items-end gap-1 overflow-x-auto overflow-y-hidden pb-0.5 scrollbar-none"
      >
        {tabs.map((tab) => {
          const Icon = iconForKind(tab.kind);
          const isActive = tab.id === activeTabId;
          const title = workspaceTabDisplayTitle(tab);
          return (
            <div
              key={tab.id}
              role="presentation"
              className={cn(
                "group/tab flex h-9 max-w-52 min-w-24 items-center gap-1.5 rounded-t-lg border px-1.5 text-left text-[12px] transition-colors",
                isActive
                  ? "border-zinc-700 border-b-[#09090b] bg-[#111116] text-zinc-100"
                  : "border-transparent bg-zinc-950/40 text-zinc-500 hover:bg-zinc-900 hover:text-zinc-200",
              )}
              title={title}
            >
              <button
                ref={registerTabRef(tab.id)}
                type="button"
                role="tab"
                id={workspaceTabDomId(tab.id, "tab")}
                aria-selected={isActive}
                aria-controls={workspaceTabDomId(tab.id, "panel")}
                tabIndex={isActive ? 0 : -1}
                onClick={() => onSelect(tab.id)}
                onKeyDown={(event) => handleTabKeyDown(event, tabs.indexOf(tab), tab.id)}
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
                    handleClose(tab.id);
                  }}
                  className={cn(
                    "flex h-9 w-9 shrink-0 items-center justify-center rounded-md text-zinc-600 transition-[opacity,color,background-color] hover:bg-zinc-800 hover:text-zinc-200 focus:opacity-100 group-hover/tab:opacity-100 group-focus-within/tab:opacity-100",
                    isActive ? "opacity-60" : "opacity-0",
                  )}
                  tabIndex={isActive ? 0 : -1}
                  title="タブを閉じる"
                  aria-label={`${title}を閉じる`}
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
          ref={menuTriggerRef}
          type="button"
          onClick={() => {
            if (isMenuOpen) closeMenu();
            else setIsMenuOpen(true);
          }}
          className={cn(
            "flex h-9 w-9 items-center justify-center rounded-lg text-zinc-500 transition-colors hover:bg-zinc-900 hover:text-zinc-100",
            isMenuOpen && "bg-zinc-900 text-zinc-100",
          )}
          title="新しいタブ"
          aria-label="新しいタブ"
          aria-haspopup="dialog"
          aria-expanded={isMenuOpen}
          aria-controls={isMenuOpen ? menuId : undefined}
        >
          <Plus size={16} />
        </button>
        {isMenuOpen && (
          <NewTabDialog
            dialogId={menuId}
            options={createOptions}
            onCreate={handleCreate}
            onDismiss={() => closeMenu()}
          />
        )}
      </div>
    </div>
  );
}

/**
 * Render the controlled panels for the APG-style automatic workspace tab set.
 * Inactive panels remain in the accessibility relationship but are hidden.
 */
export function WorkspaceTabPanels({
  tabs,
  activeTabId,
  children,
}: {
  tabs: WorkspaceTab[];
  activeTabId: string;
  children: ReactNode;
}) {
  return (
    <>
      {tabs.map((tab) => {
        const isActive = tab.id === activeTabId;
        return (
          <div
            key={tab.id}
            id={workspaceTabDomId(tab.id, "panel")}
            role="tabpanel"
            aria-labelledby={workspaceTabDomId(tab.id, "tab")}
            hidden={!isActive}
            tabIndex={isActive ? 0 : -1}
            className={isActive ? "flex min-h-0 flex-1 flex-col" : undefined}
          >
            {isActive ? children : null}
          </div>
        );
      })}
    </>
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
                  className="flex h-9 w-9 shrink-0 items-center justify-center rounded text-zinc-600 opacity-0 hover:bg-zinc-800 hover:text-zinc-200 focus:opacity-100 group-hover:opacity-100 group-focus-within:opacity-100"
                  title="タブを閉じる"
                  aria-label={`${title}を閉じる`}
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
