import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { availabilityCopy, isModelsAvailable, type ModelAvailabilityAfterKeySave } from "./useModelAvailability";

describe("model availability after key save", () => {
  it("treats a successful save without an availability snapshot as successful", () => {
    const value: ModelAvailabilityAfterKeySave = { status: "saved" };

    assert.equal(isModelsAvailable(value), false);
    assert.deepEqual(availabilityCopy(value), {
      tone: "success",
      text: "API key saved.",
    });
  });

  it("treats populated models_available responses as successful", () => {
    const value: ModelAvailabilityAfterKeySave = {
      status: "models_available",
      selected_profile_id: "google/main/gemini-test",
      profiles: [
        {
          profile_id: "google/main/gemini-test",
          qualified_model_id: "google/main/gemini-test",
          provider_id: "google",
          model_id: "gemini-test",
          display_name: "Gemini Test",
        },
      ],
    };

    assert.equal(isModelsAvailable(value), true);
    assert.deepEqual(availabilityCopy(value), {
      tone: "success",
      text: "Saved. Model is available: Gemini Test",
    });
  });

  it("does not render a green saved state for route_required", () => {
    const value: ModelAvailabilityAfterKeySave = {
      status: "route_required",
      provider_id: "google",
      api_id: "main",
      candidate_models: [{ provider_id: "google", model_id: "gemini-test" }],
      reason: "Choose a model route for this API key.",
    };

    assert.equal(isModelsAvailable(value), false);
    assert.deepEqual(availabilityCopy(value), {
      tone: "warning",
      text: "Choose a model route for this API key.",
    });
  });
});
