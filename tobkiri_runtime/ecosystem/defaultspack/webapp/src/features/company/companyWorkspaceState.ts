export type CompanyMutationPhase = "idle" | "pending" | "committed" | "rejected";

export type CompanyMutationReceipt<T = unknown> = {
  operationId: string;
  phase: "committed" | "rejected";
  value?: T;
  error?: string;
  retryable?: boolean;
  ambiguous?: boolean;
  revision?: number;
};

export type CompanyActionState = {
  phase: CompanyMutationPhase;
  operationId?: string;
  message?: string;
  retryable?: boolean;
  ambiguous?: boolean;
  updatedAt?: number;
};

export type CompanyLoadToken = {
  generation: number;
  companyId: string | null;
  channelId: string | null;
  signal: AbortSignal;
};

export type SettingsDraftState = {
  baseline: Record<string, unknown>;
  draft: Record<string, unknown>;
  dirty: boolean;
  conflict: boolean;
};

function randomOperationSuffix(): string {
  const randomUuid = globalThis.crypto?.randomUUID?.();
  if (randomUuid) return randomUuid.replace(/-/g, "");
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
}

export function createCompanyOperationId(action: string): string {
  const prefix = action.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "mutation";
  return `${prefix}-${randomOperationSuffix().slice(0, 32)}`;
}

export function pendingCompanyAction(operationId: string): CompanyActionState {
  return { phase: "pending", operationId, message: "Saving…" };
}

export function committedCompanyAction(operationId: string, now = Date.now()): CompanyActionState {
  return {
    phase: "committed",
    operationId,
    message: "Saved",
    updatedAt: now,
  };
}

export function rejectedCompanyAction(
  operationId: string,
  error: unknown,
): CompanyActionState {
  const normalized = safeCompanyMutationError(error);
  return {
    phase: "rejected",
    operationId,
    message: normalized.message,
    retryable: normalized.retryable,
    ambiguous: normalized.ambiguous,
    updatedAt: Date.now(),
  };
}

export function safeCompanyMutationError(error: unknown): {
  message: string;
  retryable: boolean;
  ambiguous: boolean;
} {
  const detail = error instanceof Error ? error.message : String(error ?? "");
  const normalized = detail.toLowerCase();
  if (normalized.includes("revision") || normalized.includes("conflict") || normalized.includes("stale")) {
    return {
      message: "Company data changed before this action was saved. Refresh and retry.",
      retryable: true,
      ambiguous: false,
    };
  }
  if (normalized.includes("approval") || normalized.includes("authority") || normalized.includes("denied")) {
    return {
      message: "This action was not authorized. Review approval and try again.",
      retryable: true,
      ambiguous: false,
    };
  }
  if (
    normalized.includes("network")
    || normalized.includes("offline")
    || normalized.includes("timeout")
    || normalized.includes("failed to fetch")
  ) {
    return {
      message: "The outcome could not be confirmed. Your draft was kept; retry to reconcile safely.",
      retryable: true,
      ambiguous: true,
    };
  }
  return {
    message: "The action was not saved. Your draft and context were kept.",
    retryable: true,
    ambiguous: false,
  };
}

export class CompanyLoadGate {
  private generation = 0;

  private activeController: AbortController | null = null;

  begin(companyId?: string | null, channelId?: string | null): CompanyLoadToken {
    this.activeController?.abort();
    this.activeController = new AbortController();
    this.generation += 1;
    return {
      generation: this.generation,
      companyId: companyId ?? null,
      channelId: channelId ?? null,
      signal: this.activeController.signal,
    };
  }

  isCurrent(token: CompanyLoadToken): boolean {
    return token.generation === this.generation && !token.signal.aborted;
  }

  cancel(): void {
    this.activeController?.abort();
    this.activeController = null;
    this.generation += 1;
  }
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

export function companySettingsEqual(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): boolean {
  return canonicalJson(left) === canonicalJson(right);
}

export function updateSettingsDraft(
  current: SettingsDraftState,
  incoming: Record<string, unknown>,
): SettingsDraftState {
  if (!current.dirty) {
    if (companySettingsEqual(current.baseline, incoming)) return current;
    return { baseline: incoming, draft: incoming, dirty: false, conflict: false };
  }
  if (companySettingsEqual(current.baseline, incoming)) return current;
  return { ...current, conflict: true };
}

export function editSettingsDraft(
  current: SettingsDraftState,
  key: string,
  value: unknown,
): SettingsDraftState {
  const draft = { ...current.draft, [key]: value };
  return {
    ...current,
    draft,
    dirty: !companySettingsEqual(current.baseline, draft),
  };
}

export function discardSettingsDraft(
  current: SettingsDraftState,
  incoming = current.baseline,
): SettingsDraftState {
  return { baseline: incoming, draft: incoming, dirty: false, conflict: false };
}

export function shouldRunCompanyPoll({
  visible,
  online,
  editing,
  mutationPending,
}: {
  visible: boolean;
  online: boolean;
  editing: boolean;
  mutationPending: boolean;
}): boolean {
  return visible && online && !editing && !mutationPending;
}
