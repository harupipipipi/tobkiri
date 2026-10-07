import test from "node:test";
import assert from "node:assert/strict";
import { isProjectStateSnapshot } from "./api";

const primary = { workspace_id: "one", workspace_label: "One", workspace_root: "/repo/one" };
const project = { id: "group-one", title: "One", ...primary, rumi_data_path: null };
const snapshot = (record: unknown) => ({ namespace: "defaultspack.projects.v1", revision: 1, projects: [record] });

test("Project API accepts legacy and exact multiple-workspace snapshots", () => {
  assert.equal(isProjectStateSnapshot(snapshot(project)), true);
  assert.equal(isProjectStateSnapshot(snapshot({ ...project, workspace_bindings: [primary, { workspace_id: "two", workspace_label: "Two", workspace_root: "C:\\repos\\two" }] })), true);
});

test("Project API rejects malformed bindings, duplicates and mismatched primary", () => {
  for (const workspace_bindings of [null, [], [primary, primary], [{ ...primary, approved: true }],
    [{ ...primary, workspace_root: "relative" }], [{ ...primary, workspace_root: "/repo/../one" }],
    [{ ...primary, workspace_label: null }], Array.from({ length: 33 }, () => primary)]) {
    assert.equal(isProjectStateSnapshot(snapshot({ ...project, workspace_bindings })), false);
  }
  assert.equal(isProjectStateSnapshot(snapshot({ ...project, workspace_label: "Mismatch", workspace_bindings: [primary] })), false);
  assert.equal(isProjectStateSnapshot(snapshot({ ...project, workspace_bindings: [primary], extra: true })), false);
});

test("Project metadata accepts absolute Windows roots and requires a UNC share", () => {
  for (const workspace_root of ["C:\\repos\\one", "C:/repos/one", "\\\\server\\share\\one", "/repos/one"]) {
    const binding = { ...primary, workspace_root };
    assert.equal(isProjectStateSnapshot(snapshot({ ...project, ...binding, workspace_bindings: [binding] })), true);
  }
  for (const workspace_root of ["C:relative", "\\\\server", "\\\\server\\", "\\\\server\\share\\..\\one", "C:\\repo\0one"]) {
    const binding = { ...primary, workspace_root };
    assert.equal(isProjectStateSnapshot(snapshot({ ...project, ...binding, workspace_bindings: [binding] })), false);
  }
});
