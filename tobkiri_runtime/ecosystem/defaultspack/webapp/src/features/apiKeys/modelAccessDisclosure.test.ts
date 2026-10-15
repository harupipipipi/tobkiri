import assert from "node:assert/strict";
import test from "node:test";
import { MODEL_ACCESS_VERSION } from "./modelAccessPolicy";
import {
  initialModelAccessDisclosureState,
  modelAccessDisclosureReducer,
  modelAccessPolicySummary,
} from "./modelAccessDisclosure";

test("access disclosure keeps saved and inherited policy controls collapsed until requested", () => {
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
  });
  assert.deepEqual(unsetPolicy, { settingsOpen: false, modelListOpen: false });

  const openedSettings = modelAccessDisclosureReducer(unsetPolicy, {
    type: "toggle-settings", open: true,
  });

  const openedModelList = modelAccessDisclosureReducer(openedSettings, {
    type: "toggle-model-list",
    open: true,
  });
  assert.deepEqual(openedModelList, { settingsOpen: true, modelListOpen: true });

  const savedPolicy = modelAccessDisclosureReducer(openedModelList, {
    type: "loaded",
  });
  assert.deepEqual(savedPolicy, { settingsOpen: false, modelListOpen: false });
});

test("a collapsed policy disclosure has an explicit reopen action", () => {
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

test("inherited legacy policy is distinct from an explicit zero-model denial", () => {
  assert.equal(modelAccessPolicySummary(false, null), "既存の許可設定を使用");
  assert.equal(modelAccessPolicySummary(false, {
    version: MODEL_ACCESS_VERSION, mode: "explicit", model_ids: [],
  }), "個別指定・0件");
  assert.equal(modelAccessPolicySummary(true, undefined), "設定を確認中");
  assert.equal(modelAccessPolicySummary(false, undefined), "状態を確認できません");
});
