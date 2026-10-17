// Receipts are cumulative in DeepThink progress; render them without adding twice.
export function formatUsage(value = {}, prompt, completion) {
  const tokens = value?.tokens ?? value ?? {};
  const count = (name, fallback) => {
    const number = tokens[name] ?? fallback;
    return Number.isSafeInteger(number) && number >= 0 ? number.toLocaleString("ja-JP") : "不明";
  };
  const cost = value?.cost_known === false ? "不明" :
    (typeof value?.cost_usd === "string" ? `$${value.cost_usd}` : "不明");
  const coverage = value?.tokens_complete === false ? "（確認済み分・未確定の使用量あり）" : "";
  return `入力 ${count("input_tokens", prompt)} / 出力 ${count("output_tokens", completion)} / 合計 ${count("total_tokens")} / キャッシュ ${count("cached_input_tokens")} / 推論 ${count("reasoning_tokens")} tokens・費用 ${cost}${coverage}`;
}

export function parseEventText(text) {
  if (typeof text !== "string") return {};
  try {
    const value = JSON.parse(text);
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch { return {}; }
}
