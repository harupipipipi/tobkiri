import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { ModelProfile } from "../../lib/api";
import {
  normalizeThinkingControlInput,
  thinkingControlCandidates,
  thinkingControlInputError,
  thinkingControlMode,
} from "./thinkingControl";


const numericProfile: ModelProfile = {
  profile_id: "example/numeric",
  display_name: "Numeric",
  supports_thinking: true,
  thinking_control: {
    supported: true,
    input_schema: {
      type: "number",
      unit: "tokens",
      min: 0,
      max: 32_000,
      step: 500,
    },
    request_binding: { path: "thinking.budget_tokens", value: "$input" },
  },
};

describe("profile-driven thinking control", () => {
  it("selects numeric, enum, and text UI from the profile", () => {
    assert.equal(thinkingControlMode(numericProfile), "number");
    assert.equal(thinkingControlMode({
      profile_id: "example/text",
      display_name: "Text",
      thinking_control: { supported: true, input_schema: { type: "text" } },
    }), "text");
    assert.equal(thinkingControlMode({
      profile_id: "example/enum",
      display_name: "Enum",
      supports_thinking: true,
      thinking_levels: ["low", "ultra super max"],
    }), "enum");
  });

  it("normalizes numeric shorthand and reports profile bounds", () => {
    assert.equal(thinkingControlInputError(numericProfile, "1.5k"), null);
    assert.equal(normalizeThinkingControlInput(numericProfile, "1.5k"), 1_500);
    assert.equal(thinkingControlInputError(numericProfile, "32.5k"), "Maximum: 32000");
    assert.match(thinkingControlInputError(numericProfile, "high") ?? "", /optional k/);
  });

  it("sources enum candidates only from the selected profile", () => {
    const profile: ModelProfile = {
      profile_id: "example/enum",
      display_name: "Enum",
      supports_thinking: true,
      thinking_control: {
        supported: true,
        input_schema: { type: "enum", values: ["low", "ultra super max"] },
      },
    };

    assert.deepEqual(thinkingControlCandidates(profile), ["low", "ultra super max"]);
    assert.notEqual(thinkingControlInputError(profile, "high"), null);
  });
});
