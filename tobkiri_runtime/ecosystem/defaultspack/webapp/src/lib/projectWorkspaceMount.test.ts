import assert from "node:assert/strict";
import test from "node:test";
import { mountProjectDirectories, mountProjectDirectory, type ProjectMountStatus } from "./projectWorkspaceMount";
import { ProjectFolderSelection } from "./projectFolderSelection";
const selection = { selection_id: "opaque-ticket", display_name: "Folder", expires_in_ms: 60000 };
const pending: ProjectMountStatus = { effect_id: "effect", approval_request_id: "approval", state: "approval_pending" };
const complete: ProjectMountStatus = { ...pending, state: "succeeded", redacted_metadata: { workspace_id: "workspace" } };
function ports<T extends object = {}>(overrides: T = {} as T) {
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

const set = {selections:[selection,{...selection,selection_id:"second"}],primary_selection_id:"second"};
test("all-root effect requires all distinct identities and explicit primary", async () => {
  const p = ports({prepare: async (request: unknown) => {assert.deepEqual(request,{selection_ids:["opaque-ticket","second"],primary_selection_id:"second"});return pending;}, resume: async () => ({...complete,redacted_metadata:{workspace_id:"two",workspace_ids:["one","two"]}})});
  assert.deepEqual(await mountProjectDirectories(set,p),{workspaceId:"two",workspaceIds:["one","two"]});
});
test("partial or duplicated successful root sets cannot persist a project", async () => {
  for (const ids of [["one"],["one","one"]]) {
    const p = ports({prepare:async()=>pending,resume:async()=>({...complete,redacted_metadata:{workspace_id:"one",workspace_ids:ids}})});
    await assert.rejects(mountProjectDirectories(set,p), /every selected folder/);
  }
});
test("invalid primary fails before preparing any root", async () => {
  const p = ports({prepare:async()=>{throw new Error("must not prepare");}});
  await assert.rejects(mountProjectDirectories({...set,primary_selection_id:"outside"},p),/selection is invalid/);
});
