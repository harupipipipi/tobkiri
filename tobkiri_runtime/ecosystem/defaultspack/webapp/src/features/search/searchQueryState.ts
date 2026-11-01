import {
  applySearchSuggestion,
  parseSearchQuery,
  suggestSearchFilters,
  type ParsedSearchQuery,
  type SearchPreset,
  type SearchSuggestion,
  type SearchToken,
} from "./searchQuery";

export type SearchQueryState = { value: string; confirmed: SearchToken[] };
/** The replaced range in the old value, captured before native input mutation. */
export type SearchQueryEdit = { start: number; end: number };

/** New input starts as plain text, including complete mention spellings. */
export function createSearchQueryState(value = ""): SearchQueryState {
  return { value, confirmed: [] };
}

function validTokens(state: SearchQueryState): SearchToken[] {
  const recognized = parseSearchQuery(state.value).tokens;
  return state.confirmed.filter((token) => recognized.some((candidate) =>
    candidate.start === token.start && candidate.end === token.end
    && candidate.raw === token.raw && candidate.axis === token.axis
    && candidate.value === token.value,
  ));
}

/** Carry confirmations across edits only when their complete word survives. */
export function updateSearchQueryState(
  state: SearchQueryState,
  nextValue: string,
  edit?: SearchQueryEdit,
): SearchQueryState {
  if (state.value === nextValue && edit === undefined) {
    return { value: nextValue, confirmed: validTokens(state) };
  }
  let start = 0;
  let oldEnd = state.value.length;
  let newEnd = nextValue.length;
  if (edit !== undefined) {
    start = edit.start;
    oldEnd = edit.end;
    newEnd = oldEnd + nextValue.length - state.value.length;
    const validEdit = Number.isInteger(start) && Number.isInteger(oldEnd)
      && start >= 0 && oldEnd >= start && oldEnd <= state.value.length
      && newEnd >= start && newEnd <= nextValue.length
      && state.value.slice(0, start) === nextValue.slice(0, start)
      && state.value.slice(oldEnd) === nextValue.slice(newEnd);
    if (!validEdit) return { value: nextValue, confirmed: [] };
  } else {
    while (start < state.value.length && start < nextValue.length
      && state.value[start] === nextValue[start]) start += 1;
    while (oldEnd > start && newEnd > start
      && state.value[oldEnd - 1] === nextValue[newEnd - 1]) {
      oldEnd -= 1;
      newEnd -= 1;
    }
  }
  const oldTokens = parseSearchQuery(state.value).tokens;
  const newTokens = parseSearchQuery(nextValue).tokens;
  const ambiguous = new Set<string>();
  if (edit === undefined) {
    for (const token of oldTokens) {
      const oldCount = oldTokens.filter((item) => item.raw === token.raw).length;
      const newCount = newTokens.filter((item) => item.raw === token.raw).length;
      if (oldCount > 1 && newCount < oldCount) ambiguous.add(token.raw);
    }
  }
  const delta = newEnd - oldEnd;
  const confirmed = validTokens(state).flatMap((token) => {
    if (ambiguous.has(token.raw)) return [];
    if (token.end <= start) return [{ ...token }];
    if (token.start >= oldEnd) return [{ ...token, start: token.start + delta, end: token.end + delta }];
    return [];
  });
  const next = { value: nextValue, confirmed };
  return { value: nextValue, confirmed: validTokens(next) };
}

/** Confirm only the selected occurrence, and leave a separator at the end. */
export function confirmSearchSuggestion(
  state: SearchQueryState,
  suggestion: SearchSuggestion,
): { state: SearchQueryState; caret: number } {
  const { start, end } = suggestion;
  const validRange = Number.isInteger(start) && Number.isInteger(end)
    && start >= 0 && end > start && end <= state.value.length
    && (start === 0 || /\s/u.test(state.value[start - 1]))
    && (end === state.value.length || /\s/u.test(state.value[end]))
    && /^@\S*$/u.test(state.value.slice(start, end));
  const recognized = parseSearchQuery(suggestion.token).tokens;
  if (!validRange || recognized.length !== 1 || recognized[0].raw !== suggestion.token) {
    return { state, caret: suggestion.end };
  }
  const replacement = applySearchSuggestion(state.value, suggestion);
  const tokenEnd = suggestion.start + suggestion.token.length;
  const value = replacement + (tokenEnd === replacement.length ? " " : "");
  const next = updateSearchQueryState(state, value, { start, end });
  const token = { ...recognized[0], start: suggestion.start, end: tokenEnd };
  next.confirmed = next.confirmed.filter((item) => item.start !== token.start || item.end !== token.end);
  next.confirmed.push(token);
  next.confirmed.sort((a, b) => a.start - b.start);
  return { state: next, caret: tokenEnd + (value[tokenEnd] === " " ? 1 : 0) };
}

/** Apply confirmed filters while retaining drafts as ordinary search text. */
export function parseConfirmedSearchQuery(state: SearchQueryState, preset?: SearchPreset): ParsedSearchQuery {
  const tokens = validTokens(state).sort((a, b) => a.start - b.start);
  const result = parseSearchQuery(tokens.map((token) => token.raw).join(" "), preset);
  let offset = 0;
  const text: string[] = [];
  for (const token of tokens) {
    text.push(state.value.slice(offset, token.start));
    offset = token.end;
  }
  text.push(state.value.slice(offset));
  const words = text.join(" ").match(/\S+/gu) ?? [];
  return { ...result, tokens, text: words.join(" "), unknownTokens: words.filter((word) => word.startsWith("@")) };
}

/** A fully typed draft still needs a selectable confirmation candidate. */
export function getSearchQuerySuggestions(state: SearchQueryState, caret = state.value.length): SearchSuggestion[] {
  if (!Number.isInteger(caret) || caret < 0 || caret > state.value.length) return [];
  let start = caret;
  while (start > 0 && !/\s/u.test(state.value[start - 1])) start -= 1;
  const prefix = state.value.slice(start, caret).toLowerCase();
  if (!/^@[a-z0-9-]*$/u.test(prefix)) return [];
  if (validTokens(state).some((token) => token.start === start && caret <= token.end)) return [];
  return suggestSearchFilters(state.value, start + 1).filter((item) => item.token.startsWith(prefix));
}

/** Return confirmation keys only outside IME composition and reverse tabbing. */
export function mentionConfirmationKey(event: {
  key: string; shiftKey?: boolean; isComposing?: boolean; keyCode?: number; repeat?: boolean;
}): "enter" | "tab" | null {
  if (event.isComposing || event.keyCode === 229 || event.repeat) return null;
  if (event.key === "Enter") return "enter";
  if (event.key === "Tab" && !event.shiftKey) return "tab";
  return null;
}

/** Recognized occurrences awaiting explicit user confirmation. */
export function getDraftSearchTokens(state: SearchQueryState): SearchToken[] {
  const confirmed = validTokens(state);
  return parseSearchQuery(state.value).tokens.filter((token) => !confirmed.some((item) =>
    item.start === token.start && item.end === token.end,
  ));
}
