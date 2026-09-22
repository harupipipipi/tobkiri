import type { UICatalog } from "../lib/api";
import { hasShellRegion, shellRendererForRegion } from "../lib/uiShell";
import { ChatHeaderRenderer } from "./ChatHeaderRenderer";
import { ChatMessagesRenderer } from "./ChatMessagesRenderer";
import { ComposerRenderer } from "./ComposerRenderer";
import { HistoryBoardRenderer } from "./HistoryBoardRenderer";
import { RightSidebarRenderer } from "./RightSidebarRenderer";
import { SettingsModalRenderer } from "./SettingsModalRenderer";
import { TitleBarRenderer } from "./TitleBarRenderer";
import { ToolPreviewPanelRenderer } from "./ToolPreviewPanelRenderer";
import { loadTrustedRenderer, rendererSafeModeEnabled } from "./trustedRendererLoader";

export const defaultspackRendererIds = [
  "title_bar",
  "history",
  "chat_header",
  "chat_messages",
  "composer",
  "activity_preview",
  "right_sidebar",
  "settings_modal",
] as const;

export type DefaultspackRendererId = (typeof defaultspackRendererIds)[number];

export const defaultspackRenderers = {
  titleBar: TitleBarRenderer,
  historyBoard: HistoryBoardRenderer,
  chatHeader: ChatHeaderRenderer,
  chatMessages: ChatMessagesRenderer,
  composer: ComposerRenderer,
  toolPreviewPanel: ToolPreviewPanelRenderer,
  rightSidebar: RightSidebarRenderer,
  settingsModal: SettingsModalRenderer,
} as const;

export function isDefaultspackRegionVisible(
  catalog: UICatalog | null | undefined,
  regionId: string,
  safeMode: boolean,
): boolean {
  return safeMode || !catalog?.shell || hasShellRegion(catalog, regionId);
}

export function resolveDefaultspackRenderers(
  catalog: UICatalog | null | undefined,
  options: { safeMode?: boolean } = {},
) {
  const safeMode = options.safeMode ?? rendererSafeModeEnabled();
  const rendererForRegion = (regionId: string) => safeMode ? null : shellRendererForRegion(catalog, regionId);
  return {
    titleBar: loadTrustedRenderer(rendererForRegion("title_bar"), TitleBarRenderer),
    historyBoard: loadTrustedRenderer(rendererForRegion("history"), HistoryBoardRenderer),
    chatHeader: loadTrustedRenderer(rendererForRegion("chat_header"), ChatHeaderRenderer),
    chatMessages: loadTrustedRenderer(rendererForRegion("chat_messages"), ChatMessagesRenderer),
    composer: loadTrustedRenderer(rendererForRegion("composer"), ComposerRenderer),
    toolPreviewPanel: loadTrustedRenderer(rendererForRegion("activity_preview"), ToolPreviewPanelRenderer),
    rightSidebar: loadTrustedRenderer(rendererForRegion("right_sidebar"), RightSidebarRenderer),
    settingsModal: loadTrustedRenderer(rendererForRegion("settings_modal"), SettingsModalRenderer),
  };
}
