export const MODEL_CATALOG_SEARCH_FAILED =
  "モデル一覧を取得できませんでした。再試行してください。";

const LOCAL_SEARCH_UNAVAILABLE =
  "モデル検索の接続を確認できませんでした。Tobkiri Launcherとの接続を確認して再試行してください。";

const STATUS_MESSAGES: Readonly<Record<number, string>> = {
  400: "モデル検索の要求を確認できませんでした。条件を見直して再試行してください。",
  401: "モデル検索を認証できませんでした。Tobkiri Launcherとの接続を確認して再試行してください。",
  403: "モデル一覧を取得する権限を確認できませんでした。Profileと接続を確認して再試行してください。",
  404: "モデル検索を利用できません。Profileと接続を確認して再試行してください。",
  409: "モデル検索の設定が更新されました。画面を更新して再試行してください。",
  429: "モデル検索の呼び出しが制限されています。少し待って再試行してください。",
};
const SERVER_ERROR = "モデル検索を利用できません。しばらくしてから再試行してください。";

/** Return safe, actionable copy without exposing API or provider error details. */
export function modelCatalogSearchError(error: unknown): string {
  if (error instanceof TypeError) return LOCAL_SEARCH_UNAVAILABLE;
  if (!(error instanceof Error)) return MODEL_CATALOG_SEARCH_FAILED;

  // The API client prefixes HTTP failures with this stable label. Only inspect
  // that leading status; the rest may contain backend, URL, or credential data.
  const statusPrefix = /^HTTP ([1-5]\d{2})(?=[\t \r\n]|$)/.exec(error.message);
  if (!statusPrefix) return MODEL_CATALOG_SEARCH_FAILED;

  const status = Number(statusPrefix[1]);
  if (status >= 500) return SERVER_ERROR;
  return STATUS_MESSAGES[status] ?? MODEL_CATALOG_SEARCH_FAILED;
}
