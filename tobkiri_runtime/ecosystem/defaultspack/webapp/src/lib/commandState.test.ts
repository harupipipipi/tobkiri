import assert from "node:assert/strict";
import test from "node:test";

import { applyCommandStateSnapshots } from "./commandState";

test("authoritative command snapshot updates only its field", () => {
  const current = {
    models: { deepthink_enabled: false, preferred_model: "stub/default" },
    theme: { font_size: 16 },
  };
  const result = applyCommandStateSnapshots(current, {}, [{
    state_ref: "defaultspack:models.deepthink_enabled",
    value: true,
    revision: 1,
    freshness: "authoritative",
  }]);

  assert.equal(result.values.models.deepthink_enabled, true);
  assert.equal(result.values.models.preferred_model, "stub/default");
  assert.equal(result.values.theme.font_size, 16);
  assert.deepEqual(result.appliedPaths, ["models.deepthink_enabled"]);
});

test("older state snapshots cannot overwrite a newer revision", () => {
  const current = { models: { deepthink_enabled: false } };
  const result = applyCommandStateSnapshots(
    current,
    { "defaultspack:models.deepthink_enabled": 4 },
    [{
      state_ref: "defaultspack:models.deepthink_enabled",
      value: true,
      revision: 3,
    }],
  );

  assert.equal(result.values, current);
  assert.deepEqual(result.appliedPaths, []);
});
