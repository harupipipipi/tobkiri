import assert from "node:assert/strict";
import test from "node:test";
import { savedCodingContext, savedCodingContextAfterMutation } from "./savedCodingContext";
import { assertSavedProjectSendSupported } from "./savedProjectSendPreflight";
import { ConversationMutationBarrier } from "./conversationMutationBarrier";
import { dispatchCodingWorkspaceSelection } from "../components/coding/CodingCockpit";
const base = { coding: true, workspaceId: "workspace.one", pending: null, conversation: null };

test("single-folder Project send preserves membership and inert folder hints", () => {
  const binding = Object.freeze({ workspaceId: "workspace.one", workspaceLabel: "Work", workspaceRoot: "/display/only" });
  const project = Object.freeze({ groupId: "project.one", workspaceId: "workspace.one", workspaceBindings: Object.freeze([binding]) });
  assertSavedProjectSendSupported({
    projectId: project.groupId,
    pendingWorkspaceBindings: project.workspaceBindings,
  });
  assert.equal(project.groupId, "project.one");
  assert.equal(project.workspaceBindings[0], binding);
});

test("existing folder-less Group membership can use its original conversation", () => {
  assertSavedProjectSendSupported({ projectId: "project.one" });
  for (const projectId of [false, 0, {}, "../group", "bad group"]) {
    assert.throws(() => assertSavedProjectSendSupported({ projectId }), /所属情報/);
  }
});

test("multiple-root Project metadata remains complete and ordered", () => {
  for (const source of ["pendingWorkspaceBindings", "conversationWorkspaceBindings"] as const) {
    const bindings = [
        { workspaceId: "workspace.one", workspaceRoot: "/work/one", workspaceLabel: "One" },
        { workspaceId: "workspace.two", workspaceRoot: "/work/two", workspaceLabel: "Two" },
    ];
    assertSavedProjectSendSupported({ projectId: "project.one", [source]: bindings });
    assert.deepEqual(bindings.map((item) => item.workspaceId), ["workspace.one", "workspace.two"]);
  }
});

test("malformed Project binding shapes stay rejected", () => {
  for (const value of [false, 0, "legacy", {}, [], [null], [{ workspaceId: "workspace.one" }]]) {
    for (const source of ["pendingWorkspaceBindings", "conversationWorkspaceBindings"] as const) {
      assert.throws(() => assertSavedProjectSendSupported({ projectId: "project.one", [source]: value }), /作業フォルダ設定/);
    }
  }
});

test("No project and one canonical Coding workspace keep the exact saved context", () => {
  assertSavedProjectSendSupported({ projectId: null });
  assert.deepEqual(savedCodingContext({ ...base, pending: {
    workspaceId: "workspace.one", workspaceLabel: "Work", workspaceRoot: "/display/only",
  } }), { mode: "coding", workspace_id: "workspace.one" });
});

test("normal coding-workspace picker creates exact saved coding context", () => {
  assert.deepEqual(savedCodingContext(base), {mode:"coding",workspace_id:"workspace.one"});
  assert.deepEqual(savedCodingContext({...base,pending:{workspaceId:"workspace.one"}}),
    {mode:"coding",workspace_id:"workspace.one"});
  assert.deepEqual(savedCodingContext({...base,pending:{workspaceId:"workspace.one",
    workspaceLabel:"Work",workspaceRoot:"/display/only"}}),
    {mode:"coding",workspace_id:"workspace.one"});
});
test("display hints must belong to the exact selected workspace", () => {
  assert.throws(() => savedCodingContext({...base,pending:{workspaceId:"workspace.other",
    workspaceLabel:"Work",workspaceRoot:"/display/only"}}));
  assert.throws(() => savedCodingContext({...base,pending:{workspaceLabel:"Work",
    workspaceRoot:"/display/only"}}));
});
test("refetched coding conversation with a stale workspace fails closed", () => {
  assert.deepEqual(savedCodingContext({...base,conversation:{conversation_kind:"coding",
    metadata:{mode:"coding",workspace_id:"workspace.one"}}}),
    {mode:"coding",workspace_id:"workspace.one"});
  assert.throws(() => savedCodingContext({...base,conversation:{metadata:{workspace_id:"other"}}}));
  assert.throws(() => savedCodingContext({...base,conversation:{conversation_kind:"coding",
    metadata:{mode:"coding",workspace_id:"workspace.other"}}}));
  assert.throws(() => savedCodingContext({...base,conversation:{conversation_kind:"coding",
    metadata:{workspace_id:"workspace.one"}}}));
  assert.throws(() => savedCodingContext({...base,conversation:{conversation_kind:"chat",
    metadata:{mode:"coding",workspace_id:"workspace.one"}}}));
});
test("unowned legacy roots remain denied while Project scalar primary is retained", () => {
  for (const key of ["workspaceRoot","workspaceBindings","rumiDataPath"]) {
    assert.throws(() => savedCodingContext({...base,pending:{[key]:"legacy"}}));
  }
  assert.deepEqual(savedCodingContext({...base,conversation:{conversation_kind:"coding",
    metadata:{mode:"coding",workspace_id:"workspace.one",workspace_root:"/display/only"}}}),
    {mode:"coding",workspace_id:"workspace.one"});
  assert.deepEqual(savedCodingContext({...base,pending:{groupId:"project.one",
    workspaceId:"workspace.one",workspaceRoot:"/display/only"}}), {mode:"coding",workspace_id:"workspace.one"});
  assert.throws(() => savedCodingContext({...base,pending:{workspaceId:"workspace.one",
    workspaceBindings:[{workspaceId:"workspace.one"}]}}));
});

test("a second binding can remain the existing scalar primary without a new selection", () => {
  const bindings = [
    { workspace_id: "workspace.one", workspace_label: "One", workspace_root: "/one" },
    { workspace_id: "workspace.two", workspace_label: "Two", workspace_root: "/two" },
  ];
  const conversation = { conversation_kind: "coding", group_id: "project.one", metadata: {
    mode: "coding", workspace_id: "workspace.two", workspace_bindings: bindings,
  } };
  assertSavedProjectSendSupported({ projectId: conversation.group_id, conversationWorkspaceBindings: bindings });
  assert.deepEqual(savedCodingContext({ coding: true, workspaceId: "workspace.two", pending: null, conversation }),
    {mode:"coding",workspace_id:"workspace.two"});
  assert.equal(conversation.metadata.workspace_bindings, bindings);
});

test("clearing membership preserves the canonical conversation primary and inert full bindings", () => {
  const bindings = [
    { workspace_id: "workspace.one", workspace_label: "One", workspace_root: "/one" },
    { workspace_id: "workspace.two", workspace_label: "Two", workspace_root: "/two" },
  ];
  assertSavedProjectSendSupported({ projectId: null, conversationWorkspaceBindings: bindings });
  assert.deepEqual(savedCodingContext({ coding: true, workspaceId: "workspace.two", pending: null,
    conversation: { conversation_kind: "coding", group_id: null, metadata: {
      mode: "coding", workspace_id: "workspace.two", workspace_bindings: bindings,
    } } }), { mode: "coding", workspace_id: "workspace.two" });
  assert.deepEqual(bindings.map((binding) => binding.workspace_id), ["workspace.one", "workspace.two"]);
});
test("missing invalid IDs and unsupported kinds deny", () => {
  for (const workspaceId of [null,"","../outside","bad id"]) {
    assert.throws(() => savedCodingContext({...base,workspaceId}));
  }
  assert.throws(() => savedCodingContext({...base,conversation:{conversation_kind:"operations",
    metadata:{workspace_id:"workspace.one"}}}));
  assert.equal(savedCodingContext({...base,coding:false}),null);
});


test("CodingCockpit selection waits for mutation ACK then emits only canonical saved context", async () => {
  let selectedWorkspaceId: string | null = null;
  dispatchCodingWorkspaceSelection("workspace.one", (id) => { selectedWorkspaceId = id; });
  assert.equal(selectedWorkspaceId, "workspace.one");

  const barrier = new ConversationMutationBarrier();
  let acknowledgeMutation!: () => void;
  const mutationAck = new Promise<void>((resolve) => { acknowledgeMutation = resolve; });
  barrier.record("store.one", "conversation.one", mutationAck);
  let refetches = 0;
  let savedTurnStarts = 0;
  const startSavedTurn = async () => {
    const result = await savedCodingContextAfterMutation({
      coding: true,
      workspaceId: selectedWorkspaceId,
      pending: null,
      waitForMutation: () => barrier.wait("store.one", "conversation.one"),
      refetchConversation: async () => {
        refetches += 1;
        return {conversation_kind:"coding",metadata:{mode:"coding",workspace_id:selectedWorkspaceId!}};
      },
    });
    savedTurnStarts += 1;
    return result.metadata;
  };
  const started = startSavedTurn();
  await Promise.resolve();
  assert.equal(refetches, 0);
  assert.equal(savedTurnStarts, 0);
  acknowledgeMutation();
  assert.deepEqual(await started, {mode:"coding",workspace_id:"workspace.one"});
  assert.equal(refetches, 1);
  assert.equal(savedTurnStarts, 1);
});

test("stale refetched workspace context denies saved-turn start", async () => {
  let selectedWorkspaceId: string | null = null;
  dispatchCodingWorkspaceSelection("workspace.one", (id) => { selectedWorkspaceId = id; });
  const barrier = new ConversationMutationBarrier();
  barrier.record("store.one", "conversation.one", Promise.resolve());
  let savedTurnStarts = 0;
  const startSavedTurn = async () => {
    const result = await savedCodingContextAfterMutation({
      coding: true,
      workspaceId: selectedWorkspaceId,
      pending: null,
      waitForMutation: () => barrier.wait("store.one", "conversation.one"),
      refetchConversation: async () => ({conversation_kind:"coding",
        metadata:{mode:"coding",workspace_id:"workspace.other"}}),
    });
    savedTurnStarts += 1;
    return result.metadata;
  };
  await assert.rejects(startSavedTurn, /会話の作業フォルダが変わりました/);
  assert.equal(savedTurnStarts, 0);
});
