import test from "node:test";
import assert from "node:assert/strict";

import { manualRuntimeModeSelectionEnabled } from "./runtimeMode";

test("manual runtime mode selection is opt-in and fails closed", () => {
  assert.equal(manualRuntimeModeSelectionEnabled({}), false);
  assert.equal(manualRuntimeModeSelectionEnabled({ general: {} }), false);
  assert.equal(manualRuntimeModeSelectionEnabled({
    general: { manual_runtime_mode_selection: "true" },
  }), false);
  assert.equal(manualRuntimeModeSelectionEnabled({
    general: { manual_runtime_mode_selection: 1 },
  }), false);
  assert.equal(manualRuntimeModeSelectionEnabled({
    general: { manual_runtime_mode_selection: true },
  }), true);
});
