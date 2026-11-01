export interface PopoverRect {
  left: number;
  top: number;
  bottom: number;
}

/** Flip above the anchor when necessary and keep the entire panel in view. */
export function viewportPopoverPosition(
  anchor: PopoverRect,
  viewport: { left: number; top: number; width: number; height: number },
  desiredWidth = 520,
  desiredHeight = 360,
  preferredPlacement: "auto" | "above" | "below" = "auto",
) {
  const margin = 16;
  const gap = 6;
  const horizontalMargin = Math.min(margin, viewport.width / 2);
  const width = Math.max(0, Math.min(desiredWidth, viewport.width - horizontalMargin * 2));
  const minTop = viewport.top + Math.min(margin, viewport.height / 2);
  const maxBottom = viewport.top + viewport.height - Math.min(margin, viewport.height / 2);
  const below = Math.max(0, maxBottom - Math.max(minTop, anchor.bottom + gap));
  const above = Math.max(0, Math.min(maxBottom, anchor.top - gap) - minTop);
  const side = preferredPlacement === "above" && above >= desiredHeight
    ? "top"
    : preferredPlacement === "below" && below >= desiredHeight
      ? "bottom"
      : below >= desiredHeight || below >= above ? "bottom" : "top";
  const maxHeight = Math.min(desiredHeight, side === "bottom" ? below : above);
  const top = side === "bottom"
    ? Math.min(maxBottom, Math.max(minTop, anchor.bottom + gap))
    : Math.max(minTop, Math.min(maxBottom, anchor.top - gap) - maxHeight);
  return {
    left: Math.max(viewport.left + horizontalMargin, Math.min(anchor.left, viewport.left + viewport.width - horizontalMargin - width)),
    top,
    width,
    maxHeight,
    side,
  };
}
