import test from "node:test";
import assert from "node:assert/strict";

import type { SettingsSection } from "../lib/api";
import { settingsFieldSearchText } from "../lib/settingsSearch";
import {
  externalInputPolicySummary,
  externalInputSetupGuide,
} from "../features/settings/externalInputPresentation";
import {
  buildCodexAppServerPrelude,
  buildAccountConnectionPrelude,
  buildControlCenterSections,
  controlCenterSectionForField,
  controlCenterSectionMeta,
  localizedSettingsSourceLabel,
  mapSettingsSectionId,
  safeSettingsLabel,
} from "./controlCenter";

test("settings control center keeps the required section order", () => {
  const sections = buildControlCenterSections([]);
  assert.deepEqual(sections.map((section) => section.label), [
    "AI Assistant",
    "Models",
    "Display & Input",
    "Connections",
    "Features",
    "Tools",
    "Automation & Permissions",
    "Safety & Data",
    "Profiles",
    "Packs & Extensions",
    "Advanced Settings",
    "Diagnostics & Support",
  ]);
});

test("external input guidance and policy follow the selected provider", () => {
  const discordGuide = externalInputSetupGuide({
    provider: "discord",
    setup_steps: [
      "Select Discord Input.",
      "Save the Discord Public Key.",
      "Enable discord-main.",
    ],
  }, "discord");

  assert.equal(
    discordGuide,
    "1. Select Discord Input.\n2. Save the Discord Public Key.\n3. Enable discord-main.",
  );
  assert.doesNotMatch(discordGuide, /LINE|line-main/);
  assert.match(externalInputPolicySummary("discord"), /^discord\.production:/);
  assert.match(externalInputPolicySummary("slack"), /^slack\.production:/);
  assert.match(externalInputPolicySummary("generic"), /^Generic endpoint policy:/);
});

test("external input guidance includes the built-in generic webhook path", () => {
  const guide = externalInputSetupGuide(null, "generic");

  assert.match(guide, /Generic Webhook Input/);
  assert.match(guide, /\/api\/webhooks\/inbound\/\{webhook_id\}/);
  assert.match(guide, /Webhook Shared Secret/);
});

test("control center canonical section ids round-trip through settings navigation", () => {
  for (const section of controlCenterSectionMeta("ja")) {
    assert.equal(mapSettingsSectionId(section.id), section.id);
  }
});

test("AI API setup is shared with models while connections keeps the source field", () => {
  const sections = buildControlCenterSections([
    {
      id: "models",
      label: "Models",
      fields: [
        { id: "provider_select", label: "Provider", type: "provider_select" },
        { id: "model_api_routes", label: "Model API Routes", type: "model_api_routes" },
      ],
    },
    {
      id: "apis",
      label: "APIs / Tokens",
      fields: [{ id: "api_keys", label: "API Keys / Tokens", type: "api_keys" }],
    },
  ] as SettingsSection[]);

  const modelsApi = sections.find((section) => section.id === "models_api");
  const connections = sections.find((section) => section.id === "accounts_connections");
  assert.deepEqual(modelsApi?.fields.map((field) => field.id), ["api_keys", "provider_select", "model_api_routes"]);
  const aiApiKeys = modelsApi?.fields.find((field) => field.sourceSectionId === "apis" && field.id === "api_keys");
  assert.equal(aiApiKeys?.type, "api_key_setup");
  assert.equal((aiApiKeys as unknown as Record<string, unknown>)?.provider_scope, "llm");
  assert.deepEqual(connections?.fields.map((field) => field.id), ["api_keys"]);
});

test("pack-owned model and tool choices use their user-facing destinations", () => {
  const operationsCompany = {
    id: "operations_company",
    label: "Operations Company",
    fields: [],
  } as SettingsSection;

  assert.equal(
    controlCenterSectionForField(operationsCompany, { id: "model_allowlist", label: "Model Allowlist", type: "textarea" }),
    "models_api",
  );
  assert.equal(
    controlCenterSectionForField(operationsCompany, { id: "tool_denylist", label: "Tool Denylist", type: "textarea" }),
    "tools_mcp",
  );
});

test("feature-owned model choices stay with their feature instead of the global model page", () => {
  const calendar = {
    id: "calendar",
    label: "Calendar",
    fields: [],
  } as SettingsSection;

  assert.equal(
    controlCenterSectionForField(calendar, { id: "agent_model", label: "Agent model", type: "select" }),
    "features",
  );
});

test("webhook and channel plumbing is advanced by default", () => {
  const sections = buildControlCenterSections([
    {
      id: "external_input",
      label: "External input",
      fields: [{ id: "input_endpoint_id", label: "Endpoint ID", type: "text" }],
    },
  ] as SettingsSection[]);

  const field = sections.find((section) => section.id === "accounts_connections")?.fields[0];
  assert.equal(field?.advanced, true);
});

test("manual runtime mode selection stays in the advanced settings surface", () => {
  const sections = buildControlCenterSections([
    {
      id: "general",
      label: "General",
      fields: [{
        id: "manual_runtime_mode_selection",
        label: "Manual Runtime Mode Selection",
        type: "toggle",
        default: false,
        advanced: true,
        control_center_section: "advanced",
      }],
    },
  ] as SettingsSection[], "ja");

  const field = sections.find((section) => section.id === "advanced")?.fields[0];
  assert.equal(field?.id, "manual_runtime_mode_selection");
  assert.equal(field?.label, "実行モードを手動選択できるようにする");
  assert.equal(field?.advanced, true);
  assert.equal(field?.default, false);
});

test("Japanese settings use task-oriented copy while preserving technical search aliases", () => {
  const sections = buildControlCenterSections([
    {
      id: "general",
      label: "General",
      fields: [
        { id: "composer_placeholder", label: "Composer Placeholder", type: "text", help: "composer placeholder" },
        { id: "language", label: "Language", type: "select", options: [{ value: "auto", label: "Auto" }] },
      ],
    },
    {
      id: "tools",
      label: "Tools",
      fields: [{ id: "semantic_backend", label: "Semantic backend", type: "select", advanced: true, options: [{ value: "embedding", label: "Embedding" }] }],
    },
  ] as SettingsSection[], "ja");

  assert.equal(sections.find((section) => section.id === "workspace_ui")?.label, "表示と入力");
  const workspaceFields = sections.find((section) => section.id === "workspace_ui")?.fields ?? [];
  assert.equal(workspaceFields.find((field) => field.id === "composer_placeholder")?.label, "入力欄の案内文");
  assert.equal(workspaceFields.find((field) => field.id === "language")?.options?.[0]?.label, "端末に合わせる");
  const semanticField = sections.find((section) => section.id === "tools_mcp")?.fields.find((field) => field.id === "semantic_backend");
  assert.equal(semanticField?.label, "機能候補の探し方");
  assert.equal(semanticField?.advanced, true);
  assert.match(settingsFieldSearchText(semanticField!), /semantic_backend/);
  assert.match(settingsFieldSearchText(semanticField!), /embedding/);
});

test("Japanese placement and provenance labels never expose raw registry copy", () => {
  assert.equal(localizedSettingsSourceLabel("general", "General", "ja"), "表示と操作");
  assert.equal(localizedSettingsSourceLabel("mimo_coding_company", "Internal Pack", "ja"), "MiMo Coding");
  assert.equal(localizedSettingsSourceLabel("external_input", "External Input", "ja"), "外部からの受信・Webhook");
  assert.equal(localizedSettingsSourceLabel("unknown_extension", "Internal Vector Registry", "ja"), "拡張機能の設定");
  assert.equal(localizedSettingsSourceLabel("general", "General", "en"), "General");
});

test("settings control center separates computer control from tools", () => {
  const source = {
    id: "tools",
    label: "Tools",
    fields: [
      { id: "computer_approval_mode", label: "Computer approval mode", type: "select" },
      { id: "mcp_servers", label: "MCP servers", type: "textarea" },
    ],
  } as SettingsSection;

  assert.equal(controlCenterSectionForField(source, source.fields[0]), "computer_automation");
  assert.equal(controlCenterSectionForField(source, source.fields[1]), "tools_mcp");
});

test("settings control center removes raw labels from normal UI", () => {
  assert.equal(safeSettingsLabel("mimo"), "Mimo model preset");
  assert.equal(safeSettingsLabel("mimo_model_preset"), "Mimo model preset");
  assert.equal(safeSettingsLabel("computer_use_gradient"), "Automation visual indicator");
  assert.equal(safeSettingsLabel("computer_use_gradient_enabled"), "Automation visual indicator");
  assert.equal(safeSettingsLabel("openrouter_auto_mode"), "OpenRouter auto routing");

  const sections = buildControlCenterSections([
    {
      id: "models",
      label: "Models",
      fields: [
        {
          id: "model_preset",
          label: "mimo",
          type: "select",
          options: [{ value: "openrouter_auto", label: "openrouter_auto" }],
        },
      ],
    },
  ]);
  const modelField = sections.find((section) => section.id === "models_api")?.fields[0];
  assert.equal(modelField?.label, "Mimo model preset");
  assert.equal(modelField?.options?.[0]?.label, "OpenRouter auto routing");
});

test("account connection prelude disables unsupported Cloudflare backend", () => {
  const cards = buildAccountConnectionPrelude({
    apis: {
      api_keys: [
        {
          provider_id: "cloudflare",
          oauth: {
            backend_supported: false,
            connect_enabled: false,
            connection_status: "missing_scope_config",
            status_label: "Missing scope config",
            disabled_reason: "Configure self-host OAuth",
          },
        },
      ],
    },
  });

  const cloudflare = cards.find((card) => card.providerId === "cloudflare");
  assert.equal(cloudflare?.canConnect, false);
  assert.equal(cloudflare?.connectAction, undefined);
  assert.equal(cloudflare?.status, "missing_scope_config");
  assert.equal(cloudflare?.disabledReason, "Configure self-host OAuth");
  assert.match(cloudflare?.officialAppDescription ?? "", /Official app required/);
});

test("account connection prelude shows Cloudflare fallback when provider exists without client config", () => {
  const cards = buildAccountConnectionPrelude({
    accounts_connections: {
      providers: {
        cloudflare: {
          supported: true,
          backend_supported: false,
          client_configured: false,
          connect_enabled: false,
          connection_status: "needs_official_app",
          status_label: "Official app required",
          disabled_reason: "Official app required",
        },
      },
    },
  });

  const cloudflare = cards.find((card) => card.providerId === "cloudflare");
  assert.equal(cloudflare?.label, "Cloudflare");
  assert.equal(cloudflare?.canConnect, false);
  assert.equal(cloudflare?.connectAction, undefined);
  assert.equal(cloudflare?.statusLabel, "Official app required");
  assert.match(cloudflare?.officialAppDescription ?? "", /Official app required/);
  assert.match(cloudflare?.selfHostDescription ?? "", /Self-host OAuth remains available/);
});

test("account connection prelude enables Cloudflare when self-host OAuth is ready", () => {
  const cards = buildAccountConnectionPrelude({
    accounts_connections: {
      providers: {
        cloudflare: {
          supported: true,
          backend_supported: true,
          client_configured: true,
          connect_enabled: true,
          connected: false,
          connection_status: "not_connected",
          status_label: "Ready to connect",
          scopes: ["account:read", "user:read"],
          provisioning: {
            environment_status: "blocked",
            sandbox_ready: false,
            stable_pc_tunnel_ready: false,
            pc_tool_bridge_ready: false,
            constraints: {
              cloudflare_sandbox_requires_workers_paid: true,
              pages_dev_is_not_a_pc_tunnel_hostname: true,
            },
            blockers: [
              { code: "CLOUDFLARE_CONTAINERS_PAID_PLAN_REQUIRED", message: "Cloudflare Containers require the Workers Paid plan." },
            ],
          },
        },
      },
    },
  });

  const cloudflare = cards.find((card) => card.providerId === "cloudflare");
  assert.equal(cloudflare?.canConnect, true);
  assert.deepEqual(cloudflare?.connectAction, {
    providerId: "cloudflare",
    scopeMode: undefined,
    services: [],
  });
  assert.equal(cloudflare?.primaryLabel, "Connect Cloudflare");
  assert.equal(cloudflare?.disabledReason, "");
  assert.deepEqual(cloudflare?.scopes, ["account:read", "user:read"]);
  assert.equal(cloudflare?.provisioning.environment_status, "blocked");
  assert.deepEqual(
    (cloudflare?.provisioning.blockers as Array<Record<string, unknown>> | undefined)?.map((item) => item.code),
    ["CLOUDFLARE_CONTAINERS_PAID_PLAN_REQUIRED"],
  );
});

test("account connection prelude gives Google explicit OAuth scope mode actions", () => {
  const cards = buildAccountConnectionPrelude({
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
                description: "Basic identity",
                scopes: ["openid", "email", "profile"],
                services: ["identity"],
              },
              {
                id: "google_drive",
                label: "Google Drive selected files",
                description: "Drive file",
                scopes: ["openid", "email", "profile", "https://www.googleapis.com/auth/drive.file"],
                services: ["identity", "drive_file"],
              },
              {
                id: "google_gmail_labels",
                label: "Gmail labels",
                description: "Labels only",
                scopes: ["openid", "email", "profile", "https://www.googleapis.com/auth/gmail.labels"],
                services: ["identity", "gmail_labels"],
              },
              {
                id: "google_gmail_metadata",
                label: "Gmail metadata/search",
                description: "Metadata",
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
      ],
    },
  });

  const google = cards.find((card) => card.providerId === "google");
  assert.equal(google?.canConnect, true);
  assert.deepEqual(google?.connectAction, { providerId: "google", scopeMode: "google_gmail_labels", services: ["identity", "gmail_labels"] });
  assert.equal(google?.scopeMode, "google_gmail_labels");
  assert.ok(google?.scopes.includes("https://www.googleapis.com/auth/gmail.labels"));
  assert.equal(google?.scopeModes.length, 4);
  assert.ok(google?.scopeModes.some((mode) => mode.id === "google_drive"));
  assert.ok(google?.scopeModes.some((mode) => mode.id === "google_gmail_metadata" && mode.restricted));
  assert.match(google?.scopeModes.find((mode) => mode.id === "google_gmail_metadata")?.warning ?? "", /Restricted Gmail/);
});

test("account connection prelude treats Codex as a redacted credential", () => {
  const rawToken = ["codex", "raw", "token"].join("-");
  const cards = buildAccountConnectionPrelude({
    accounts_connections: {
      providers: {
        codex: {
          provider_kind: "codex",
          auth_type: "codex",
          platform_api_key_required: false,
          auth_methods: [
            { id: "chatgpt_account", configured: false },
            { id: "codex_access_token", configured: true },
            { id: "app_server_secret", configured: false },
          ],
          connected: true,
          configured: true,
          token_configured: true,
          can_clear: true,
          connection_status: "connected",
          status_label: "Token saved",
          access_token: rawToken,
        },
      },
    },
  });

  const codex = cards.find((card) => card.providerId === "codex");
  assert.equal(codex?.label, "Codex");
  assert.equal(codex?.providerKind, "codex");
  assert.equal(codex?.authType, "codex");
  assert.equal(codex?.platformApiKeyRequired, false);
  assert.deepEqual(codex?.authMethods.map((method) => method.id), ["chatgpt_account", "codex_access_token", "app_server_secret"]);
  assert.equal(codex?.canConnect, false);
  assert.equal(codex?.connectAction, undefined);
  assert.equal(codex?.credential?.kind, "codex_access_token");
  assert.equal(codex?.credential?.configured, true);
  assert.equal(codex?.credential?.canClear, true);
  assert.doesNotMatch(JSON.stringify(codex), new RegExp(rawToken));
});

test("Japanese account connection copy covers every provider and OAuth variant", () => {
  const cards = buildAccountConnectionPrelude({
    accounts_connections: {
      providers: {
        google: {
          connect_enabled: true,
          connection_status: "not_connected",
          status_label: "Ready to connect",
          scope_mode: "google_gmail_metadata",
          scope_modes: [
            {
              id: "google_gmail_metadata",
              label: "Gmail metadata/search",
              description: "Restricted metadata/search scope for Gmail.",
              warning: "Restricted Gmail scopes require explicit review.",
              restricted: true,
              scopes: ["https://www.googleapis.com/auth/gmail.metadata"],
              services: ["gmail_metadata"],
            },
          ],
        },
      },
    },
  }, "ja");

  assert.deepEqual(cards.map((card) => card.providerId), ["cloudflare", "google", "github", "codex"]);
  for (const card of cards) {
    const visibleCopy = [
      card.description,
      card.statusLabel,
      card.primaryLabel,
      card.disabledReason,
      card.officialAppDescription,
      card.selfHostDescription,
      card.configureLabel,
      card.credential?.saveLabel ?? "",
      card.credential?.clearLabel ?? "",
    ].join(" ");
    assert.doesNotMatch(visibleCopy, /Connect|Credential|Token needed|Client config|Official app|required|Import JSON|Review credential/i);
  }
  const gmail = cards.find((card) => card.providerId === "google")?.scopeModes[0];
  assert.equal(gmail?.label, "Gmailの検索とメタデータ");
  assert.match(gmail?.description ?? "", /メールの検索/);
  assert.doesNotMatch(`${gmail?.label} ${gmail?.description} ${gmail?.warning}`, /Restricted|metadata\/search|scope/i);
});

test("Codex App Server prelude maps safe Tools & MCP status", () => {
  const prelude = buildCodexAppServerPrelude({
    tools_mcp: {
      codex_app_server: {
        configured: true,
        enabled: true,
        transport: "websocket_remote",
        connection_status: "blocked_auth_required",
        status_label: "Auth required",
        blocked_reason: "Configure a Codex App Server WS token or shared secret before using a non-loopback endpoint.",
        base_url: "https://codex-app.example.test",
        websocket_url: "wss://codex-app.example.test/ws",
        unix_socket_path: "",
        loopback: false,
        auth_required: true,
        auth_configured: false,
        auth_source: "missing",
        auth_kind: "",
        auth_type: "codex",
        provider_kind: "codex",
        auth_methods: [
          { id: "chatgpt_account", configured: true },
          { id: "app_server_secret", configured: false },
        ],
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
          requires_openai_auth: true,
        },
        tool_source: { status: "blocked_auth_required" },
        automation_endpoint: { status: "disabled" },
      },
    },
  });

  assert.equal(prelude.configured, true);
  assert.equal(prelude.providerId, "codex");
  assert.equal(prelude.providerKind, "codex");
  assert.equal(prelude.authType, "codex");
  assert.deepEqual(prelude.authMethods.map((method) => method.id), ["chatgpt_account", "app_server_secret"]);
  assert.equal(prelude.enabled, true);
  assert.equal(prelude.transport, "websocket_remote");
  assert.equal(prelude.status, "blocked_auth_required");
  assert.equal(prelude.statusLabel, "Auth required");
  assert.equal(prelude.loopback, false);
  assert.equal(prelude.authRequired, true);
  assert.equal(prelude.authSource, "missing");
  assert.equal(prelude.wsTokenFile, "/Users/haru/.config/rumi/codex-app-server.token");
  assert.equal(prelude.authConfigured, false);
  assert.equal(prelude.accountProviderId, "codex");
  assert.equal(prelude.accountAuthMethod, "chatgpt_account");
  assert.equal(prelude.accountAuthMethodLabel, "ChatGPT account");
  assert.equal(prelude.accountType, "chatgpt");
  assert.equal(prelude.accountLabel, "rumi-user@example.test");
  assert.equal(prelude.accountEmail, "rumi-user@example.test");
  assert.equal(prelude.accountPlanType, "prolite");
  assert.equal(prelude.requiresOpenaiAuth, true);
  assert.equal(prelude.toolSourceStatus, "blocked_auth_required");
  assert.equal(prelude.automationEndpointStatus, "disabled");
});
