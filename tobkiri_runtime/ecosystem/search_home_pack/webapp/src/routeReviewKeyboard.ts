import { cycleCandidateIndex, type RouteDecision } from "./routerTypes";

export type RouteReviewKeyboardAction = "previous" | "next" | "confirm";

export type RouteReviewKeyboardEvent = Pick<
  KeyboardEvent,
  | "altKey"
  | "ctrlKey"
  | "defaultPrevented"
  | "isComposing"
  | "key"
  | "metaKey"
  | "repeat"
  | "shiftKey"
  | "target"
  | "currentTarget"
>;

/**
 * Return the candidate-review action for a narrowly scoped keyboard event.
 *
 * The review surface must itself own focus. Events from descendants are rejected
 * so inputs, buttons, dialogs, results, and contenteditable controls retain their
 * native keyboard behavior.
 */
export function routeReviewKeyboardAction(
  event: RouteReviewKeyboardEvent,
  shortcutsEnabled: boolean,
): RouteReviewKeyboardAction | null {
  if (
    !shortcutsEnabled ||
    event.defaultPrevented ||
    event.target !== event.currentTarget ||
    event.isComposing ||
    event.repeat ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey
  ) {
    return null;
  }
  if (event.key === "ArrowLeft") return "previous";
  if (event.key === "ArrowRight") return "next";
  if (event.key === "Enter") return "confirm";
  return null;
}

/** Resolve selection-only changes separately from an explicit confirmation. */
export function resolveRouteReviewKeyboardAction(
  decision: RouteDecision,
  currentIndex: number,
  action: RouteReviewKeyboardAction,
): { nextIndex: number; shouldConfirm: boolean } {
  if (action === "confirm") {
    return { nextIndex: currentIndex, shouldConfirm: true };
  }
  return {
    nextIndex: cycleCandidateIndex(
      decision,
      currentIndex,
      action === "previous" ? -1 : 1,
    ),
    shouldConfirm: false,
  };
}
