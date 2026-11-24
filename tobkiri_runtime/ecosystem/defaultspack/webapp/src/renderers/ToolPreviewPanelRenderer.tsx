import { ToolPreviewPanel } from "../components/ToolPreview";
import type { ToolPreviewPanelRendererProps } from "./types";

export function ToolPreviewPanelRenderer({
  previews,
  showPreview,
  activePreviewId,
  activePreviewRevision,
  memo,
  onClose,
  onMemoChange,
}: ToolPreviewPanelRendererProps) {
  return (
    <div className="w-full min-w-0 h-full rumi-anim-fade-right">
      <ToolPreviewPanel
        previews={previews}
        isVisible={showPreview}
        onClose={onClose}
        activePreviewId={activePreviewId}
        activePreviewRevision={activePreviewRevision}
        memo={memo}
        onMemoChange={onMemoChange}
      />
    </div>
  );
}
