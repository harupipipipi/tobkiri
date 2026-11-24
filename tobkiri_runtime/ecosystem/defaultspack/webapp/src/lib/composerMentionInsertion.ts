import {
  activeMentionAtCursor,
  codePointIndexToUtf16Offset,
  extractMentionTokens,
} from "./mentionContract";

const MENTION_TOKEN_CHAR = /[\p{L}\p{M}\p{N}_./:-]/u;

/** Replace the active mention, including its suffix or selected text. */
export function insertAtMentionText(
  input: string,
  cursorPos: number,
  label: string,
  knownValues?: Iterable<string>,
  selectionEnd = cursorPos,
): { value: string; cursor: number } {
  const values = [...(knownValues ?? [])];
  const cursor = Math.min(Math.max(cursorPos, 0), input.length);
  const active = activeMentionAtCursor(input, cursor, values);
  const start = active?.start ?? cursor;
  let end = Math.min(Math.max(cursor, selectionEnd), input.length);
  if (active) {
    const token = extractMentionTokens(input, values)
      .find((candidate) => candidate.start === active.startCodePoint);
    if (token) end = Math.max(end, codePointIndexToUtf16Offset(input, token.end));
    // Human-facing labels may contain spaces, unlike the raw token grammar.
    for (const value of values) {
      const bare = value.trim().replace(/^@/, "");
      if (!bare) continue;
      for (const syntax of [`@${bare}`, `@-${bare}`]) {
        const syntaxEnd = start + syntax.length;
        if (input.slice(start, syntaxEnd).toLocaleLowerCase() !== syntax.toLocaleLowerCase()) continue;
        const following = [...input.slice(syntaxEnd)][0] ?? "";
        const trailingPeriod = following === "."
          && !MENTION_TOKEN_CHAR.test([...input.slice(syntaxEnd + 1)][0] ?? "");
        if (following && !trailingPeriod && MENTION_TOKEN_CHAR.test(following)) continue;
        end = Math.max(end, syntaxEnd);
      }
    }
  }
  const following = [...input.slice(end)][0] ?? "";
  const separator = following && /\p{P}/u.test(following) ? "" : " ";
  const value = `${input.slice(0, start)}@${label}${separator}${input.slice(end)}`;
  return { value, cursor: start + label.length + 1 + separator.length };
}
