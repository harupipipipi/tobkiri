export type CalendarAnchorRect = Pick<DOMRect, "left" | "top" | "right" | "bottom" | "width" | "height">;

export type CalendarViewportSize = { width: number; height: number };

export type CalendarPopoverPlacement = {
  left: number;
  top: number;
  width: number;
  maxHeight: number;
  side: "top" | "bottom";
  align: "left" | "right";
  transformOrigin: string;
};

export type CalendarTimeMenuKeyAction =
  | { handled: false }
  | { handled: true; nextOpen: boolean; nextActiveIndex: number; selectedIndex?: number };

export type CalendarOverlayDismissAction = { closeEditor: boolean; closeTimeMenu: boolean };

const DEFAULT_MARGIN = 12;
const DEFAULT_GAP = 8;

function clamp(value: number, min: number, max: number): number {
  if (max < min) return min;
  return Math.min(Math.max(value, min), max);
}

/** Place a calendar overlay next to its anchor without leaving the viewport. */
export function placeCalendarFloatingOverlay(
  anchor: CalendarAnchorRect,
  viewport: CalendarViewportSize,
  {
    preferredWidth,
    preferredHeight,
    minHeight = 160,
    margin = DEFAULT_MARGIN,
    gap = DEFAULT_GAP,
  }: {
    preferredWidth: number;
    preferredHeight: number;
    minHeight?: number;
    margin?: number;
    gap?: number;
  },
): CalendarPopoverPlacement {
  const availableWidth = Math.max(0, viewport.width - margin * 2);
  const width = Math.min(preferredWidth, availableWidth);
  const align: "left" | "right" = anchor.left + width > viewport.width - margin ? "right" : "left";
  const rawLeft = align === "right" ? anchor.right - width : anchor.left;
  const left = clamp(rawLeft, margin, viewport.width - margin - width);
  const belowTop = anchor.bottom + gap;
  const belowSpace = Math.max(0, viewport.height - margin - belowTop);
  const aboveSpace = Math.max(0, anchor.top - margin - gap);
  const side: "top" | "bottom" = belowSpace >= Math.min(preferredHeight, minHeight) || belowSpace >= aboveSpace ? "bottom" : "top";
  const availableHeight = side === "bottom" ? belowSpace : aboveSpace;
  const viewportHeight = Math.max(0, viewport.height - margin * 2);
  const maxHeight = Math.min(
    viewportHeight,
    Math.max(Math.min(minHeight, viewportHeight), Math.min(preferredHeight, availableHeight)),
  );
  const top = side === "bottom"
    ? clamp(belowTop, margin, viewport.height - margin - maxHeight)
    : clamp(anchor.top - gap - maxHeight, margin, viewport.height - margin - maxHeight);

  return {
    align,
    left,
    maxHeight,
    side,
    top,
    transformOrigin: `${align === "right" ? "calc(100% - 18px)" : "18px"} ${side === "top" ? "calc(100% - 18px)" : "18px"}`,
    width,
  };
}

/** Resolve WAI-ARIA combobox keys without moving DOM focus into the listbox. */
export function calendarTimeMenuKeyAction({
  key,
  isOpen,
  activeIndex,
  selectedIndex,
  optionCount,
}: {
  key: string;
  isOpen: boolean;
  activeIndex: number;
  selectedIndex: number;
  optionCount: number;
}): CalendarTimeMenuKeyAction {
  if (optionCount <= 0) return { handled: false };
  const baseIndex = activeIndex >= 0 ? activeIndex : selectedIndex >= 0 ? selectedIndex : 0;
  if (key === "ArrowDown") return { handled: true, nextActiveIndex: isOpen ? (baseIndex + 1) % optionCount : baseIndex, nextOpen: true };
  if (key === "ArrowUp") return { handled: true, nextActiveIndex: isOpen ? (baseIndex - 1 + optionCount) % optionCount : baseIndex, nextOpen: true };
  if (key === "Home" && isOpen) return { handled: true, nextActiveIndex: 0, nextOpen: true };
  if (key === "End" && isOpen) return { handled: true, nextActiveIndex: optionCount - 1, nextOpen: true };
  if ((key === "Enter" || key === " ") && isOpen && activeIndex >= 0) {
    return { handled: true, nextActiveIndex: activeIndex, nextOpen: false, selectedIndex: activeIndex };
  }
  if (key === "Escape" && isOpen) return { handled: true, nextActiveIndex: activeIndex, nextOpen: false };
  return { handled: false };
}

/** Decide which calendar surfaces an intentional pointer interaction dismisses. */
export function calendarOverlayPointerAction({
  hasActiveEditor,
  higherPriorityOverlayOwnsEvent = false,
  isTimeMenuOpen,
  insideCalendar,
  insideEditorPopover,
  insideTimeInput,
  insideTimeMenu,
}: {
  hasActiveEditor: boolean;
  higherPriorityOverlayOwnsEvent?: boolean;
  isTimeMenuOpen: boolean;
  insideCalendar: boolean;
  insideEditorPopover: boolean;
  insideTimeInput: boolean;
  insideTimeMenu: boolean;
}): CalendarOverlayDismissAction {
  if (higherPriorityOverlayOwnsEvent) return { closeEditor: false, closeTimeMenu: false };
  const insideCalendarSurface = insideCalendar || insideEditorPopover || insideTimeMenu;
  return {
    closeEditor: hasActiveEditor && !insideCalendarSurface,
    closeTimeMenu: isTimeMenuOpen && !insideTimeMenu && !insideTimeInput,
  };
}

/** Close the innermost calendar overlay first unless a higher layer owns Escape. */
export function calendarEscapeAction({
  hasActiveEditor,
  isTimeMenuOpen,
  higherPriorityOverlayOwnsEvent = false,
}: {
  hasActiveEditor: boolean;
  isTimeMenuOpen: boolean;
  higherPriorityOverlayOwnsEvent?: boolean;
}): CalendarOverlayDismissAction {
  if (higherPriorityOverlayOwnsEvent) return { closeEditor: false, closeTimeMenu: false };
  return {
    closeEditor: hasActiveEditor && !isTimeMenuOpen,
    closeTimeMenu: isTimeMenuOpen,
  };
}
