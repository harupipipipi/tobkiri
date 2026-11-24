export function approvalRequestExpired(expiresAtSeconds: number, nowMs: number): boolean {
  return Number.isFinite(expiresAtSeconds) && expiresAtSeconds > 0
    && nowMs >= expiresAtSeconds * 1000;
}

export function approvalUnavailableMessage(state: string): string {
  switch (state) {
    case "expired": return "このリクエストは期限切れです。";
    case "stale": return "このリクエストは古くなりました。状態を再読み込みしてください。";
    case "denied": return "このリクエストはすでに拒否されています。";
    case "approved": return "このリクエストはすでに承認されています。";
    case "cancelled": return "このリクエストは取り消されています。";
    case "failed": return "このリクエストは失敗しました。状態を再読み込みしてください。";
    default: return "このリクエストは処理できません。状態を再読み込みしてください。";
  }
}
