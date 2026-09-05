import { PanelRightClose, PanelRightOpen, Settings2 } from "lucide-react";

import type { ChatHeaderRendererProps } from "./types";

export function ChatHeaderRenderer({
  title,
  showPreview,
  canShowPreview,
  canOpenSettings,
  onTogglePreview,
  onOpenSettings,
}: ChatHeaderRendererProps) {
  if (!canShowPreview && !canOpenSettings) return null;
  return (
    <header
      data-tauri-drag-region
      aria-label={`${title} actions`}
      className="rumi-chat-context-toolbar flex h-9 flex-shrink-0 items-center justify-end gap-1 border-b border-zinc-800/50 bg-[#09090b]/80 px-3 backdrop-blur-md rumi-layer-panel rumi-anim-fade-down"
    >
      <h2 className="sr-only">{title}</h2>
      {canShowPreview && (
        <button
          type="button"
          onClick={onTogglePreview}
          aria-pressed={showPreview}
          className="inline-flex h-8 items-center gap-1.5 rounded-md px-2.5 text-[11px] font-medium text-zinc-500 transition-colors hover:bg-zinc-900 hover:text-zinc-100 aria-pressed:bg-zinc-800 aria-pressed:text-zinc-100"
          title={showPreview ? "Close canvas" : "Open canvas"}
        >
          {showPreview ? <PanelRightClose size={14} aria-hidden="true" /> : <PanelRightOpen size={14} aria-hidden="true" />}
          <span>Canvas</span>
        </button>
      )}
      {canOpenSettings && (
        <button
          type="button"
          onClick={onOpenSettings}
          className="inline-flex h-8 items-center gap-1.5 rounded-md px-2.5 text-[11px] font-medium text-zinc-500 transition-colors hover:bg-zinc-900 hover:text-zinc-100"
          title="Open settings"
        >
          <Settings2 size={14} aria-hidden="true" />
          <span>Settings</span>
        </button>
      )}
    </header>
  );
}
