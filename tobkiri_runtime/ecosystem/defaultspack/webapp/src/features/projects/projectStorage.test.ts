import test from "node:test";
import assert from "node:assert/strict";
import { projectFromOwner, projectFromStorageItem, projectTaskContext, projectToOwner, type ProjectInfo } from "./projectStorage";

const project: ProjectInfo = {
  id: "group-one", title: "One", workspaceId: "one", workspaceLabel: "One", workspaceRoot: "/repo/one", rumiDataPath: null,
  workspaceBindings: [
    { workspaceId: "one", workspaceLabel: "One", workspaceRoot: "/repo/one" },
    { workspaceId: "two", workspaceLabel: "Two", workspaceRoot: "/repo/two" },
  ],
};

test("canonical serialization and reload preserve every binding and scalar primary", () => {
  const owner = projectToOwner(project);
  assert.equal(owner.workspace_bindings?.length, 2);
  assert.deepEqual(projectFromOwner(JSON.parse(JSON.stringify(owner))), project);
  assert.deepEqual(projectFromStorageItem(owner), project);
  assert.deepEqual(projectFromStorageItem(project), project);
  assert.deepEqual(projectTaskContext(project)?.workspaceBindings, project.workspaceBindings);
});

test("legacy six-field records stay legacy without adding bindings", () => {
  const legacy = { ...project };
  delete legacy.workspaceBindings;
  assert.equal(Object.prototype.hasOwnProperty.call(projectToOwner(legacy), "workspace_bindings"), false);
  assert.deepEqual(projectFromOwner(projectToOwner(legacy)), legacy);
});

test("invalid arrays and primary mismatches fail without dropping extra bindings", () => {
  const variants: unknown[] = [null, [], [...project.workspaceBindings!, project.workspaceBindings![0]],
    [{ ...project.workspaceBindings![0], trusted: true }],
    [{ ...project.workspaceBindings![0], workspaceRoot: "relative" }],
    [{ ...project.workspaceBindings![0], workspaceRoot: "/repo/../one" }],
    [{ ...project.workspaceBindings![0], workspaceId: null }],
    Array.from({ length: 33 }, () => project.workspaceBindings![0]),
  ];
  for (const workspaceBindings of variants) {
    assert.equal(projectFromStorageItem({ ...project, workspaceBindings }), null);
  }
  assert.equal(projectFromStorageItem({ ...project, workspaceLabel: "Mismatch" }), null);
  assert.throws(() => projectToOwner({ ...project, workspaceId: "unknown" }), /primary/);
});

test("Project storage accepts absolute Windows metadata and rejects incomplete UNC", () => {
  for (const workspaceRoot of ["C:\\repos\\one", "C:/repos/one", "\\\\server\\share\\one", "/repos/one"]) {
    const candidate = { ...project, workspaceRoot, workspaceBindings: [{ ...project.workspaceBindings![0], workspaceRoot }] };
    assert.deepEqual(projectFromOwner(projectToOwner(candidate)), candidate);
  }
  for (const workspaceRoot of ["C:relative", "\\\\server", "\\\\server\\", "\\\\server\\share\\..\\one", "C:\\repo\0one"]) {
    assert.equal(projectFromStorageItem({ ...project, workspaceRoot, workspaceBindings: [{ ...project.workspaceBindings![0], workspaceRoot }] }), null);
  }
});
