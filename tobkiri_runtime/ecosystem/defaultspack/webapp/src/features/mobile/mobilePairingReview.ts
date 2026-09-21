export type PairingSettlement = "approved" | "rejected" | "expired" | "revoked" | "already-settled";
export type PairingDecision = "approve" | "reject" | "cancel";

const SETTLED = new Set(["approved", "rejected", "expired", "revoked"]);

export function pairingSettlement(status: unknown): PairingSettlement | null {
  const value = String(status ?? "").trim().toLowerCase();
  return SETTLED.has(value) ? value as PairingSettlement : null;
}

export function pairingDecisionReason(decision: PairingDecision): string | undefined {
  if (decision === "reject") return "rejected by desktop reviewer";
  if (decision === "cancel") return "pairing cancelled by desktop reviewer";
  return undefined;
}

export function pairingErrorCode(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error ?? "");
  if (/expired/i.test(text)) return "expired";
  if (/revoked/i.test(text)) return "revoked";
  if (/not[_ -]?(claimed|pending)|already|settled/i.test(text)) return "already-settled";
  return "failed";
}

export class PairingRequestGate {
  private generation = 0;
  private inFlight = false;

  begin(): number | null {
    if (this.inFlight) return null;
    this.inFlight = true;
    this.generation += 1;
    return this.generation;
  }

  finish(generation: number): boolean {
    if (!this.inFlight || generation !== this.generation) return false;
    this.inFlight = false;
    return true;
  }

  invalidate(): void {
    this.generation += 1;
    this.inFlight = false;
  }

  get busy(): boolean { return this.inFlight; }
}
