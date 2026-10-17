import type { CommandStateSnapshot } from "./api";

export type CommandStateRevisionMap = Record<string, number>;

export type AppliedCommandState = {
  values: Record<string, Record<string, unknown>>;
  revisions: CommandStateRevisionMap;
  appliedPaths: string[];
};

function statePath(stateRef: string): { section: string; field: string } | null {
  const normalized = String(stateRef ?? "").trim();
  const separator = normalized.indexOf(":");
  const path = separator >= 0 ? normalized.slice(separator + 1) : normalized;
  const dot = path.indexOf(".");
  if (dot <= 0 || dot >= path.length - 1) return null;
  const section = path.slice(0, dot).trim();
  const field = path.slice(dot + 1).trim();
  return section && field ? { section, field } : null;
}

export function applyCommandStateSnapshots(
  currentValues: Record<string, Record<string, unknown>>,
  currentRevisions: CommandStateRevisionMap,
  snapshots: CommandStateSnapshot[] | undefined,
): AppliedCommandState {
  let values = currentValues;
  const revisions = { ...currentRevisions };
  const appliedPaths: string[] = [];

  for (const snapshot of snapshots ?? []) {
    const stateRef = String(snapshot?.state_ref ?? "").trim();
    const revision = Number(snapshot?.revision);
    if (!stateRef || !Number.isInteger(revision) || revision < 0) continue;
    if ((revisions[stateRef] ?? -1) > revision) continue;
    const path = statePath(stateRef);
    if (!path) continue;

    if (values === currentValues) values = { ...currentValues };
    values[path.section] = {
      ...(values[path.section] ?? {}),
      [path.field]: snapshot.value,
    };
    revisions[stateRef] = revision;
    appliedPaths.push(`${path.section}.${path.field}`);
  }

  return { values, revisions, appliedPaths };
}

export function createCommandInvocationId(prefix = "command"): string {
  const randomUuid = globalThis.crypto?.randomUUID?.();
  if (randomUuid) return `${prefix}-${randomUuid}`;
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
