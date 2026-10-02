export type SearchAction = "smart" | "answer" | "google" | "open";

export type SearchRequest = Readonly<{
  input: string;
  model: string;
  action: SearchAction;
}>;

export const SEARCH_ACTIONS: ReadonlyArray<{
  id: SearchAction;
  title: string;
  subtitle: string;
}> = [
  { id: "google", title: "Googleで検索", subtitle: "Googleの検索結果を開きます" },
  { id: "answer", title: "AIに質問", subtitle: "Defaultsと共有するモデルの知識で回答します" },
  { id: "open", title: "サイト・URLを開く", subtitle: "URLは確認して開き、サイト名はGoogleで探します" },
];

/** Keep retries attached to the submitted input and model, even after editing. */
export function captureSearchRequest(
  input: string,
  model: string,
  action: SearchAction,
): SearchRequest | null {
  return input.trim() ? Object.freeze({ input, model, action }) : null;
}

export function requestErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

/** IME composition must finish before Enter or arrows can submit/select. */
export function searchActionIndexForKey(
  key: string,
  current: number,
  composing: boolean,
): number | null {
  if (composing) return null;
  if (key === "ArrowDown") return (current + 1) % SEARCH_ACTIONS.length;
  if (key === "ArrowUp") return (current - 1 + SEARCH_ACTIONS.length) % SEARCH_ACTIONS.length;
  return null;
}
