import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { buildVisibleModelOptions, SettingsModalRenderer, settingsCloseRequiresConfirmation, toggleSettingsRowSelection } from "./SettingsModalRenderer";
import { CredentialTransferModal, credentialTransferCanClose, credentialTransferFocusTarget } from "../components/CredentialTransferModal";
import { createSettingsFieldRendererRegistry, SettingsFieldRendererHost } from "./settings/fieldRendererRegistry";
import { builtinSettingsFieldRendererEntries } from "./settings/builtinSettingsFieldRenderers";
import {
  appendEmptySlashCommandDraft,
  serializeSlashCommandDrafts,
  slashCommandDraftRowsFromValue,
} from "./settings/renderers/slashCommandsField";
import { allowCleartextMobileQr } from "../lib/mobileCleartextQr";
import { apiKeySetupTargetFieldId } from "./settings/renderers/settingsFieldRendererUtils";
import { SettingsStatusBar } from "./settings/SettingsStatusBar";
import { ProfileSettingsPanel } from "./settings/ProfileSettingsPanel";
import { ModelSearchPicker } from "../features/models/ModelSearchPicker";
import type { TemplateSettingsField } from "./template/settingsFieldMetadata";
import type { SettingsSection } from "../lib/api";

function makeModelOption(index: number) {
  return {
    value: `demo/provider-model-${index}`,
    label: `Demo Provider / Model ${index}`,
    provider_id: "demo",
    provider_display_name: "Demo Provider",
    model_id: `model-${index}`,
  };
}

test("settings error surfaces keep severity glyphs separate from stable copy controls", () => {
  const statusHtml = renderToStaticMarkup(createElement(SettingsStatusBar, {
    backendNote: "Kernel is unreachable.",
    backendState: "offline",
    loadState: { status: "error", message: "Settings refresh failed." },
    locale: "en",
    saveState: {
      dirtyKeys: ["profiles.active_profile"],
      message: "Profile save failed.",
      status: "error",
    },
  }));
  const profileHtml = renderToStaticMarkup(createElement(ProfileSettingsPanel, {
    loadState: { status: "error", message: "Profiles could not load." },
    locale: "en",
    onSettingChange: () => undefined,
    workspace: {
      activeProfileId: "",
      defaultProfileId: "",
      editableCollection: null,
      modelRoutesText: "",
      profiles: [],
    },
  }));
  const modelHtml = renderToStaticMarkup(createElement(ModelSearchPicker, {
    error: "Model search failed.",
    onChange: () => undefined,
    onOpenChange: () => undefined,
    onQueryChange: () => undefined,
    open: true,
    query: "demo",
    value: "",
  }));

  assert.equal((statusHtml.match(/data-copy-action=""/g) ?? []).length, 3);
  assert.match(statusHtml, /data-error-icon="error"/);
  assert.match(profileHtml, /aria-label="Copy profile load error"/);
  assert.match(profileHtml, /data-copy-action=""/);
  assert.match(modelHtml, /aria-label="モデル検索エラーをコピー"/);
  assert.match(modelHtml, /data-error-icon="error"/);
  assert.match(modelHtml, /data-copy-action=""/);
});


test("settings close guard allows in-flight autosaves and guards only failed dirty changes", () => {
  assert.equal(settingsCloseRequiresConfirmation({ status: "saving", dirtyKeys: [] }), false);
  assert.equal(settingsCloseRequiresConfirmation({ status: "saving", dirtyKeys: ["profiles.active_profile"] }), false);
  assert.equal(settingsCloseRequiresConfirmation({ status: "error", dirtyKeys: ["profiles.active_profile"] }), true);
  assert.equal(settingsCloseRequiresConfirmation({ status: "error", dirtyKeys: [] }), false);
  assert.equal(settingsCloseRequiresConfirmation({ status: "saved", dirtyKeys: [], lastSavedAt: Date.now() }), false);
});

test("settings row selection clears when the selected row is clicked again", () => {
  assert.equal(toggleSettingsRowSelection("provider:token", "provider:token"), "");
  assert.equal(toggleSettingsRowSelection("provider:token", "provider:other"), "provider:other");
  assert.equal(toggleSettingsRowSelection("", "provider:token"), "provider:token");
});

test("settings AI surface launches the normal chat with the Settings skill", () => {
  const html = renderToStaticMarkup(createElement(SettingsModalRenderer, {
    isOpen: true,
    activeSectionId: "quick_setup",
    catalog: {
      sidebar: { filters: [], items: [{ id: "browser", label: "Browser", category: "tool", description: "Inspect pages" }] },
      settings: { sections: [], values: {} },
      skills: [{ id: "review", label: "Review", description: "Review settings" }],
      chat_rendering: { renderers: [] },
      extension_points: [],
    },
    health: null,
    previewsCount: 0,
    settingsSections: [],
    settingsValues: {},
    saveState: { status: "idle", dirtyKeys: [] },
    locale: "ja",
    onClose: () => undefined,
    onStartSettingsChat: () => undefined,
    onSettingChange: () => undefined,
  }));

  assert.match(html, /AIアシスタント/);
  assert.match(html, /AIと設定する/);
  assert.match(html, /Settings Modeを開く/);
  assert.match(html, /@Settings/);
  assert.doesNotMatch(html, /設定について相談する/);
  assert.doesNotMatch(html, /設定ホーム/);
});

test("CredentialTransferModal never renders cleartext credentials or a legacy QR payload", () => {
  const html = renderToStaticMarkup(createElement(CredentialTransferModal, {
    providerId: "google",
    providerLabel: "Google",
    apiId: "main",
    onClose: () => undefined,
  }));
  assert.doesNotMatch(html, /rumi_api/i);
  assert.doesNotMatch(html, /data:image/);
  assert.doesNotMatch(html, /credential[^<]*(?:value|secret)/i);
});

test("CredentialTransferModal guards active transfer close and traps focus", () => {
  assert.equal(credentialTransferCanClose("awaiting_confirmation", false), false);
  assert.equal(credentialTransferCanClose("pending", false), false);
  assert.equal(credentialTransferCanClose("accepted", false), true);
  assert.equal(credentialTransferCanClose("completed", true), false);
  assert.equal(credentialTransferFocusTarget(2, 3, false), 0);
  assert.equal(credentialTransferFocusTarget(0, 3, true), 2);
  assert.equal(credentialTransferFocusTarget(0, 0, false), null);
});

test("settings field renderer host falls back for unknown fields", () => {
  const registry = createSettingsFieldRendererRegistry();
  const field = {
    id: "future_field",
    label: "Future Field",
    type: "future_field",
    default: "default value",
  } as TemplateSettingsField;

  const html = renderToStaticMarkup(
    createElement(SettingsFieldRendererHost, {
      registry,
      field,
      sectionId: "demo",
      value: "fallback value",
      onChange: () => undefined,
      fallbackRenderer: ({ value }) => createElement("span", { "data-fallback": "settings" }, String(value)),
    }),
  );

  assert.match(html, /data-fallback="settings"/);
  assert.match(html, /fallback value/);
});

test("settings field renderer registry routes new field types and catalog bindings", () => {
  const registry = createSettingsFieldRendererRegistry([
    {
      id: "builtin-model-select",
      types: ["model_select"],
      render: ({ field, value }) => createElement("output", { "data-renderer": "model" }, `${field.id}:${String(value)}`),
    },
    {
      id: "api-key-setup-binding",
      component: "ApiKeySetupField",
      render: ({ field }) => createElement("output", { "data-renderer": "api-key" }, field.id),
    },
    {
      id: "provider-select-renderer",
      renderers: ["provider_select.compact"],
      render: ({ field }) => createElement("output", { "data-renderer": "provider" }, field.id),
    },
  ]);

  const modelHtml = renderToStaticMarkup(
    createElement(SettingsFieldRendererHost, {
      registry,
      field: {
        id: "preferred_model",
        label: "Preferred Model",
        type: "model_select",
      } as TemplateSettingsField,
      sectionId: "models",
      value: "google/gemini",
      onChange: () => undefined,
      fallbackRenderer: () => createElement("span", null, "fallback"),
    }),
  );
  const apiKeyHtml = renderToStaticMarkup(
    createElement(SettingsFieldRendererHost, {
      registry,
      componentBindings: [{ part_id: "api_key_setup", component: "ApiKeySetupField" }],
      field: {
        id: "provider_key",
        label: "Provider Key",
        type: "api_key_setup",
        part_id: "api_key_setup",
      } as TemplateSettingsField,
      sectionId: "providers",
      value: null,
      onChange: () => undefined,
      fallbackRenderer: () => createElement("span", null, "fallback"),
    }),
  );
  const providerHtml = renderToStaticMarkup(
    createElement(SettingsFieldRendererHost, {
      registry,
      field: {
        id: "provider",
        label: "Provider",
        type: "provider_select",
        renderer: "provider_select.compact",
      } as TemplateSettingsField,
      sectionId: "providers",
      value: "google",
      onChange: () => undefined,
      fallbackRenderer: () => createElement("span", null, "fallback"),
    }),
  );

  assert.match(modelHtml, /data-renderer="model"/);
  assert.match(modelHtml, /preferred_model:google\/gemini/);
  assert.match(apiKeyHtml, /data-renderer="api-key"/);
  assert.match(apiKeyHtml, /provider_key/);
  assert.match(providerHtml, /data-renderer="provider"/);
  assert.match(providerHtml, /provider/);
});

test("builtin settings field renderer registry resolves template model_select renderer", () => {
  const registry = createSettingsFieldRendererRegistry(builtinSettingsFieldRendererEntries);
  const match = registry.resolve({
    id: "preferred_model_template",
    label: "Preferred Model",
    type: "model_select",
  } as TemplateSettingsField);

  assert.equal(match?.entry.id, "builtin-settings-model-select");
  assert.equal(match?.key, "model_select");
});

test("api_key_setup renderer actions target the rendered template field", () => {
  assert.equal(apiKeySetupTargetFieldId({
    id: "api_key_setup_template",
    label: "API Setup",
    type: "api_key_setup",
  } as TemplateSettingsField), "api_key_setup_template");
});

test("cleartext mobile QR flag only enables on explicit opt-in", () => {
  assert.equal(allowCleartextMobileQr({}), false);
  assert.equal(allowCleartextMobileQr({ VITE_RUMI_MOBILE_ALLOW_CLEARTEXT_QR: "0" }), false);
  assert.equal(allowCleartextMobileQr({ VITE_RUMI_MOBILE_ALLOW_CLEARTEXT_QR: "1" }), true);
});

test("SettingsModalRenderer renders template model_select with searchable model selector surface", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "models",
      locale: "en",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        {
          id: "models",
          label: "Models",
          fields: [
            {
              id: "preferred_model",
              label: "Preferred Model",
              type: "model_select",
              options: [
                {
                  value: "google/gemini-2.5-flash",
                  label: "Gemini 2.5 Flash",
                  provider_id: "google",
                  model_id: "gemini-2.5-flash",
                  configured: true,
                },
              ],
            } as TemplateSettingsField,
          ] as unknown as SettingsSection["fields"],
        },
      ],
      settingsValues: {
        models: {
          preferred_model: "google/gemini-2.5-flash",
        },
      },
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /data-settings-renderer="model_select"/);
  assert.match(html, /Gemini 2.5 Flash/);
  assert.doesNotMatch(html, /type="text"[^>]*google\/gemini-2\.5-flash/);
});

test("SettingsModalRenderer keeps everyday model slots visible and hides internal roles in standard mode", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "models",
      locale: "en",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        {
          id: "models",
          label: "Models",
          fields: [
            {
              id: "main_model",
              label: "Main Model",
              type: "model_select",
              options: [{ value: "provider/main", label: "Main Choice" }],
            } as TemplateSettingsField,
            {
              id: "lightweight_model",
              label: "Lightweight Model",
              type: "model_select",
              options: [{ value: "provider/fast", label: "Fast Choice" }],
            } as TemplateSettingsField,
            {
              id: "utility_models",
              label: "Utility Models",
              type: "textarea",
              advanced: true,
            } as TemplateSettingsField,
          ] as unknown as SettingsSection["fields"],
        },
      ],
      settingsValues: {
        models: {
          main_model: "provider/main",
          lightweight_model: "provider/fast",
          utility_models: { fast_reply: "provider/fast" },
        },
      },
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /Main Model/);
  assert.match(html, /Lightweight Model/);
  assert.match(html, /Main Choice/);
  assert.match(html, /Fast Choice/);
  assert.match(html, /Advanced settings are hidden/);
  assert.doesNotMatch(html, /Utility Models/);
  assert.equal((html.match(/data-settings-renderer="model_select"/g) ?? []).length, 2);
});

test("SettingsModalRenderer renders template slash command registration field", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "commands",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        {
          id: "commands",
          label: "Commands",
          fields: [
            {
              id: "registered_slash_commands",
              label: "Slash Commands",
              type: "slash_commands",
              renderer: "slash_commands",
              default: [],
            } as TemplateSettingsField,
          ] as unknown as SettingsSection["fields"],
        },
      ],
      settingsValues: {
        commands: {
          registered_slash_commands: [
            { name: "yolo", action: "toggle_yolo", aliases: ["go"], enabled: true },
          ],
        },
      },
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /data-settings-renderer="slash_commands"/);
  assert.match(html, /value="yolo"/);
  assert.match(html, /value="go"/);
  assert.match(html, /YOLO/);
});

test("SettingsModalRenderer keeps internal extension paths out of standard Pack settings", () => {
  const longTemplatePath = "/Users/demo/Library/Application Support/Rumi/extensions/external-custom/templates/very/deep/path/with/no-natural-breaks/ExternalCustomTemplateExtensionThatWouldOtherwiseOverflowColumns";
  const longProfilePath = "/Users/demo/Library/Application Support/Rumi/extensions/external-custom/profiles/another/very/deep/path/with/no-natural-breaks/ExternalCustomProfileExtensionThatWouldOtherwiseOverlap";
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "packs",
      locale: "en",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        {
          id: "external_custom",
          label: "External Custom",
          fields: [
            {
              id: "custom_template_path",
              label: "Template Extension Path",
              type: "readonly",
              control_center_section: "packs",
              default: longTemplatePath,
            } as TemplateSettingsField & Record<string, unknown>,
            {
              id: "custom_profile_paths",
              label: "Profile Extension Paths",
              type: "readonly",
              control_center_section: "packs",
              default: longProfilePath,
            } as TemplateSettingsField & Record<string, unknown>,
          ] as unknown as SettingsSection["fields"],
        },
      ],
      settingsValues: {
        external_custom: {
          custom_template_path: longTemplatePath,
          custom_profile_paths: longProfilePath,
        },
      },
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /Advanced settings are hidden/);
  assert.doesNotMatch(html, /Template Extension Path/);
  assert.doesNotMatch(html, /Profile Extension Paths/);
  assert.doesNotMatch(html, /ExternalCustomTemplateExtensionThatWouldOtherwiseOverflowColumns/);
  assert.doesNotMatch(html, /ExternalCustomProfileExtensionThatWouldOtherwiseOverlap/);
});

test("Settings Profiles presents active/default routing and keeps profile secrets out of markup", () => {
  const rawSecret = ["profile", "plain", "secret"].join("-");
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "profiles",
      locale: "en",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        {
          id: "profiles",
          label: "Profiles",
          fields: [
            { id: "profiles", label: "Profiles", type: "json" },
            { id: "active_profile", label: "Active profile", type: "text" },
            { id: "default_profile", label: "Default profile", type: "text" },
          ] as unknown as SettingsSection["fields"],
        },
      ],
      settingsValues: {
        profiles: {
          profiles: [
            {
              profile_id: "work/deep-focus",
              display_name: "Deep Focus",
              description: "Research and synthesis",
              role: "Long-form analysis",
              preferred_model: "openai/gpt-4.1",
              api_key: rawSecret,
              credential_ref: "RUMIAPI_OPENAI_PRIMARY",
              editable: true,
            },
            {
              profile_id: "work/fast",
              display_name: "Fast Draft",
              preferred_model: "local/qwen",
              editable: true,
            },
          ],
          active_profile: "work/deep-focus",
          default_profile: "work/fast",
        },
        models: {
          model_api_routes: "openai/gpt-4.1: openai/primary\n",
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
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /data-settings-profile-panel/);
  assert.match(html, /Profile workspace/);
  assert.match(html, /Deep Focus/);
  assert.match(html, /Fast Draft/);
  assert.match(html, /Active profile route/);
  assert.match(html, /openai\/gpt-4\.1/);
  assert.match(html, /openai\/primary/);
  assert.match(html, /Create profile/);
  assert.match(html, /Duplicate/);
  assert.match(html, /Rename/);
  assert.doesNotMatch(html, new RegExp(rawSecret));
  assert.doesNotMatch(html, /data-settings-field="profiles\.(?:profiles|active_profile|default_profile)"/);
});

test("Settings keeps the modal header compact", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "profiles",
      locale: "en",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [],
      settingsValues: {},
      backendConnectionState: "offline",
      backendConnectionNote: "Backend reconnect is pending.",
      saveState: {
        status: "error",
        dirtyKeys: ["profiles.profiles"],
        message: "Profile changes were not confirmed.",
      },
      loadState: {
        status: "error",
        message: "Settings refresh failed.",
      },
      onRetryLoad: () => undefined,
      onRetrySave: () => undefined,
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /id="rumi-settings-dialog-title"/);
  assert.match(html, />Settings</);
  assert.doesNotMatch(html, /rumi-settings-dialog-description/);
  assert.doesNotMatch(html, /Backend reconnect is pending/);
});

test("slash command settings keep unsaved empty rows with stable row ids", () => {
  let nextId = 0;
  const rows = slashCommandDraftRowsFromValue([], () => `row-${++nextId}`);
  const withEmptyRow = appendEmptySlashCommandDraft(rows, "row-new");

  assert.equal(withEmptyRow.length, 1);
  assert.equal(withEmptyRow[0].rowId, "row-new");
  assert.equal(withEmptyRow[0].name, "");
  assert.deepEqual(serializeSlashCommandDrafts(withEmptyRow), []);

  const namedRows = withEmptyRow.map((row) => ({ ...row, name: "ship" }));
  assert.equal(namedRows[0].rowId, "row-new");
  assert.deepEqual(serializeSlashCommandDrafts(namedRows), [
    { name: "ship", action: "toggle_yolo", aliases: [], description: "", enabled: true },
  ]);
});

test("SettingsModalRenderer hides ambient detail fields until finger recording is enabled", () => {
  const sections = [
    {
      id: "ambient",
      label: "Ambient",
      fields: [
        {
          id: "ambient.monitor.enabled",
          label: "指で録音",
          type: "toggle",
          default: false,
        },
        {
          id: "ambient.camera.lock",
          label: "カメラ",
          type: "device_lock",
          renderer: "device_lock",
          visible_when: { field: "ambient.monitor.enabled", truthy: true },
          lock_message: "カメラが見つかりません。",
        },
        {
          id: "ambient.routing.model",
          label: "Ambient Send Model",
          type: "model_select",
          renderer: "model_select",
          visible_when: { field: "ambient.monitor.enabled", truthy: true },
        },
      ] as unknown as SettingsSection["fields"],
    },
  ];

  const offHtml = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "ambient",
      catalog: { sidebar: { filters: [], items: [] }, settings: { sections: [], values: {} }, chat_rendering: { renderers: [] }, extension_points: [] },
      health: null,
      previewsCount: 0,
      settingsSections: sections,
      settingsValues: { ambient: { "ambient.monitor.enabled": false } },
      onClose: () => undefined,
      onOpenSection: () => undefined,
      onSettingChange: () => undefined,
    }),
  );
  assert.match(offHtml, /指で録音/);
  assert.doesNotMatch(offHtml, /Ambient Send Model/);
  assert.doesNotMatch(offHtml, /data-settings-renderer="device_lock"/);

  const onHtml = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "ambient",
      catalog: { sidebar: { filters: [], items: [] }, settings: { sections: [], values: {} }, chat_rendering: { renderers: [] }, extension_points: [] },
      health: null,
      previewsCount: 0,
      settingsSections: sections,
      settingsValues: { ambient: { "ambient.monitor.enabled": true } },
      onClose: () => undefined,
      onOpenSection: () => undefined,
      onSettingChange: () => undefined,
    }),
  );
  assert.match(onHtml, /Ambient Send Model/);
  assert.match(onHtml, /data-settings-renderer="device_lock"/);
});

test("SettingsModalRenderer renders template api_key_setup with setup control", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "apis",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        {
          id: "apis",
          label: "APIs",
          fields: [
            {
              id: "api_key_setup_template",
              label: "API Key Setup",
              type: "api_key_setup",
              provider_id: "openai",
            } as TemplateSettingsField,
          ] as unknown as SettingsSection["fields"],
        },
      ],
      settingsValues: {
        apis: {
          api_keys: [
            {
              provider_id: "openai",
              label: "OpenAI",
              apis: [{ api_id: "main", name: "main", configured: true }],
            },
          ],
        },
      },
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /data-settings-renderer="api_key_setup"/);
  assert.match(html, /openai:main:\*\*\*/);
  assert.doesNotMatch(html, />APIキーを追加</);
  assert.match(html, /placeholder="openai API key"/);
  assert.match(html, />Save</);
});

test("Connections API credential template excludes AI provider keys", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "apis",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        {
          id: "apis",
          label: "Connections",
          fields: [
            {
              id: "api_key_setup_template",
              label: "API Keys / Tokens",
              type: "api_key_setup",
              provider_id: "line",
              provider_scope: "non_llm",
            } as unknown as TemplateSettingsField,
          ] as unknown as SettingsSection["fields"],
        },
      ],
      settingsValues: {
        apis: {
          api_keys: [
            {
              provider_id: "openai",
              label: "OpenAI",
              kind: "llm",
              apis: [{ api_id: "main", name: "AI key", kind: "llm", configured: true }],
            },
            {
              provider_id: "line",
              label: "LINE",
              kind: "custom",
              apis: [{ api_id: "channel", name: "LINE token", kind: "custom", configured: true }],
            },
          ],
        },
      },
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /data-provider-scope="non_llm"/);
  assert.match(html, /line:channel:\*\*\*/);
  assert.doesNotMatch(html, /openai:main:\*\*\*/);
});

test("Models places AI API registration before model API connections", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "models",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        {
          id: "models",
          label: "Models",
          fields: [
            {
              id: "main_model",
              label: "Main Model",
              type: "select",
              options: [{ value: "openai/gpt-4.1", label: "GPT-4.1" }],
            },
            {
              id: "model_api_routes",
              label: "Model API Variants",
              type: "model_api_routes",
              renderer: "model_routing",
              options: [{ value: "openai/gpt-4.1", label: "GPT-4.1", provider_id: "openai" }],
              api_keys: [
                {
                  provider_id: "openai",
                  label: "OpenAI",
                  kind: "llm",
                  apis: [{ api_id: "main", name: "AI key", kind: "llm", configured: true }],
                },
              ],
            } as TemplateSettingsField,
          ],
        },
        {
          id: "apis",
          label: "APIs",
          fields: [
            {
              id: "api_keys",
              label: "API Keys / Tokens",
              type: "api_key_setup",
              renderer: "api_key_setup",
              provider_scope: "non_llm",
              api_keys: [
                {
                  provider_id: "openai",
                  label: "OpenAI",
                  kind: "llm",
                  apis: [{ api_id: "main", name: "AI key", kind: "llm", configured: true }],
                },
                {
                  provider_id: "line",
                  label: "LINE",
                  kind: "custom",
                  apis: [{ api_id: "channel", name: "LINE token", kind: "custom", configured: true }],
                },
              ],
            } as unknown as TemplateSettingsField,
          ] as unknown as SettingsSection["fields"],
        },
      ] as SettingsSection[],
      settingsValues: {
        models: {
          main_model: "openai/gpt-4.1",
          model_api_routes: "openai/gpt-4.1: openai/main",
        },
        apis: {
          api_keys: [
            {
              provider_id: "openai",
              label: "OpenAI",
              kind: "llm",
              apis: [{ api_id: "main", name: "AI key", kind: "llm", configured: true }],
            },
            {
              provider_id: "line",
              label: "LINE",
              kind: "custom",
              apis: [{ api_id: "channel", name: "LINE token", kind: "custom", configured: true }],
            },
          ],
        },
      },
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /data-provider-scope="llm"/);
  assert.match(html, /openai:main:\*\*\*/);
  assert.doesNotMatch(html, /line:channel:\*\*\*/);
  assert.ok(
    html.indexOf('data-settings-field="apis.api_keys"')
      < html.indexOf('data-settings-field="models.model_api_routes"'),
  );
});

test("CredentialTransferModal keeps transfer device-bound and credential-free", () => {
  const html = renderToStaticMarkup(
    createElement(CredentialTransferModal, {
      providerId: "anthropic",
      providerLabel: "Anthropic",
      apiId: "main",
      onClose: () => undefined,
    }),
  );

  assert.match(html, /暗号化して端末へ転送/);
  assert.match(html, /確認した1台だけ/);
  assert.doesNotMatch(html, /Rumi Mobile QR/);
  assert.doesNotMatch(html, /sk-ant-test/);
  assert.match(html, /Anthropic/);
});

test("SettingsModalRenderer renders template model_api_routes through registered model routing renderer", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "models",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        {
          id: "models",
          label: "Models",
          fields: [
            {
              id: "model_api_routes",
              label: "Model API Variants",
              type: "model_api_routes",
              renderer: "model_routing",
              options: [
                {
                  value: "google/gemini-2.5-flash",
                  label: "Gemini 2.5 Flash",
                  provider_id: "google",
                  model_id: "gemini-2.5-flash",
                  configured: true,
                },
              ],
              api_keys: [
                {
                  provider_id: "google",
                  label: "Google",
                  apis: [{ api_id: "main", name: "main", configured: true }],
                },
              ],
            } as TemplateSettingsField,
          ] as unknown as SettingsSection["fields"],
        },
      ],
      settingsValues: {
        models: {
          preferred_model: "google/gemini-2.5-flash",
          model_api_routes: "google/gemini-2.5-flash: google/main",
        },
      },
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /data-settings-renderer="model_routing"/);
  assert.match(html, /data-model-search-picker="settings"/);
  assert.match(html, /Gemini 2\.5 Flash/);
  assert.match(html, /google\/main/);
  assert.match(html, /min-h-11/);
  assert.match(html, /API keyを追加/);
  assert.doesNotMatch(html, /data-settings-routing-overview/);
});

test("SettingsModalRenderer renders continuity handoff controls", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "continuity",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        {
          id: "continuity",
          label: "Continuity",
          fields: [
            {
              id: "handoff",
              label: "Cloud / Device Handoff",
              type: "continuity",
              default: {
                sandbox_id: "sandbox-demo",
                mode: "move",
                destination_node_id: "node-workstation",
                route_id: "route-openai",
                local_node: {
                  node_id: "node-source",
                  display_name: "MacBook",
                  destination_kind: "source",
                  online: true,
                },
                nodes: [
                  {
                    node_id: "node-workstation",
                    display_name: "Workstation",
                    destination_kind: "cloud_node",
                    platform: "Linux",
                    online: true,
                  },
                ],
                routes: [
                  {
                    route_id: "route-openai",
                    provider_id: "openai",
                    api_id: "primary",
                    model_id: "gpt-4.1",
                    qualified_route: "openai/primary/gpt-4.1",
                    endpoint_class: "public_https",
                    credential_ref: "RUMIAPI_OPENAI_PRIMARY",
                    portable: true,
                  },
                ],
                operations: [
                  {
                    operation_id: "handoff-demo",
                    status: "COMPLETED",
                    sandbox_id: "sandbox-demo",
                    destination_node_id: "node-workstation",
                  },
                ],
              },
            } as TemplateSettingsField,
          ] as unknown as SettingsSection["fields"],
        },
      ],
      settingsValues: {
        continuity: {
          handoff: {
            sandbox_id: "sandbox-demo",
            destination_node_id: "node-workstation",
            route_id: "route-openai",
            mode: "move",
          },
        },
      },
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /data-settings-renderer="continuity"/);
  assert.match(html, /Workstation/);
  assert.match(html, /openai\/primary\/gpt-4\.1/);
  assert.match(html, /handoff-demo/);
  assert.match(html, /Current primary/);
  assert.match(html, /Source/);
  assert.match(html, /Destination/);
  assert.match(html, /planning-only/);
  assert.match(html, /Source primary/);
  assert.match(html, /Review plan/);
  assert.match(html, /Completed/);
  assert.doesNotMatch(html, /Return to this device/);
  assert.doesNotMatch(html, /Switch primary/);
  assert.doesNotMatch(html, /Move primary/);
  assert.match(html, /Advanced routing details/);
  assert.doesNotMatch(html, /COMPLETED/);
});

test("Settings > Tools keeps selector internals out of standard mode", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "tools",
      catalog: {
        sidebar: {
          filters: [],
          items: [
            {
              id: "vision_tool",
              label: "Vision Tool",
              category: "tool",
              description: "Inspect images",
              tool_info: {
                requires_approval: true,
                requires_model_capabilities: ["model.image_input"],
                attachment_policy: "images_only",
              },
            },
          ],
        },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        {
          id: "tools",
          label: "機能と接続",
          fields: [
            { id: "default_mode", label: "既定の使い方", type: "select", default: "auto", options: [{ value: "auto", label: "自動で選ぶ" }] },
          ],
        },
      ],
      settingsValues: {
        tools: {
          disabled_tool_ids: [],
          hidden_tool_ids: [],
          tool_permission_overrides: {},
        },
      },
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /基本/);
  assert.match(html, /権限/);
  assert.match(html, /接続/);
  assert.doesNotMatch(html, /高度な設定/);
  assert.match(html, /既定の使い方/);
  assert.match(html, /自動で選ぶ/);
});

test("Settings > Tools defaults to the tool experience overview", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "tools",
      catalog: {
        sidebar: {
          filters: [],
          items: [
            {
              id: "vision_tool",
              label: "Vision Tool",
              category: "tool",
              description: "Inspect images",
              tool_info: {
                requires_approval: true,
                requires_model_capabilities: ["model.image_input"],
                attachment_policy: "images_only",
              },
            },
          ],
        },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        {
          id: "tools",
          label: "Tools",
          fields: [
            { id: "keep_selected_tools_after_send", label: "Keep Selected Tools", type: "toggle", default: true },
          ],
        },
      ],
      settingsValues: {
        tools: {
          keep_selected_tools_after_send: true,
          disabled_tool_ids: [],
          hidden_tool_ids: [],
        },
      },
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /基本/);
  assert.match(html, /権限/);
  assert.doesNotMatch(html, />1件</);
  assert.match(html, /選んだ機能を回答内に表示/);
  assert.doesNotMatch(html, /Tool details/);
});

test("settings surface pinned placements render in the modal", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "models",
      locale: "en",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        { id: "models", label: "Models", description: "Model settings", fields: [] },
      ],
      settingsValues: {
        sidebar: {
          ui_placements: [{ id: "settings-section:models", surface: "settings" }],
        },
      },
      onClose: () => undefined,
      onOpenSection: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /Pinned placements/);
  assert.match(html, /Models/);
  assert.match(html, /このセクションを開く/);
});

test("preferred model visibility keeps configured models beyond the old first-40 cutoff", () => {
  const filler = Array.from({ length: 45 }, (_, index) => makeModelOption(index));
  const zenOption = {
    value: "opencode-zen/minimax-m3-free",
    label: "OpenCode Zen / MiniMax M3 Free via OpenCode Zen",
    provider_id: "opencode-zen",
    provider_display_name: "OpenCode Zen",
    model_id: "minimax-m3-free",
    configured: true,
    supports_tool_calling: true,
    supports_thinking: true,
    supports_vision: true,
  };

  const visible = buildVisibleModelOptions({
    options: [...filler, zenOption],
    selected: null,
    remoteOptions: [],
    query: "",
  });

  assert.equal(visible.length, 46);
  assert(visible.some((option) => option.value === zenOption.value));
});

test("operations company model allowlist renders as an addable selection list", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "operations_company",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        {
          id: "operations_company",
          label: "Operations Company",
          fields: [
            {
              id: "model_allowlist",
              label: "Model Allowlist",
              type: "textarea",
              default: "stub/default\ngoogle/gemini-2.5-flash",
            },
          ],
        },
      ],
      settingsValues: {
        operations_company: {
          model_allowlist: "stub/default\ngoogle/gemini-2.5-flash",
        },
      },
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /モデルを追加/);
  assert.match(html, /stub\/default/);
  assert.match(html, /google\/gemini-2.5-flash/);
  assert.doesNotMatch(html, /<textarea[^>]*>stub\/default/);
});

test("MiMo model allowlist also uses the catalog picker instead of raw model IDs", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "mimo_coding_company",
      locale: "ja",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [{
        id: "mimo_coding_company",
        label: "MiMo Coding Company",
        fields: [{
          id: "model_allowlist",
          label: "Model Allowlist",
          type: "textarea",
          default: "xiaomi-token-plan-sgp/mimo-v2.5-pro\nstub/default",
        }],
      }],
      settingsValues: {
        mimo_coding_company: {
          model_allowlist: "xiaomi-token-plan-sgp/mimo-v2.5-pro\nstub/default",
        },
      },
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /MiMo Coding/);
  assert.match(html, /モデルを追加/);
  assert.match(html, /xiaomi-token-plan-sgp\/mimo-v2.5-pro/);
  assert.doesNotMatch(html, /<textarea[^>]*>xiaomi-token-plan-sgp/);
});

test("settings system info renders viewer version and macOS permissions", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "system_info",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        { id: "system_info", label: "System Info", description: "Version and permission status", fields: [] },
      ],
      settingsValues: {},
      desktopSystemInfo: {
        source: "viewer_tauri",
        reliable: true,
        app_name: "Tobkiri",
        display_version: "beta 1.0.0",
        viewer_version: "1.0.0-beta.1",
        build_channel: "beta",
        platform: "macos",
        platform_release: "15.0",
        permission_subject: "Tobkiri Launcher",
        host_broker: {
          enabled: true,
          available: true,
          status: "running",
        },
        permissions: [
          {
            id: "screen_recording",
            label: "Screen Recording",
            status: "missing",
            granted: false,
            detail: "Allows screen capture.",
            settings_hint: "System Settings > Privacy & Security > Screen Recording",
          },
        ],
      },
      onClose: () => undefined,
      onOpenSection: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /beta 1\.0\.0/);
  assert.match(html, /1\.0\.0-beta\.1/);
  assert.match(html, /macOSの承認対象は Tobkiri Launcher です/);
  assert.match(html, /macOS Permissions/);
  assert.match(html, /Screen Recording/);
  assert.match(html, /Missing/);
});

test("settings system info does not show missing permissions when viewer state is unreliable", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "system_info",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        { id: "system_info", label: "System Info", description: "Version and permission status", fields: [] },
      ],
      settingsValues: {},
      desktopSystemInfo: {
        source: "fallback",
        reliable: false,
        app_name: "Tobkiri",
        display_version: "",
        viewer_version: "",
        build_channel: "beta",
        platform: "darwin",
        platform_release: "15.0",
        permission_subject: "Tobkiri Launcher",
        host_broker: {
          enabled: false,
          available: false,
          status: "unavailable",
        },
        permissions: [
          {
            id: "viewer_host",
            label: "Tobkiri Launcher",
            status: "missing",
            granted: false,
            detail: "Fallback row should not be rendered.",
            settings_hint: "Open Tobkiri Launcher.",
          },
        ],
      },
      onClose: () => undefined,
      onOpenSection: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /Viewer permission status is unverified/);
  assert.doesNotMatch(html, /macOS Permissions/);
  assert.doesNotMatch(html, /Missing/);
  assert.doesNotMatch(html, /Fallback row should not be rendered/);
});

test("settings system info shows browser context message when info is null", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "system_info",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        { id: "system_info", label: "System Info", description: "Version and permission status", fields: [] },
      ],
      settingsValues: {},
      desktopSystemInfo: null,
      onClose: () => undefined,
      onOpenSection: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /権限状態を取得できませんでした/);
  assert.match(html, /Tobkiri Launcherを起動し/);
  assert.doesNotMatch(html, /Rumi Defaultspack\.app/);
});

test("settings accounts prelude renders actionable Google and disabled Cloudflare states", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "accounts",
      locale: "en",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        { id: "accounts", label: "Accounts", fields: [] },
      ],
      settingsValues: {
        apis: {
          api_keys: [
            {
              provider_id: "google",
              oauth: {
                supported: true,
                backend_supported: true,
                client_configured: true,
                connect_enabled: true,
                connection_status: "not_connected",
                status_label: "Ready to connect",
                scope_mode: "google_gmail_labels",
                scope_modes: [
                  {
                    id: "google_identity",
                    label: "Google identity",
                    description: "Basic sign-in identity only.",
                    scopes: ["openid", "email", "profile"],
                    services: ["identity"],
                  },
                  {
                    id: "google_drive",
                    label: "Google Drive selected files",
                    description: "Drive file scope.",
                    scopes: ["openid", "email", "profile", "https://www.googleapis.com/auth/drive.file"],
                    services: ["identity", "drive_file"],
                  },
                  {
                    id: "google_gmail_labels",
                    label: "Gmail labels",
                    description: "Labels only.",
                    scopes: ["openid", "email", "profile", "https://www.googleapis.com/auth/gmail.labels"],
                    services: ["identity", "gmail_labels"],
                  },
                  {
                    id: "google_gmail_metadata",
                    label: "Gmail metadata/search",
                    description: "Restricted metadata mode.",
                    scopes: ["openid", "email", "profile", "https://www.googleapis.com/auth/gmail.metadata"],
                    services: ["identity", "gmail_metadata"],
                    restricted: true,
                    warning: "Restricted Gmail scopes require explicit review.",
                  },
                ],
                scopes: [
                  "openid",
                  "email",
                  "profile",
                  "https://www.googleapis.com/auth/drive.file",
                  "https://www.googleapis.com/auth/gmail.labels",
                ],
              },
            },
            {
              provider_id: "cloudflare",
              oauth: {
                backend_supported: false,
                connect_enabled: false,
                connection_status: "missing_scope_config",
                status_label: "Missing scope config",
                disabled_reason: "Configure self-host OAuth",
                provisioning: {
                  environment_status: "blocked",
                  sandbox_ready: false,
                  pages_ready: true,
                  stable_pc_tunnel_ready: false,
                  pc_tool_bridge_ready: false,
                  constraints: {
                    cloudflare_sandbox_requires_workers_paid: true,
                    pages_dev_is_not_a_pc_tunnel_hostname: true,
                    all_tools_cloudflare_native_supported: false,
                    pc_local_tools_require_pc_bridge: true,
                    wrangler_diagnostics_require_explicit_command_or_local_install: true,
                  },
                  blockers: [
                    {
                      code: "CLOUDFLARE_WRANGLER_MISSING",
                      message:
                        "Set RUMI_WRANGLER_COMMAND or run npm install in a Cloudflare scaffold so its pinned node_modules/.bin/wrangler is available.",
                    },
                    { code: "CLOUDFLARE_CONTAINERS_PAID_PLAN_REQUIRED", message: "Cloudflare Containers require the Workers Paid plan." },
                    { code: "CLOUDFLARE_PC_TUNNEL_ENV_NOT_CONFIGURED", message: "Set a named Cloudflare Tunnel hostname." },
                  ],
                },
              },
            },
          ],
        },
      },
      onClose: () => undefined,
      onOpenSection: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /Connect selected mode/);
  assert.match(html, /Ready to connect/);
  assert.match(html, /Google identity/);
  assert.match(html, /Google Drive selected files/);
  assert.match(html, /Gmail labels/);
  assert.match(html, /Gmail metadata\/search/);
  assert.match(html, /Restricted/);
  assert.match(html, /Restricted Gmail scopes require explicit review/);
  assert.match(html, /<input[^>]*(value="google_gmail_labels"[^>]*checked=""|checked=""[^>]*value="google_gmail_labels")/);
  assert.match(html, /https:\/\/www\.googleapis\.com\/auth\/gmail\.labels/);
  assert.match(html, /Connect Cloudflare/);
  assert.match(html, /Missing scope config/);
  assert.match(html, /Official app required|Hosted broker flows|official hosted broker/);
  assert.match(html, /Configure self-host OAuth/);
  assert.match(html, /title="Configure self-host OAuth"/);
  assert.match(html, /Cloudflare runtime/);
  assert.match(html, /Sandbox \+ PC bridge/);
  assert.match(html, /Run diagnostics/);
  assert.match(html, /Sandbox: Workers Paid plan/);
  assert.match(html, /pages\.dev is not a PC tunnel/);
  assert.match(html, /Wrangler: explicit command or local install/);
  assert.match(html, /Set RUMI_WRANGLER_COMMAND/);
  assert.match(html, /node_modules\/\.bin\/wrangler/);
  assert.match(html, /Cloudflare Containers require the Workers Paid plan/);
  assert.match(html, /src="data:image\/svg\+xml,%3Csvg/);
  assert.doesNotMatch(html, />Not connected</);
});

test("settings accounts prelude renders Codex token credential without raw token", () => {
  const rawToken = ["codex", "renderer", "token"].join("-");
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "accounts",
      locale: "en",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        { id: "accounts", label: "Accounts", fields: [] },
      ],
      settingsValues: {
        accounts_connections: {
          providers: {
            codex: {
              configured: true,
              connected: true,
              token_configured: true,
              can_clear: true,
              connection_status: "connected",
              status_label: "Token saved",
              access_token: rawToken,
            },
          },
        },
      },
      onClose: () => undefined,
      onOpenSection: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /Codex access token/);
  assert.match(html, /Token saved/);
  assert.match(html, /Saved/);
  assert.match(html, /Update token/);
  assert.match(html, /Clear token/);
  assert.doesNotMatch(html, /Connect Codex/);
  assert.doesNotMatch(html, new RegExp(rawToken));
});

test("settings tools prelude renders Codex App Server status and controls", () => {
  const rawToken = ["codex", "hidden", "token"].join("-");
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "tools",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        { id: "tools", label: "Tools", fields: [] },
      ],
      settingsValues: {
        accounts_connections: {
          providers: {
            codex: {
              configured: true,
              token_configured: true,
              access_token: rawToken,
            },
          },
        },
        tools_mcp: {
          codex_app_server: {
            configured: true,
            enabled: true,
            transport: "websocket_loopback",
            connection_status: "configured",
            status_label: "Configured",
            base_url: "http://127.0.0.1:7331",
            websocket_url: "ws://127.0.0.1:7331/ws",
            unix_socket_path: "",
            loopback: true,
            auth_required: false,
            auth_configured: true,
            auth_source: "file",
            auth_kind: "ws_token",
            ws_token_file: "/Users/haru/.config/rumi/codex-app-server.token",
            shared_secret_file: "",
            account: {
              provider_id: "codex",
              provider_kind: "codex",
              type: "chatgpt",
              auth_method: "chatgpt_account",
              auth_method_label: "ChatGPT account",
              account_label: "rumi-user@example.test",
              email: "rumi-user@example.test",
              plan_type: "prolite",
            },
            tool_source: { status: "configured" },
            automation_endpoint: { status: "configured" },
          },
        },
      },
      onClose: () => undefined,
      onOpenSection: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /Codex App Server/);
  assert.match(html, /Tool source/);
  assert.match(html, /Automation/);
  assert.match(html, /http:\/\/127\.0\.0\.1:7331/);
  assert.match(html, /ws:\/\/127\.0\.0\.1:7331\/ws/);
  assert.match(html, /websocket_loopback/);
  assert.match(html, /ws_token via file/);
  assert.match(html, /Connected Codex provider via ChatGPT account: rumi-user@example.test/);
  assert.match(html, /Save config/);
  assert.match(html, /Probe/);
  assert.doesNotMatch(html, new RegExp(rawToken));
  assert.doesNotMatch(html, /Connected ChatGPT account/);
});

test("settings help pane uses reported active profile with fallback when absent", () => {
  const withProfile = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "models",
      locale: "en",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [{ id: "models", label: "Models", fields: [] }],
      settingsValues: { profiles: { active_profile: "workbench/deep-focus" } },
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );
  const withoutProfile = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "models",
      locale: "en",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [{ id: "models", label: "Models", fields: [] }],
      settingsValues: {},
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(withProfile, /workbench\/deep-focus/);
  assert.doesNotMatch(withProfile, />default</);
  assert.match(withoutProfile, /No active profile reported/);
  assert.doesNotMatch(withoutProfile, />default</);
});

test("SettingsModalRenderer normalizes object profile references for the active profile route", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "models",
      locale: "en",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: null,
      previewsCount: 0,
      settingsSections: [
        {
          id: "profiles",
          label: "Profiles",
          fields: [
            { id: "profiles", label: "Profiles", type: "json" },
            { id: "active_profile", label: "Active profile", type: "text" },
          ] as unknown as SettingsSection["fields"],
        },
        { id: "models", label: "Models", fields: [] },
      ],
      settingsValues: {
        profiles: {
          profiles: [
            { profile_id: "work/base", display_name: "Base", preferred_model: "local/base" },
            { profile_id: "work/deep-focus", display_name: "Deep Focus", preferred_model: "openai/gpt-4.1" },
          ],
          active_profile: { profile_id: "work/deep-focus", display_name: "Deep Focus" },
        },
        models: {
          model_api_routes: "openai/gpt-4.1: openai/primary",
        },
        apis: {
          api_keys: [
            { provider_id: "openai", apis: [{ api_id: "primary", credential_ref: "RUMIAPI_OPENAI_PRIMARY" }] },
          ],
        },
      },
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /Active profile route/);
  assert.match(html, /Deep Focus/);
  assert.match(html, /work\/deep-focus/);
  assert.doesNotMatch(html, /\[object Object\]/);
});

test("Settings modal exposes localized dialog semantics and task-oriented Japanese copy", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "general",
      locale: "ja",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: { status: "ok", pack: "defaultspack", ts: "" },
      previewsCount: 0,
      settingsSections: [{
        id: "general",
        label: "General",
        fields: [{ id: "composer_placeholder", label: "Composer Placeholder", type: "text", help: "composer placeholder" }],
      } as SettingsSection],
      settingsValues: {},
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /role="dialog"/);
  assert.match(html, /aria-modal="true"/);
  assert.match(html, /aria-labelledby="rumi-settings-dialog-title"/);
  assert.doesNotMatch(html, /aria-describedby="rumi-settings-dialog-description"/);
  assert.match(html, /aria-label="設定を閉じる"/);
  assert.match(html, />設定<\/h2>/);
  assert.match(html, /入力欄の案内文/);
  assert.doesNotMatch(html, />Composer Placeholder</);
  assert.doesNotMatch(html, /バックエンド登録情報/);
});

test("Japanese Accounts modal does not expose English connection implementation copy", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "accounts",
      locale: "ja",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: { status: "ok", pack: "defaultspack", ts: "" },
      previewsCount: 0,
      settingsSections: [{
        id: "accounts_connections",
        label: "Accounts & Connections",
        fields: [],
      } as SettingsSection],
      settingsValues: {},
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /ログイン、認証情報、権限を分けて管理します/);
  assert.match(html, /Gmailの検索とメタデータ/);
  assert.match(html, /認証情報を読み込んで保存/);
  assert.match(html, /設定の提供元/);
  assert.match(html, /パックや外部サービスから追加される設定は、利用可能になるとここに表示されます。/);
  assert.doesNotMatch(html, /Client config needed|Credential needed|Token needed/);
  assert.doesNotMatch(html, /Connect selected mode|Configure self-host OAuth|Import credential JSON/);
  assert.doesNotMatch(html, /Restricted Gmail scopes|Settings placement candidates|Accounts &amp; Connections/);
  assert.doesNotMatch(html, /Pack or provider contributions for this section will appear here after registry validation/);
});

test("English Settings empty section keeps registry contribution guidance", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsModalRenderer, {
      isOpen: true,
      activeSectionId: "accounts",
      locale: "en",
      catalog: {
        sidebar: { filters: [], items: [] },
        settings: { sections: [], values: {} },
        chat_rendering: { renderers: [] },
        extension_points: [],
      },
      health: { status: "ok", pack: "defaultspack", ts: "" },
      previewsCount: 0,
      settingsSections: [{
        id: "accounts_connections",
        label: "Accounts & Connections",
        fields: [],
      } as SettingsSection],
      settingsValues: {},
      onClose: () => undefined,
      onSettingChange: () => undefined,
    }),
  );

  assert.match(html, /Pack or provider contributions for this section will appear here after registry validation\./);
  assert.doesNotMatch(html, /パックや外部サービスから追加される設定/);
});
