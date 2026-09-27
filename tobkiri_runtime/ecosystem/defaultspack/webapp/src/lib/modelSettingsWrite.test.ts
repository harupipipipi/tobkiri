import assert from "node:assert/strict";
import test from "node:test";

import { modelStateWriteForSettingsField } from "./modelSettingsWrite";

test("both visible and advanced model pickers use the model-state write contract", () => {
  for (const fieldId of ["main_model", "preferred_model"]) {
    assert.deepEqual(
      modelStateWriteForSettingsField("models", fieldId, " openrouter/example "),
      { kind: "preferred_model", value: "openrouter/example" },
    );
  }
  assert.equal(modelStateWriteForSettingsField("general", "main_model", "openrouter/example"), null);
  assert.equal(modelStateWriteForSettingsField("models", "model_api_routes", "route"), null);
});
