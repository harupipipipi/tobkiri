export type PassiveSteerRefreshStatus = {
  kind: "pending";
  message: string;
};

export type SteerRefreshToken = {
  context: string;
  contextEpoch: number;
  readSequence: number;
};

export type SteerRefreshContextTicket = {
  context: string;
  contextEpoch: number;
};

export function steerRefreshContextKey(
  conversationId: string | null | undefined,
  operationId: string | null | undefined,
): string | null {
  const conversation = String(conversationId ?? "").trim();
  const operation = String(operationId ?? "").trim();
  return conversation && operation ? `${conversation}\u0000${operation}` : null;
}

/**
 * Keeps a late saved-turn list read from changing steering UI after its
 * conversation or durable root turn has changed.
 */
export class SteerRefreshFence {
  private context: string | null = null;
  private contextEpoch = 0;
  private readSequence = 0;

  synchronize(context: string | null): void {
    if (this.context === context) return;
    this.context = context;
    this.contextEpoch += 1;
  }

  begin(context: string | null): SteerRefreshToken | null {
    if (!context || this.context !== context) return null;
    this.readSequence += 1;
    return {
      context,
      contextEpoch: this.contextEpoch,
      readSequence: this.readSequence,
    };
  }

  capture(context: string | null): SteerRefreshContextTicket | null {
    return context && this.context === context
      ? { context, contextEpoch: this.contextEpoch }
      : null;
  }

  matches(ticket: SteerRefreshContextTicket | null): boolean {
    if (!ticket) return false;
    return this.context === ticket.context && this.contextEpoch === ticket.contextEpoch;
  }

  isCurrent(token: SteerRefreshToken): boolean {
    return this.context === token.context
      && this.contextEpoch === token.contextEpoch
      && this.readSequence === token.readSequence;
  }
}

export function passiveSteerRefreshStatus(
  reason: "unsettled" | "unavailable",
): PassiveSteerRefreshStatus {
  return {
    kind: "pending",
    message: reason === "unsettled"
      ? "送信状況を確認しています。"
      : "接続を待っています。送信結果はまだ確認できていません。",
  };
}
