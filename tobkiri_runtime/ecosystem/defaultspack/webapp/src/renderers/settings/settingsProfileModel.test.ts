import test from "node:test";
import assert from "node:assert/strict";

import type { ModelProfile, SettingsSection } from "../../lib/api";
import {
  buildSettingsProfileWorkspace,
  createProfileRecord,
  deleteProfileRecord,
  duplicateProfileRecord,
  renameProfileRecord,
  uniqueProfileId,
  type EditableSettingsProfileCollection,
  type SettingsProfileRecord,
} from "./settingsProfileModel";

const editableSections = [
  {
    id: "profiles",
    label: "Profiles",
    fields: [
      { id: "profiles", label: "Profiles", type: "json" },
      { id: "active_profile", label: "Active profile", type: "text" },
      { id: "default_profile", label: "Default profile", type: "text" },
    ],
  },
] as SettingsSection[];

function editableCollection(records: Record<string, unknown>[]): EditableSettingsProfileCollection {
  return {
    sectionId: "profiles",
    fieldId: "profiles",
    records,
    idField: "profile_id",
    nameField: "display_name",
    activeFieldId: "active_profile",
    defaultFieldId: "default_profile",
  };
}

function editableProfile(raw: Record<string, unknown>, index = 0): SettingsProfileRecord {
  return {
    id: String(raw.profile_id),
    name: String(raw.display_name),
    description: "",
    role: "General workspace",
    providerId: "openai",
    modelId: "openai/gpt-4.1",
    routeRefs: ["openai/primary"],
    source: "settings",
    sourceLabel: "Settings",
    editable: true,
    managed: false,
    active: false,
    default: false,
    favorite: false,
    readiness: "ready",
    readinessReason: "Connected",
    capabilityTags: [],
    raw,
    collectionIndex: index,
  };
}

test("profile workspace exposes active/default roles and the model-provider-credential route", () => {
  const workspace = buildSettingsProfileWorkspace({
    settingsSections: editableSections,
    settingsValues: {
      profiles: {
        profiles: [
          {
            profile_id: "work/deep-focus",
            display_name: "Deep Focus",
            description: "Long-form research",
            role: "Research and synthesis",
            preferred_model: "openai/gpt-4.1",
            editable: true,
          },
          {
            profile_id: "work/local",
            display_name: "Local fallback",
            preferred_model: "local/qwen",
            editable: true,
          },
        ],
        active_profile: "work/deep-focus",
        default_profile: "work/local",
      },
      models: {
        model_api_routes: "openai/gpt-4.1: openai/primary\n",
        favorite_profiles: ["work/deep-focus"],
      },
      apis: {
        api_keys: [
          {
            provider_id: "openai",
            apis: [{ api_id: "primary", credential_ref: "RUMIAPI_OPENAI_PRIMARY" }],
          },
        ],
      },
    },
    catalog: null,
    modelProfiles: [],
  });

  assert.equal(workspace.editableCollection?.sectionId, "profiles");
  assert.equal(workspace.editableCollection?.activeFieldId, "active_profile");
  assert.equal(workspace.editableCollection?.defaultFieldId, "default_profile");
  assert.equal(workspace.activeProfileId, "work/deep-focus");
  assert.equal(workspace.defaultProfileId, "work/local");

  const active = workspace.profiles.find((profile) => profile.id === "work/deep-focus");
  const fallback = workspace.profiles.find((profile) => profile.id === "work/local");
  assert.equal(active?.active, true);
  assert.equal(active?.default, false);
  assert.equal(active?.favorite, true);
  assert.equal(active?.role, "Research and synthesis");
  assert.equal(active?.providerId, "openai");
  assert.deepEqual(active?.routeRefs, ["openai/primary"]);
  assert.equal(active?.readiness, "ready");
  assert.equal(fallback?.default, true);
  assert.equal(fallback?.readiness, "local");
});

test("profile workspace merges catalog/model records without making runtime records editable", () => {
  const modelProfiles: ModelProfile[] = [
    {
      profile_id: "anthropic/claude-sonnet",
      display_name: "Claude Sonnet",
      provider_id: "anthropic",
      qualified_model_id: "anthropic/claude-sonnet",
      supports_thinking: true,
      supports_tool_calling: true,
      recommended_roles: ["coding", "analysis"],
      availability: { configured: true },
    },
  ];
  const workspace = buildSettingsProfileWorkspace({
    settingsSections: [],
    settingsValues: {
      models: { preferred_model: "anthropic/claude-sonnet" },
    },
    catalog: {
      agent_service: {
        default_profile: "runtime/balanced",
        profiles: [
          {
            profile_id: "runtime/balanced",
            display_name: "Balanced",
            preferred_model: "local/qwen",
            role: "Daily work",
          },
        ],
      },
    } as never,
    modelProfiles,
    activeModelProfileId: "anthropic/claude-sonnet",
  });

  assert.equal(workspace.editableCollection, null);
  assert.equal(workspace.profiles.length, 2);
  const active = workspace.profiles[0];
  assert.equal(active.id, "anthropic/claude-sonnet");
  assert.equal(active.active, true);
  assert.equal(active.editable, false);
  assert.deepEqual(active.capabilityTags.sort(), ["thinking", "tools"]);
  assert.equal(workspace.profiles.find((profile) => profile.id === "runtime/balanced")?.source, "catalog");
});

test("profile workspace renders localized object labels without coercing them to object text", () => {
  const workspace = buildSettingsProfileWorkspace({
    settingsSections: [],
    settingsValues: {},
    catalog: {
      agent_service: {
        default_profile: "defaultspack.local_agent",
        profiles: [
          {
            profile_id: "defaultspack.local_agent",
            display_name: { ja: "既定エージェント", en: "Default Agent" },
          },
        ],
      },
    } as never,
    modelProfiles: [],
    activeModelProfileId: "defaultspack.local_agent",
  });

  assert.equal(workspace.profiles[0]?.name, "既定エージェント");
  assert.equal(workspace.profiles[0]?.active, true);
  assert.equal(workspace.profiles[0]?.name.includes("[object Object]"), false);
});

test("profile readiness recognizes account credentials and permission blocks without exposing secret values", () => {
  const baseValues = {
    profiles: {
      profiles: [
        {
          profile_id: "google/work",
          display_name: "Google work",
          preferred_model: "google/gemini-2.5-pro",
        },
      ],
      active_profile: "google/work",
    },
  };

  const connected = buildSettingsProfileWorkspace({
    settingsSections: editableSections,
    settingsValues: {
      ...baseValues,
      accounts_connections: {
        providers: {
          google: {
            credential_ref: { credential_id: "oauth-google-primary" },
            access_token: "must-never-be-rendered",
          },
        },
      },
    },
    catalog: null,
  });
  assert.equal(connected.profiles[0]?.readiness, "ready");
  assert.doesNotMatch(connected.profiles[0]?.readinessReason ?? "", /must-never-be-rendered/);

  const blocked = buildSettingsProfileWorkspace({
    settingsSections: editableSections,
    settingsValues: {
      ...baseValues,
      accounts_connections: {
        providers: {
          google: {
            connection_status: "permission_denied",
            status_label: "Required scope was denied",
          },
        },
      },
    },
    catalog: null,
  });
  assert.equal(blocked.profiles[0]?.readiness, "blocked");
  assert.equal(blocked.profiles[0]?.readinessReason, "Required scope was denied");
});

test("profile readiness does not infer a successful connection from identifiers alone", () => {
  const workspace = buildSettingsProfileWorkspace({
    settingsSections: editableSections,
    settingsValues: {
      profiles: {
        profiles: [
          { profile_id: "unknown", display_name: "Unknown route" },
          { profile_id: "openai/enumerated", display_name: "Enumerated API", preferred_model: "openai/gpt-4.1" },
          { profile_id: "anthropic/connected", display_name: "Connected status", preferred_model: "anthropic/claude" },
          { profile_id: "google/blocked", display_name: "Blocked", preferred_model: "google/gemini" },
        ],
      },
      apis: {
        api_keys: [
          { provider_id: "openai", apis: [{ api_id: "available-option" }] },
          { provider_id: "anthropic", connection_status: "connected" },
          { provider_id: "google", connection_status: "permission_denied" },
        ],
      },
    },
    catalog: null,
  });

  assert.equal(workspace.profiles.find((profile) => profile.id === "unknown")?.readiness, "unknown");
  assert.equal(workspace.profiles.find((profile) => profile.id === "openai/enumerated")?.readiness, "needs_connection");
  assert.equal(workspace.profiles.find((profile) => profile.id === "anthropic/connected")?.readiness, "ready");
  assert.equal(workspace.profiles.find((profile) => profile.id === "google/blocked")?.readiness, "blocked");
});

test("model availability reasons are not treated as blocks without an unavailable status", () => {
  const workspace = buildSettingsProfileWorkspace({
    settingsSections: [],
    settingsValues: {},
    catalog: null,
    modelProfiles: [
      {
        profile_id: "openai/discovered",
        display_name: "Discovered",
        provider_id: "openai",
        qualified_model_id: "openai/discovered",
        availability: { status: "available", reason: "Discovered from the provider catalog." },
      } as ModelProfile,
      {
        profile_id: "openai/denied",
        display_name: "Denied",
        provider_id: "openai",
        qualified_model_id: "openai/denied",
        availability: { status: "unavailable", reason: "Organization policy denied this model." },
      } as ModelProfile,
      {
        profile_id: "vllm/local",
        display_name: "Local vLLM",
        provider_id: "vllm",
        qualified_model_id: "vllm/local",
      } as ModelProfile,
    ],
  });

  assert.equal(workspace.profiles.find((profile) => profile.id === "openai/discovered")?.readiness, "needs_connection");
  assert.equal(workspace.profiles.find((profile) => profile.id === "openai/denied")?.readiness, "blocked");
  assert.equal(workspace.profiles.find((profile) => profile.id === "vllm/local")?.readiness, "local");
});

test("profile collection is editable only when the settings schema exposes the array field", () => {
  const workspace = buildSettingsProfileWorkspace({
    settingsSections: [{ id: "profiles", label: "Profiles", fields: [] }],
    settingsValues: {
      profiles: {
        profiles: [{ profile_id: "hidden/profile", display_name: "Hidden profile" }],
      },
    },
    catalog: null,
  });

  assert.equal(workspace.editableCollection, null);
  assert.equal(workspace.profiles.length, 0);
});

test("profile ids are stable, namespaced, and collision-safe", () => {
  assert.equal(uniqueProfileId("Deep Focus", ["work/default"]), "custom/deep-focus");
  assert.equal(uniqueProfileId("Deep Focus", ["work/default", "custom/deep-focus"]), "custom/deep-focus-2");
  assert.equal(uniqueProfileId("集中", ["profile"]), "profile-2");
});

test("create follows the existing record shape instead of inventing a backend contract", () => {
  const collection = editableCollection([
    {
      profile_id: "work/default",
      display_name: "Default",
      model_profile_id: "openai/gpt-4.1-mini",
      editable: true,
      managed: false,
      builtin: false,
    },
  ]);
  const created = createProfileRecord({
    collection,
    name: "Deep Focus",
    description: "Research",
    modelId: "anthropic/claude-sonnet",
  });

  assert.deepEqual(created, {
    profile_id: "custom/deep-focus",
    display_name: "Deep Focus",
    description: "Research",
    model_profile_id: "anthropic/claude-sonnet",
    provider_id: "anthropic",
    editable: true,
    managed: false,
    builtin: false,
  });
});

test("duplicate strips plaintext secrets and active/default flags but preserves credential references", () => {
  const raw = {
    profile_id: "work/private",
    display_name: "Private",
    active: true,
    default: true,
    api_key: "plain-api-key",
    private_key: "plain-private-key",
    authorization_header: "Bearer plain-token",
    provider_id: "openai",
    credential_ref: "RUMIAPI_OPENAI_PRIMARY",
    nested: {
      password: "plain-password",
      refresh_token: "plain-refresh-token",
      credential_ref: { credential_id: "vault-entry" },
      token_id: "reference-id",
    },
  };
  const collection = editableCollection([raw]);
  const duplicate = duplicateProfileRecord({
    collection,
    profile: editableProfile(raw),
    name: "Private copy",
  });

  assert.equal(duplicate.profile_id, "custom/private-copy");
  assert.equal(duplicate.display_name, "Private copy");
  assert.equal(duplicate.provider_id, "openai");
  assert.equal(duplicate.credential_ref, "RUMIAPI_OPENAI_PRIMARY");
  assert.equal(duplicate.active, undefined);
  assert.equal(duplicate.default, undefined);
  assert.equal(duplicate.api_key, undefined);
  assert.equal(duplicate.private_key, undefined);
  assert.equal(duplicate.authorization_header, undefined);
  assert.deepEqual(duplicate.nested, {
    credential_ref: { credential_id: "vault-entry" },
    token_id: "reference-id",
  });
  assert.doesNotMatch(JSON.stringify(duplicate), /plain-api-key|plain-private-key|plain-token|plain-password|plain-refresh-token/);
});

test("rename and delete change only the selected collection record", () => {
  const records = [
    { profile_id: "one", display_name: "One" },
    { profile_id: "two", display_name: "Two" },
  ];
  const collection = editableCollection(records);
  const profile = editableProfile(records[1], 1);

  assert.deepEqual(renameProfileRecord(collection, profile, "Second"), [
    records[0],
    { profile_id: "two", display_name: "Second" },
  ]);
  assert.deepEqual(deleteProfileRecord(collection, profile), [records[0]]);
});
