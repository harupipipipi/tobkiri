import assert from "node:assert/strict";
import test from "node:test";
import { savedCodingContext, savedCodingContextAfterMutation } from "./savedCodingContext";
import { ConversationMutationBarrier } from "./conversationMutationBarrier";
import { dispatchCodingWorkspaceSelection } from "../components/coding/CodingCockpit";
const base = { coding: true, workspaceId: "workspace.one", pending: null, conversation: null };
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
});
test("legacy root group multiple bindings aliases remain denied", () => {
  for (const key of ["groupId","workspaceRoot","workspaceBindings","rumiDataPath"]) {
    assert.throws(() => savedCodingContext({...base,pending:{[key]:"legacy"}}));
  }
  assert.throws(() => savedCodingContext({...base,conversation:{conversation_kind:"coding",
    metadata:{workspace_id:"workspace.one",workspace_root:"/legacy"}}}));
  assert.throws(() => savedCodingContext({...base,pending:{groupId:"project.one",
    workspaceId:"workspace.one",workspaceRoot:"/display/only"}}));
  assert.throws(() => savedCodingContext({...base,pending:{workspaceId:"workspace.one",
    workspaceBindings:[{workspaceId:"workspace.one"}]}}));
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
  await assert.rejects(startSavedTurn, /workspace context/);
  assert.equal(savedTurnStarts, 0);
});
