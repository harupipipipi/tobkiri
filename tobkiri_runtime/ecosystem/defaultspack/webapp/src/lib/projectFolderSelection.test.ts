import assert from "node:assert/strict";
import test from "node:test";
import { ProjectFolderSelection } from "./projectFolderSelection";

test("synchronously excludes duplicate selection and creation during selection", () => {
  const guard = new ProjectFolderSelection();
  const ticket = guard.begin("selection")!;
  assert.equal(guard.begin("selection"), null);
  assert.equal(guard.begin("creation"), null);
  assert.equal(guard.finish(ticket), true);
  assert.notEqual(guard.begin("creation"), null);
  assert.equal(guard.creating, true);
});

test("old picker result and finalizer cannot affect reopened form operation", async () => {
  const guard = new ProjectFolderSelection();
  const old = guard.begin("selection")!;
  let resolve!: (value: string) => void;
  const deferred = new Promise<string>((done) => { resolve = done; });
  let path = "/prior";
  const pending = deferred.then((result) => {
    if (guard.matches(old)) path = result;
  }).finally(() => { guard.finish(old); });
  guard.invalidate();
  const current = guard.begin("selection")!;
  resolve("/obsolete");
  await pending;
  assert.equal(path, "/prior");
  assert.equal(guard.matches(current), true);
  assert.equal(guard.begin("creation"), null);
});

test("choice or profile invalidation discards pending errors and creation continuations", () => {
  const guard = new ProjectFolderSelection();
  for (const operation of ["selection", "creation"] as const) {
    const ticket = guard.begin(operation)!;
    guard.invalidate();
    assert.equal(guard.matches(ticket), false);
    assert.equal(guard.finish(ticket), false);
  }
  assert.notEqual(guard.begin("selection"), null);
});
