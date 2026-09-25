import type { SearchHomeModel } from "./api";

export type SearchAction = "smart" | "answer" | "google" | "open";

export const searchHomeCopy = {
  productName: "Tobkiri Search Home",
  heading: "何を探しましょう？",
  model: {
    defaultLabel: "おまかせモデル",
    defaultStatus: "用途に合わせて自動選択",
    pickerTitle: "モデルを選ぶ",
    availableCount: (count: number) => `${count}件のモデル`,
    filterLabel: "モデルを絞り込む",
    filterPlaceholder: "名前や提供元で検索",
    listLabel: "利用できるモデル",
    defaultDescription: "Tobkiriの設定に合わせて選びます",
    automaticProvider: "自動選択",
    noMatches: "条件に合うモデルがありません",
    unknownLabel: "名称未設定のモデル",
    unknownProvider: "提供元不明",
    selectedFallback: "選択したモデル",
    technicalDetails: "技術情報",
    profileId: "プロファイルID",
    qualifiedModelId: "完全なモデルID",
    modelId: "モデルID",
  },
  search: {
    attachLabel: "ファイルを添付",
    inputLabel: "検索語またはURLを入力",
    placeholder: "検索ワードを入力...",
    submit: "検索",
    working: "処理中",
    actionsLabel: "検索方法",
    removeFile: "添付ファイルを削除",
  },
  answer: {
    transportError: "回答を取得できませんでした。接続を確認して、もう一度お試しください。",
    inProgressTitle: "回答を作成しています",
    inProgressDetail: "送信済みです。完了するまで重複送信はできません。",
    requestFailedTitle: "回答を取得できませんでした",
    retry: "もう一度試す",
    successLabel: "AIからの回答",
    statusLabel: "回答の状態",
    toolsUnavailable: "この回答では追加ツールを利用できませんでした。",
    originalQuery: "質問",
    toolsUsed: "利用したツール",
    toolCount: (count: number) => `${count}件`,
    noTools: "利用なし",
    openConversation: "会話を開いて続ける",
    dismiss: "閉じる",
    privacyNote: "回答はこの画面のメモリだけに保持されます。会話リンクがある場合は、再読み込み後も会話から再開できます。",
    malformed: "回答データの形式を確認できませんでした。",
    rejected: "回答リクエストを受け付けられませんでした。",
    unknownStatus: "回答の状態を確認できませんでした。",
    empty: "回答文がない状態で処理が完了しました。",
    partial: "中断前までの回答を表示しています。",
    ready: "回答を表示します。",
  },
  review: {
    eyebrow: "移動前の確認",
    selected: "選択した移動先",
    blocked: "ブロックされた移動先",
    guidance: "自動では移動しません。ホストと警告を確認してから開いてください。",
    blockedHost: "ブロック",
    destination: (protocol: "HTTPS" | "HTTP") => `${protocol} の移動先`,
    warningLabel: "移動先の注意",
    blockedMessage: "この移動先は開けません。",
    candidatesLabel: "移動先候補",
    candidate: (index: number) => `候補 ${index}`,
    open: "この移動先を開く",
    copyUrl: "URLをコピー",
    copyBlocked: "ブロック詳細をコピー",
    openGoogle: "Google検索を開く",
    cancel: "キャンセル",
    blockedClipboard: (code: string, message: string) => `ブロックされた移動先: ${code}。${message}`,
    redirected: "リダイレクト後のURLです",
    possibleLogin: "ログイン画面の可能性があります",
    possiblePaywall: "有料記事の可能性があります",
    possibleMissing: "見つからないページの可能性があります",
    possibleAds: "広告が多い可能性があります",
  },
} as const;

export const SEARCH_HOME_ACTIONS: ReadonlyArray<{
  id: SearchAction;
  title: string;
  subtitle: (query: string) => string;
}> = [
  {
    id: "smart",
    title: "おすすめで探す",
    subtitle: (query) => `質問には回答し、サイトは候補を確認します: 「${query}」`,
  },
  {
    id: "answer",
    title: "AIに質問",
    subtitle: (query) => `必要に応じて検索しながら回答します: 「${query}」`,
  },
  {
    id: "google",
    title: "Googleで検索",
    subtitle: (query) => `Google検索の移動先を確認します: 「${query}」`,
  },
  {
    id: "open",
    title: "候補サイトを確認",
    subtitle: (query) => `関連するサイトを比較して移動先を選びます: 「${query}」`,
  },
];

export function searchHomeModelId(model: SearchHomeModel): string {
  return model.profile_id || model.qualified_model_id || "";
}

function humanizeIdentifier(value: string | undefined): string {
  const segments = String(value ?? "").split(/[/:]/).filter(Boolean);
  const finalSegment = segments[segments.length - 1] ?? "";
  return finalSegment.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
}

function normalizeLegacyProductName(value: string): string {
  return value.replace(/\bRumi\b/g, "Tobkiri");
}

export function searchHomeModelLabel(model: SearchHomeModel): string {
  const label = model.label
    || model.display_name
    || humanizeIdentifier(model.model_id)
    || searchHomeCopy.model.unknownLabel;
  return normalizeLegacyProductName(label);
}

export function searchHomeProviderLabel(model: SearchHomeModel): string {
  const label = model.provider_display_name
    || humanizeIdentifier(model.provider_id)
    || searchHomeCopy.model.unknownProvider;
  return normalizeLegacyProductName(label);
}

function hasMetadataFlag(model: SearchHomeModel, key: string): boolean {
  return Boolean(model.metadata && model.metadata[key]);
}

export function searchHomeModelStatus(model: SearchHomeModel): string {
  const availability = model.availability ?? {};
  if (model.configured || availability.configured || availability.active || availability.available) {
    return "利用可能";
  }
  if (hasMetadataFlag(model, "settings_only")) {
    return "設定から選択可能";
  }
  if (model.requires_api_key) {
    return "接続設定が必要";
  }
  const status = String(availability.status ?? "").toLowerCase();
  if (["unavailable", "disabled", "offline"].includes(status)) {
    return "現在利用できません";
  }
  if (["error", "unknown"].includes(status)) {
    return "状態を確認してください";
  }
  return "利用状況を確認";
}

export function searchHomeModelLabelForReference(
  models: readonly SearchHomeModel[],
  reference: string,
): string {
  if (!reference) {
    return searchHomeCopy.model.defaultLabel;
  }
  const model = models.find((candidate) => (
    searchHomeModelId(candidate) === reference
    || candidate.qualified_model_id === reference
    || candidate.model_id === reference
  ));
  return model ? searchHomeModelLabel(model) : searchHomeCopy.model.selectedFallback;
}
