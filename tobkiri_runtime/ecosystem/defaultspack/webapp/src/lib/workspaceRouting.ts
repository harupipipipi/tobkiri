import {
  DEFAULT_WORKSPACE_TAB_ID,
  createWorkspaceTab,
  type WorkspaceTab,
  type WorkspaceTabKind,
} from "../components/WorkspaceTabs";

export function workspaceKindForPathname(pathname: string): WorkspaceTabKind | null {
  const normalized = (pathname || "/").replace(/\/+$/, "") || "/";
  if (normalized === "/chat" || normalized === "/defaultspack" || normalized === "/pack/defaultspack" || normalized === "/") {
    return "chat";
  }
  if (normalized === "/coding") return "coding";
  if (normalized === "/calendar") return "calendar";
  if (normalized === "/kanban") return "kanban";
  if (normalized === "/desktops") return "desktops";
  if (normalized === "/subagents") return "subagents";
  if (normalized === "/canvas") return "canvas";
  if (normalized === "/tools") return "tools";
  return null;
}

function workspaceRoutePath(kind: WorkspaceTabKind): string {
  if (kind === "coding") return "/coding";
  if (kind === "calendar") return "/calendar";
  if (kind === "kanban") return "/kanban";
  if (kind === "desktops") return "/desktops";
  if (kind === "subagents") return "/subagents";
  if (kind === "canvas") return "/canvas";
  if (kind === "tools") return "/tools";
  return "/chat";
}

export function workspaceUrlForKind(kind: WorkspaceTabKind, href: string, conversationId: string | null = null): string {
  const url = new URL(href);
  url.pathname = workspaceRoutePath(kind);
  if (kind === "chat" || kind === "coding") {
    if (conversationId) url.searchParams.set("chat", conversationId);
    else url.searchParams.delete("chat");
    url.searchParams.delete("pending");
  } else {
    url.search = "";
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

export function initialWorkspaceTabsForPathname(pathname: string, now = Date.now()): WorkspaceTab[] {
  const tabs = [
    createWorkspaceTab("chat", { id: DEFAULT_WORKSPACE_TAB_ID, title: "New Conversation" }, now),
  ];
  const kind = workspaceKindForPathname(pathname);
  if (kind && kind !== "chat") {
    tabs.push(createWorkspaceTab(kind, { id: `workspace-tab-route-${kind}` }, now));
  }
  return tabs;
}

export function initialActiveWorkspaceTabIdForPathname(pathname: string): string {
  const kind = workspaceKindForPathname(pathname);
  return kind && kind !== "chat" ? `workspace-tab-route-${kind}` : DEFAULT_WORKSPACE_TAB_ID;
}
