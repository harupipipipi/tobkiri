import test from "node:test";
import assert from "node:assert/strict";
import type { ComposerCommandItem } from "./api";
import { parseSlashCommandInput } from "../App";
import { completeLocalTaskPetSubmission, consumeLocalTaskPetInput, executeLocalTaskPetCommand, isLocalTaskPetCommand } from "./taskPetCommand";

const pet: ComposerCommandItem = {
  id: "pet", name: "pet", label: "Pet", category: "chat",
  visibility: "default", risk: "low", modes: ["chat", "coding", "agent"],
  execution: { type: "frontend", action: "open_task_pet" },
};

test("ordinary and resolved pet commands parse locally while escaped input stays literal", () => {
  for (const command of [pet, { ...pet, canonical_id: "defaultspack:pet" }]) {
    assert.equal(parseSlashCommandInput(" /pet ", [command])?.command, command);
    assert.equal(isLocalTaskPetCommand(command), true);
    assert.equal(parseSlashCommandInput("//pet", [command]), null);
    assert.equal(parseSlashCommandInput("/pet", [command], { enabled: false }), null);
  }
  assert.equal(isLocalTaskPetCommand({ ...pet, canonical_id: "foreign:pet" }), false);
  assert.equal(isLocalTaskPetCommand({ ...pet, id: "terminal", risk: "high" }), false);
  assert.equal(isLocalTaskPetCommand({ ...pet, execution: { type: "frontend", action: "request_terminal_approval" } }), false);
});

test("pet opening starts synchronously and performs no backend or chat dispatch", async () => {
  const originalFetch = globalThis.fetch;
  let requests = 0;
  let opens = 0;
  const errors: string[] = [];
  globalThis.fetch = async () => { requests += 1; throw new Error("unexpected backend/chat request"); };
  try {
    const opened = executeLocalTaskPetCommand(pet, "default", {
      profileId: "default", open: () => { opens += 1; return Promise.resolve(true); },
    }, (message) => errors.push(message));
    assert.equal(opens, 1, "open must occur before awaiting its result");
    assert.equal(await opened, true);
    assert.equal(requests, 0);
    assert.deepEqual(errors, []);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("pet failures and a stale profile retain a false result without opening another profile", async () => {
  const errors: string[] = [];
  const onError = (message: string) => errors.push(message);
  let opens = 0;
  const controller = { profileId: "other", open: () => { opens += 1; return Promise.resolve(true); } };
  assert.equal(await executeLocalTaskPetCommand(pet, "default", controller, onError), false);
  assert.equal(opens, 0);
  assert.equal(await executeLocalTaskPetCommand(pet, "default", null, onError), false);
  assert.equal(await executeLocalTaskPetCommand(pet, "default", {
    profileId: "default", open: () => Promise.resolve(false),
  }, onError), false);
  assert.equal(await executeLocalTaskPetCommand(pet, "default", {
    profileId: "default", open: () => Promise.reject(new Error("transport unavailable")),
  }, onError), false);
  assert.equal(errors.length, 3);
});

test("delayed pet opening cannot clear a newer draft or another profile", async () => {
  for (const change of ["draft", "profile", "view", "none"] as const) {
    let draft = "/pet";
    let generation = 1;
    let profile = "default";
    let currentView = true;
    let resolveOpen!: (value: boolean) => void;
    const controller = { profileId: "default", open: () => new Promise<boolean>((resolve) => { resolveOpen = resolve; }) };
    const completion = completeLocalTaskPetSubmission(
      () => executeLocalTaskPetCommand(pet, "default", controller, () => assert.fail("unexpected error")),
      () => generation === 1 && profile === "default" && currentView,
      () => { draft = ""; },
      draft,
    );
    if (change === "draft") { draft = "new message"; generation += 1; }
    if (change === "profile") profile = "other";
    if (change === "view") currentView = false;
    resolveOpen(true);
    await completion;
    assert.equal(draft, change === "none" ? "" : change === "draft" ? "new message" : "/pet");
  }
});

test("failed local submission preserves its command draft", async () => {
  let draft = "/pet";
  await completeLocalTaskPetSubmission(() => Promise.resolve(false), () => true, () => { draft = ""; }, draft);
  assert.equal(draft, "/pet");
});
test("reserved pet input never falls through to backend, chat or steering", async () => {
  for (const commands of [[pet], [{ ...pet, canonical_id: "defaultspack:pet" }], []]) {
    for (const enabled of [true, false]) {
      let backend = 0;
      let chat = 0;
      let steer = 0;
      let opens = 0;
      let unavailable = 0;
      const consumed = consumeLocalTaskPetInput("/pet", enabled,
        (raw) => parseSlashCommandInput(raw, commands),
        () => { opens += 1; }, () => { unavailable += 1; });
      if (!consumed) { backend += 1; chat += 1; steer += 1; }
      assert.equal(consumed, true);
      assert.equal(backend + chat + steer, 0);
      assert.equal(opens, enabled && commands.length ? 1 : 0);
      assert.equal(unavailable, opens ? 0 : 1);
    }
  }
  for (const raw of ["//pet", "/petal", "/model", "message"]) {
    assert.equal(consumeLocalTaskPetInput(raw, true,
      (input) => parseSlashCommandInput(input, [pet]),
      () => assert.fail("unexpected local dispatch"),
      () => assert.fail("unexpected unavailable error")), false);
  }
});
test("pet selected from command menu preserves ordinary and escaped drafts", async () => {
  for (const original of ["ongoing message", "//pet", "/model llama", "/petal", "", "/", "/p", "/pe", "/pet", "/pet extra"]) {
    let draft = original;
    await completeLocalTaskPetSubmission(() => Promise.resolve(true), () => true,
      () => { draft = ""; }, original);
    const clearable = ["/", "/p", "/pe", "/pet", "/pet extra"].includes(original);
    assert.equal(draft, clearable ? "" : original);
  }
});
