import type { ComposerCommandItem } from "./api";

/** The only Command Protocol commands owned by the V4 interactive adapter. */
export type HighRiskCommandRef = "terminal" | "commit" | "push" | "patch" | "restore";

const HIGH_RISK_COMMAND_REFS = new Set<HighRiskCommandRef>([
  "terminal",
  "commit",
  "push",
  "patch",
  "restore",
]);

function normalizedCandidates(command: ComposerCommandItem): string[] {
  return [command.canonical_id, command.id, command.name]
    .filter((value): value is string => typeof value === "string")
    .map((value) => value.trim().toLowerCase())
    .flatMap((value) => [value, value.replace(/^defaultspack:/, "")]);
}

/**
 * Return the fixed V4 adapter command ref, or null for every other command.
 *
 * The catalog is presentation data, not an authority decision: the Host
 * adapter repeats this finite allowlist before it can create an effect.
 */
export function highRiskCommandRef(command: ComposerCommandItem): HighRiskCommandRef | null {
  for (const candidate of normalizedCandidates(command)) {
    if (HIGH_RISK_COMMAND_REFS.has(candidate as HighRiskCommandRef)) {
      return candidate as HighRiskCommandRef;
    }
  }
  return null;
}

/**
 * Normalize only the request shape expected by the fixed Host providers.
 *
 * Raw arguments are supplied only to prepare. Resume/status/cancel receive
 * an opaque invocation id and cannot recreate or change this request.
 */
export function highRiskPrepareArguments(
  commandRef: HighRiskCommandRef,
  args: Record<string, unknown>,
  options: {
    workspaceId: string | null;
    currentBranch?: string | null;
  },
): Record<string, unknown> {
  if (commandRef === "terminal") {
    return {
      command: args.command ?? args.cmd,
      cwd: args.cwd ?? ".",
      env: args.env ?? {},
      timeout: args.timeout ?? 30,
    };
  }

  const workspaceId = options.workspaceId?.trim();
  if (!workspaceId) {
    throw new Error("作業空間を選択してから高リスクのコマンドを実行してください。");
  }
  // The authenticated Host context supplies the profile binding. The browser
  // must not select a default profile or carry profile authority in its input.
  const common = { workspace_id: workspaceId };

  switch (commandRef) {
    case "commit":
      return { ...common, message: args.message };
    case "push":
      return {
        ...common,
        remote: args.remote ?? "origin",
        branch: args.branch ?? options.currentBranch ?? "",
      };
    case "patch":
      return { ...common, patch: args.patch };
    case "restore":
      return {
        ...common,
        source: args.source ?? "HEAD",
        paths: restorePaths(args.paths),
      };
  }
}

/** Claim one non-blocking client dispatch attempt for an opaque invocation. */
export function beginHighRiskAttempt(inFlight: Set<string>, invocationId: string): boolean {
  if (inFlight.has(invocationId)) return false;
  inFlight.add(invocationId);
  return true;
}

/** Release a failed transport attempt so an idempotent Host request can retry. */
export function releaseHighRiskAttempt(inFlight: Set<string>, invocationId: string): void {
  inFlight.delete(invocationId);
}

function restorePaths(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => String(item));
  if (typeof value !== "string") return [];
  // A slash command has no shell parser. Preserve the established contract's
  // whitespace-separated path syntax; the Host validates every resulting path.
  return value.trim().split(/\s+/).filter(Boolean);
}
