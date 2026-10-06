import assert from "node:assert/strict";
import test from "node:test";
import { mountProjectDirectory, type ProjectMountStatus } from "./projectWorkspaceMount";
import { ProjectFolderSelection } from "./projectFolderSelection";
const selection = { selection_id: "opaque-ticket", display_name: "Folder", expires_in_ms: 60000 };
const pending: ProjectMountStatus = { effect_id: "effect", approval_request_id: "approval", state: "approval_pending" };
const complete: ProjectMountStatus = { ...pending, state: "succeeded", redacted_metadata: { workspace_id: "workspace" } };
function ports(overrides = {}) {
  const calls: string[] = [];
  return {
    calls,
    prepare: async (ticket: string) => { calls.push(`prepare:${ticket}`); return pending; },
    lookup: async () => { calls.push("lookup"); return pending; },
    status: async () => { calls.push("status"); return complete; },
    resume: async () => { calls.push("resume"); return complete; },
    cancel: async () => { calls.push("cancel"); return { ...pending, state: "cancelled" }; },
    approval: async () => ({ request_id: "approval", state: "approved" }),
    openApproval: async () => { calls.push("open"); return true; },
    pause: async () => {},
    assertCurrent: () => {},
    ...overrides,
  };
}
test("mount resumes the immutable Host effect only after exact native approval", async () => {
  const p = ports();
  assert.equal(await mountProjectDirectory(selection, p), "workspace");
  assert.deepEqual(p.calls, ["prepare:opaque-ticket", "resume"]);
});
test("denial cancels the effect without execute or another prepare", async () => {
  const p = ports({ approval: async () => ({ request_id: "approval", state: "denied" }) });
  await assert.rejects(mountProjectDirectory(selection, p), /not approved/);
  assert.deepEqual(p.calls, ["prepare:opaque-ticket", "cancel"]);
});
test("lost prepare reply reconciles correlation without replaying a one-use ticket", async () => {
  const p = ports({ prepare: async () => { p.calls.push("prepare-loss"); throw new Error("lost"); } });
  assert.equal(await mountProjectDirectory(selection, p), "workspace");
  assert.deepEqual(p.calls, ["prepare-loss", "lookup", "resume"]);
});
test("profile or form changed during approval cannot resume into another profile", async () => {
  let current = true;
  const p = ports({
    approval: async () => { current = false; return { request_id: "approval", state: "approved" }; },
    assertCurrent: () => { if (!current) throw new Error("stale form"); },
  });
  await assert.rejects(mountProjectDirectory(selection, p), /stale form/);
  assert.deepEqual(p.calls, ["prepare:opaque-ticket"]);
});
test("stale successful prepare completion does not save the new project", async () => {
  const guard = new ProjectFolderSelection();
  const ticket = guard.begin("creation")!;
  const p = ports({ prepare: async () => { guard.invalidate(); return complete; },
    assertCurrent: () => { if (!guard.matches(ticket)) throw new Error("stale form"); } });
  await assert.rejects(mountProjectDirectory(selection, p), /stale form/);
  assert.deepEqual(p.calls, []);
});
test("wrong native approval id cannot resume", async () => {
  const p = ports({ approval: async () => ({ request_id: "foreign", state: "approved" }) });
  await assert.rejects(mountProjectDirectory(selection, p), /approval changed/);
  assert.deepEqual(p.calls, ["prepare:opaque-ticket"]);
});
test("wrong effect status cannot bind a project", async () => {
  const p = ports({ resume: async () => ({ ...complete, effect_id: "foreign" }) });
  await assert.rejects(mountProjectDirectory(selection, p), /operation changed/);
});
test("successful state requires a Host workspace identity before project persistence", async () => {
  const p = ports({ resume: async () => ({ ...complete, redacted_metadata: {} }) });
  await assert.rejects(mountProjectDirectory(selection, p), /did not return an identity/);
});
