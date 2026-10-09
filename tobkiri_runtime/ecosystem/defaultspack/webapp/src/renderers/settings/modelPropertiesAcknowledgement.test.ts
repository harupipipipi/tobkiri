import assert from "node:assert/strict";
import test from "node:test";
import { matchesModelPropertiesProfile } from "./modelPropertiesAcknowledgement";
import type { SettingsProfileRecord } from "./settingsProfileModel";
import type { ModelPropertiesRequest } from "../../features/search/modelPropertiesNavigation";

const request = {
  requestId: 1, intent: "properties", registered: true,
  identity: { profileId: "saved", providerId: "provider", modelId: "model", connectionId: "route" },
} as ModelPropertiesRequest;
const profile = {
  id: "saved", providerId: "provider", modelId: "provider/model",
  raw: { model_id: "model", metadata: { provider_instance_id: "route" } },
} as unknown as SettingsProfileRecord;

test("acknowledges the exact saved profile with a qualified presentation model ID", () => {
  assert.equal(matchesModelPropertiesProfile(request, profile), true);
});
test("profile ID alone cannot acknowledge a different model, provider, or connection", () => {
  assert.equal(matchesModelPropertiesProfile(request, { ...profile, providerId: "other" }), false);
  assert.equal(matchesModelPropertiesProfile(request, { ...profile, raw: { model_id: "other" } }), false);
  assert.equal(matchesModelPropertiesProfile(request, { ...profile, raw: { model_id: "model", connection_id: "other" } }), false);
});
test("missing route evidence and mismatched qualified identity keep the request queued", () => {
  assert.equal(matchesModelPropertiesProfile(request, { ...profile, raw: { model_id: "model" } }), false);
  assert.equal(matchesModelPropertiesProfile({ ...request, identity: { ...request.identity, qualifiedModelId: "exact" } }, profile), false);
  assert.equal(matchesModelPropertiesProfile({ ...request, registered: false }, profile), false);
});
test("canonical saved provider instance is exact connection evidence", () => {
  const bound = { ...request, identity: { ...request.identity, providerId: "provider.openrouter.main", connectionId: "provider.openrouter.main" } };
  assert.equal(matchesModelPropertiesProfile(bound, { ...profile,
    providerId: "provider.openrouter.main", raw: { model_id: "model", provider_id: "provider.openrouter.main" } }), true);
  assert.equal(matchesModelPropertiesProfile({ ...bound, identity: { ...bound.identity, connectionId: "provider.openrouter.backup" } }, { ...profile,
    providerId: "provider.openrouter.main", raw: { model_id: "model", provider_id: "provider.openrouter.main" } }), false);
});
