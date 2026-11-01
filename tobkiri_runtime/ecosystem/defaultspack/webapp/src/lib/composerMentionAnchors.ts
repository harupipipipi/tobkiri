import type { DroppedWidget } from "../renderers/types";
import { composerMentionMetadataFromWidgets } from "./composerWidgets";
import { codePointIndexToUtf16Offset, isMentionStart, utf16OffsetToCodePointIndex } from "./mentionContract";

export type ConfirmedComposerMentionRange = { start: number; end: number; syntax: string };
type Confirmation = ConfirmedComposerMentionRange & { value: string };

function validRange(text: string, start: number, syntax: string): boolean {
  if (!syntax.startsWith("@") || syntax.length < 2 || !Number.isInteger(start) || start < 0
    || text.slice(start, start + syntax.length) !== syntax) return false;
  const point = utf16OffsetToCodePointIndex(text, start);
  if (codePointIndexToUtf16Offset(text, point) !== start
    || !isMentionStart(text, point, [syntax.slice(1)])) return false;
  const following = [...text.slice(start + syntax.length)];
  const token = /[\p{L}\p{M}\p{N}_./:-]/u;
  return !following[0] || (following[0] === "."
    ? !following[1] || !token.test(following[1]) : !token.test(following[0]));
}

/** Bind an admitted widget to exactly the explicitly confirmed UTF-16 occurrence. */
export function anchorComposerMentionWidget(widget: DroppedWidget, text: string, start: number): DroppedWidget {
  const mention = composerMentionMetadataFromWidgets([widget])[0];
  if (!mention || !validRange(text, start, mention.syntax)) return widget;
  return { ...widget, metadata: { ...widget.metadata, composer_confirmation: {
    value: text, start, end: start + mention.syntax.length, syntax: mention.syntax,
  } satisfies Confirmation } };
}

/** Read only a current anchor; legacy widgets resolve their first valid occurrence. */
export function confirmedComposerMentionRange(widget: DroppedWidget, text: string): ConfirmedComposerMentionRange | null {
  const mention = composerMentionMetadataFromWidgets([widget])[0];
  if (!mention || widget.enabled === false) return null;
  const raw = widget.metadata?.composer_confirmation;
  if (raw !== undefined) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const anchor = raw as Confirmation;
    return anchor.value === text && anchor.syntax === mention.syntax
      && anchor.end === anchor.start + mention.syntax.length
      && validRange(text, anchor.start, mention.syntax)
      ? { start: anchor.start, end: anchor.end, syntax: anchor.syntax } : null;
  }
  for (let start = text.indexOf(mention.syntax); start >= 0; start = text.indexOf(mention.syntax, start + 1)) {
    if (validRange(text, start, mention.syntax)) return { start, end: start + mention.syntax.length, syntax: mention.syntax };
  }
  return null;
}

/** Deduplicate widgets without collapsing independently confirmed occurrences. */
export function deduplicateComposerWidgets(widgets: DroppedWidget[], text: string): DroppedWidget[] {
  const byOccurrence = new Map<string, DroppedWidget>();
  for (const widget of widgets) {
    const range = widget.metadata?.composer_confirmation !== undefined
      ? confirmedComposerMentionRange(widget, text) : null;
    const key = range ? JSON.stringify([widget.id, range.start, range.end, range.syntax]) : widget.id;
    byOccurrence.set(key, widget);
  }
  return [...byOccurrence.values()];
}

/** Move untouched anchors across edits and discard any replaced confirmed occurrence. */
export function updateConfirmedComposerWidgets(
  previous: string,
  next: string,
  widgets: DroppedWidget[],
  edit?: { start: number; end: number },
): DroppedWidget[] {
  if (previous === next && !edit) return widgets;
  let start = 0;
  let end = previous.length;
  let ambiguousEnd = 0;
  if (edit) {
    start = Math.max(0, Math.min(edit.start, previous.length));
    end = Math.max(start, Math.min(edit.end, previous.length));
  } else {
    while (start < previous.length && start < next.length && previous[start] === next[start]) start += 1;
    let suffix = 0;
    while (suffix < previous.length && suffix < next.length
      && previous[previous.length - suffix - 1] === next[next.length - suffix - 1]) suffix += 1;
    // Repeated identical text makes the actual edit location ambiguous. Discard
    // anchors in every possible minimal edit span instead of transferring one.
    const earliestEnd = previous.length - suffix;
    if (start + suffix >= Math.min(previous.length, next.length)) {
      ambiguousEnd = start + Math.max(0, previous.length - next.length);
      start = Math.min(start, previous.length - suffix, next.length - suffix);
    }
    end = Math.max(start, earliestEnd);
  }
  const delta = next.length - previous.length;
  return widgets.flatMap((widget) => {
    if (widget.metadata?.source !== "composer_at_mention") return [widget];
    const range = confirmedComposerMentionRange(widget, previous);
    if (!range) return [];
    const touchedEnd = Math.max(end, ambiguousEnd);
    if (range.start < touchedEnd && range.end > start) return [];
    if (start === touchedEnd && range.start < start && range.end > start) return [];
    const movedStart = range.start >= end ? range.start + delta : range.start;
    if (!validRange(next, movedStart, range.syntax)) return [];
    return [anchorComposerMentionWidget(widget, next, movedStart)];
  });
}

/** Preserve confirmed occurrences through submit whitespace and escaped slash normalization. */
export function transformConfirmedComposerWidgetsForSubmit(
  source: string,
  submitted: string,
  widgets: DroppedWidget[],
): DroppedWidget[] {
  const trimmed = source.trim();
  const trimStart = source.length - source.trimStart().length;
  const slashOffset = trimmed.startsWith("//") && submitted === trimmed.slice(1) ? 1 : 0;
  if (submitted !== trimmed.slice(slashOffset)) {
    return widgets.filter((widget) => widget.metadata?.source !== "composer_at_mention");
  }
  const offset = trimStart + slashOffset;
  return widgets.flatMap((widget) => {
    if (widget.metadata?.source !== "composer_at_mention") return [widget];
    const range = confirmedComposerMentionRange(widget, source);
    if (!range || range.start < offset || range.end > offset + submitted.length) return [];
    return [anchorComposerMentionWidget(widget, submitted, range.start - offset)];
  });
}
