/** A folder choice is a one-use Host ticket, never a browser-provided path. */
export type ProjectDirectorySelection = {
  selection_id: string;
  display_name: string;
  expires_in_ms: number;
};
export type ProjectMountStatus = {
  effect_id: string;
  approval_request_id: string | null;
  state: string;
  redacted_metadata?: { workspace_id?: string };
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
export async function mountProjectDirectory(selection: ProjectDirectorySelection, ports: Ports): Promise<string> {
  ports.assertCurrent();
  const correlation = crypto.randomUUID();
  let status: ProjectMountStatus;
  try {
    status = await ports.prepare(selection.selection_id, correlation);
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
      return workspace;
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
