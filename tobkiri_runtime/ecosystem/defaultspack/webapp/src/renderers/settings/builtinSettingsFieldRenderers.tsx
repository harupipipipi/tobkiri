import type { SettingsFieldRendererEntry } from "./fieldRendererRegistry";
import { BuiltinApiKeySetupRenderer } from "./renderers/apiKeySetupField";
import { BuiltinModelSelectRenderer } from "./renderers/modelSelectField";
import { McpServersField } from "./renderers/mcpServersField";
import { PromptProfileField } from "./renderers/promptProfileField";
import { BuiltinProviderSelectRenderer } from "./renderers/providerSelectField";
import { BuiltinShortcutRecorderRenderer } from "./renderers/shortcutRecorderField";
import { BuiltinSlashCommandsRenderer } from "./renderers/slashCommandsField";

export const builtinSettingsFieldRendererEntries: SettingsFieldRendererEntry[] = [
  {
    id: "builtin-settings-model-select",
    types: ["model_select"],
    renderers: ["model_select", "SettingsModelSearchSelect"],
    component: "SettingsModelSearchSelect",
    render: BuiltinModelSelectRenderer,
  },
  {
    id: "builtin-settings-provider-select",
    types: ["provider_select"],
    renderers: ["provider_select", "SearchableProviderSelect"],
    component: "SearchableProviderSelect",
    render: BuiltinProviderSelectRenderer,
  },
  {
    id: "builtin-settings-api-key-setup",
    types: ["api_key_setup"],
    renderers: ["api_key_setup", "ApiKeySetupField"],
    component: "ApiKeySetupField",
    render: BuiltinApiKeySetupRenderer,
  },
  {
    id: "builtin-settings-slash-commands",
    types: ["slash_commands"],
    renderers: ["slash_commands", "SlashCommandsField"],
    component: "SlashCommandsField",
    render: BuiltinSlashCommandsRenderer,
  },
  {
    id: "builtin-settings-shortcut-recorder",
    types: ["shortcut_recorder"],
    renderers: ["shortcut_recorder", "ShortcutRecorder"],
    component: "ShortcutRecorder",
    render: BuiltinShortcutRecorderRenderer,
  },
  {
    id: "builtin-settings-mcp-servers",
    types: ["mcp_servers"],
    renderers: ["mcp_servers", "McpServersField"],
    component: "McpServersField",
    render: McpServersField,
  },
  {
    id: "builtin-settings-prompt-profile",
    types: ["prompt_profile"],
    renderers: ["prompt_profile", "PromptProfileField"],
    component: "PromptProfileField",
    render: PromptProfileField,
  },
];
