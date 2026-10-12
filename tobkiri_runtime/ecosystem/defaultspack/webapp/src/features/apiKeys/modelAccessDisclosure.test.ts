import assert from "node:assert/strict";
import test from "node:test";
import {
  initialModelAccessDisclosureState,
  modelAccessDisclosureReducer,
} from "./modelAccessDisclosure";

test("access disclosure waits for policy, opens for unset policy, and keeps model list collapsed", () => {
  assert.deepEqual(initialModelAccessDisclosureState, {
    settingsOpen: false,
    modelListOpen: false,
  });

  const loading = modelAccessDisclosureReducer(initialModelAccessDisclosureState, {
    type: "loading",
  });
  assert.deepEqual(loading, initialModelAccessDisclosureState);

  const unsetPolicy = modelAccessDisclosureReducer(loading, {
    type: "loaded",
    hasPolicy: false,
  });
  assert.deepEqual(unsetPolicy, { settingsOpen: true, modelListOpen: false });

  const openedModelList = modelAccessDisclosureReducer(unsetPolicy, {
    type: "toggle-model-list",
    open: true,
  });
  assert.deepEqual(openedModelList, { settingsOpen: true, modelListOpen: true });

  const savedPolicy = modelAccessDisclosureReducer(openedModelList, {
    type: "loaded",
    hasPolicy: true,
  });
  assert.deepEqual(savedPolicy, { settingsOpen: false, modelListOpen: false });
});

test("a collapsed unset-policy disclosure has an explicit reopen action", () => {
  const collapsed = modelAccessDisclosureReducer(
    { settingsOpen: true, modelListOpen: false },
    { type: "toggle-settings", open: false },
  );
  assert.deepEqual(collapsed, { settingsOpen: false, modelListOpen: false });
  assert.deepEqual(modelAccessDisclosureReducer(collapsed, {
    type: "toggle-settings",
    open: true,
  }), { settingsOpen: true, modelListOpen: false });
});
