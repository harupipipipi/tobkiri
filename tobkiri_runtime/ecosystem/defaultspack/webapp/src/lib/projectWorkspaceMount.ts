import type { CodingWorkspaceRecord } from "./api";
/** A folder choice is a one-use Host ticket, never a browser-provided path. */
export type ProjectDirectorySelection = {
  selection_id: string;
  display_name: string;
  expires_in_ms: number;
};
export type ProjectDirectorySelectionSet = {
  selections: ProjectDirectorySelection[];
  primary_selection_id: string;
};
export type ProjectWorkspaceSet = {
  workspace: CodingWorkspaceRecord;
  workspaces: CodingWorkspaceRecord[];
};
export type ProjectMountStatus = {
  effect_id: string;
  approval_request_id: string | null;
  state: string;
  redacted_metadata?: { workspace_id?: string; workspace_ids?: string[] };
};
type Ports = {
  prepare: (selection: string, correlation: string) => Promise<ProjectMountStatus>;
  lookup: (correlation: string) => Promise<ProjectMountStatus>;
  status: (effect: string) => Promise<ProjectMountStatus>;
  resume: (effect: string) => Promise<ProjectMountStatus>;
  cancel: (effect: string) => Promise<ProjectMountStatus>;
  approval: (id: string) => Promise<{ request_id: string; state: string }>;
  openApproval: (id: string) => Promise<boolean>;
  pause: () => Promise<void>;
  assertCurrent: () => void;
};

/** Complete exactly one immutable Host effect; response loss never replays prepare. */
async function completeProjectMount(ports: Omit<Ports, "prepare"> & {prepare: (correlation: string) => Promise<ProjectMountStatus>}, expectedCount: number): Promise<{workspaceId: string; workspaceIds: string[]}> {
  ports.assertCurrent();
  const correlation = crypto.randomUUID();
  let status: ProjectMountStatus;
  try {
    status = await ports.prepare(correlation);
  } catch {
    ports.assertCurrent();
    status = await ports.lookup(correlation);
  }
  ports.assertCurrent();
  const effect = status.effect_id;
  if (!effect) throw new Error("Project workspace result is unknown. Check Tobkiri Launcher approvals before choosing another folder.");
  let opened = false;
  let resumed = false;
  for (let attempt = 0; attempt < 300; attempt += 1) {
    ports.assertCurrent();
    if (status.effect_id !== effect) throw new Error("Project workspace operation changed.");
    if (status.state === "succeeded") {
      const workspace = status.redacted_metadata?.workspace_id;
      if (!workspace) throw new Error("Project workspace did not return an identity.");
      const identities = status.redacted_metadata?.workspace_ids ?? (expectedCount === 1 ? [workspace] : []);
      if (identities.length !== expectedCount || new Set(identities).size !== expectedCount ||
          identities.some((id) => typeof id !== "string" || !id.trim()) || !identities.includes(workspace)) {
        throw new Error("Project workspace did not confirm every selected folder.");
      }
      return { workspaceId: workspace, workspaceIds: [...identities] };
    }
    if (["failed", "ambiguous", "stale", "cancelled"].includes(status.state)) {
      throw new Error(`Project workspace is incomplete (${status.state}). Choose the folder again after inspecting operation ${effect}.`);
    }
    if (!["prepared", "approval_pending", "approved", "claimed", "dispatched"].includes(status.state)) {
      throw new Error("Project workspace result is unknown. Check Tobkiri Launcher approvals.");
    }
    if (!resumed && ["prepared", "approval_pending", "approved"].includes(status.state)) {
      const approvalId = status.approval_request_id;
      if (!approvalId) throw new Error("Project workspace approval is unavailable.");
      const approval = await ports.approval(approvalId);
      ports.assertCurrent();
      if (approval.request_id !== approvalId) throw new Error("Project workspace approval changed.");
      if (approval.state === "approved") {
        resumed = true;
        status = await ports.resume(effect);
        continue;
      }
      if (["denied", "expired", "cancelled", "rejected"].includes(approval.state)) {
        const cancelled = await ports.cancel(effect);
        ports.assertCurrent();
        if (cancelled.effect_id !== effect || cancelled.state !== "cancelled") throw new Error("Project workspace cancellation is unconfirmed.");
        throw new Error("Project workspace was not approved. Your project draft is preserved.");
      }
      if (!opened) {
        opened = await ports.openApproval(approvalId);
        ports.assertCurrent();
        if (!opened) throw new Error("Open Tobkiri Launcher approvals to approve the project folder.");
      }
    }
    await ports.pause();
    ports.assertCurrent();
    status = await ports.status(effect);
  }
  throw new Error(`Project workspace is awaiting confirmation. Check operation ${effect} in Tobkiri Launcher.`);
}

/** Preserve the legacy one-folder API while completing the same Host effect. */
export async function mountProjectDirectory(selection: ProjectDirectorySelection, ports: Ports): Promise<string> {
  const result = await completeProjectMount({...ports, prepare: (correlation) => ports.prepare(selection.selection_id, correlation)}, 1);
  return result.workspaceId;
}

/** Mount the complete immutable folder set after its aggregate Host approval. */
export async function mountProjectDirectories(selection: ProjectDirectorySelectionSet, ports: Omit<Ports, "prepare"> & {
  prepare: (request: {selection_ids: string[]; primary_selection_id: string}, correlation: string) => Promise<ProjectMountStatus>;
}): Promise<{workspaceId: string; workspaceIds: string[]}> {
  const ids = selection.selections.map((item) => item.selection_id);
  if (!ids.length || ids.length > 32 || ids.some((id) => typeof id !== "string" || !id.trim()) ||
      new Set(ids).size !== ids.length || !ids.includes(selection.primary_selection_id)) {
    throw new Error("Project folder selection is invalid.");
  }
  const request = { selection_ids: [...ids], primary_selection_id: selection.primary_selection_id };
  return completeProjectMount({...ports, prepare: (correlation) => ports.prepare(request, correlation)}, ids.length);
}
