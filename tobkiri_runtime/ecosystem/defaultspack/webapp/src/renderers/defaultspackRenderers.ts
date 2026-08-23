import type { UICatalog } from "../lib/api";
import { shellRendererForRegion } from "../lib/uiShell";
import { ChatHeaderRenderer } from "./ChatHeaderRenderer";
import { ChatMessagesRenderer } from "./ChatMessagesRenderer";
import { ComposerRenderer } from "./ComposerRenderer";
import { HistoryBoardRenderer } from "./HistoryBoardRenderer";
import { RightSidebarRenderer } from "./RightSidebarRenderer";
import { SettingsModalRenderer } from "./SettingsModalRenderer";
import { TitleBarRenderer } from "./TitleBarRenderer";
import { ToolPreviewPanelRenderer } from "./ToolPreviewPanelRenderer";
import { loadTrustedRenderer } from "./trustedRendererLoader";
import { SurfaceTemplateRenderer } from "../surface/SurfaceTemplateRenderer";

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
  surfaceTemplate: SurfaceTemplateRenderer,
} as const;

export function resolveDefaultspackRenderers(catalog: UICatalog | null | undefined) {
  return {
    titleBar: loadTrustedRenderer(shellRendererForRegion(catalog, "title_bar"), TitleBarRenderer),
    historyBoard: loadTrustedRenderer(shellRendererForRegion(catalog, "history"), HistoryBoardRenderer),
    chatHeader: loadTrustedRenderer(shellRendererForRegion(catalog, "chat_header"), ChatHeaderRenderer),
    chatMessages: loadTrustedRenderer(shellRendererForRegion(catalog, "chat_messages"), ChatMessagesRenderer),
    composer: loadTrustedRenderer(shellRendererForRegion(catalog, "composer"), ComposerRenderer),
    toolPreviewPanel: loadTrustedRenderer(shellRendererForRegion(catalog, "activity_preview"), ToolPreviewPanelRenderer),
    rightSidebar: loadTrustedRenderer(shellRendererForRegion(catalog, "right_sidebar"), RightSidebarRenderer),
    settingsModal: loadTrustedRenderer(shellRendererForRegion(catalog, "settings_modal"), SettingsModalRenderer),
  };
}
