import type {
  ComposerCommandArg,
  ComposerCommandCategory,
  ComposerCommandItem,
  ComposerCommandRisk,
} from "./api";

export const REGISTERED_SLASH_COMMAND_SOURCE = "settings.registered_slash_commands";

export type RegisteredSlashCommandActionId =
  | "toggle_yolo"
  | "toggle_ultra_yolo"
  | "open_model_picker"
  | "open_tool_picker"
  | "open_settings"
  | "new_conversation"
  | "show_status"
  | "set_mode_chat"
  | "set_mode_coding"
  | "set_mode_agent"
  | "clear_composer_state"
  | "set_fast_mode"
  | "set_price_mode";

export type RegisteredSlashCommandActionOption = {
  id: RegisteredSlashCommandActionId;
  label: string;
  description: string;
  category: ComposerCommandCategory;
  risk: ComposerCommandRisk;
  args?: ComposerCommandArg[];
};

export type RegisteredSlashCommandRecord = {
  name: string;
  action: RegisteredSlashCommandActionId;
  aliases?: string[];
  label?: string;
  description?: string;
  enabled?: boolean;
};

export const REGISTERED_SLASH_COMMAND_ACTIONS: RegisteredSlashCommandActionOption[] = [
  {
    id: "toggle_yolo",
    label: "Full Access (YOLO)",
    description: "フルアクセスと「承認を求める」を切り替えます。",
    category: "mode",
    risk: "medium",
    args: [{ name: "enabled", type: "boolean", required: false }],
  },
  {
    id: "toggle_ultra_yolo",
    label: "Full Access（旧YOLO互換）",
    description: "フルアクセスと「承認を求める」を切り替えます。",
    category: "mode",
    risk: "medium",
    args: [{ name: "enabled", type: "boolean", required: false }],
  },
  {
    id: "open_model_picker",
    label: "Model Picker",
    description: "モデル選択を開きます。",
    category: "model",
    risk: "low",
    args: [{ name: "query", type: "string", required: false }],
  },
  {
    id: "open_tool_picker",
    label: "Tool Picker",
    description: "tool選択を開きます。",
    category: "tools",
    risk: "low",
    args: [{ name: "query", type: "string", required: false }],
  },
  {
    id: "open_settings",
    label: "Settings",
    description: "設定を開きます。",
    category: "settings",
    risk: "low",
    args: [{ name: "section", type: "string", required: false }],
  },
  {
    id: "new_conversation",
    label: "New Chat",
    description: "新しい会話を作ります。",
    category: "chat",
    risk: "low",
  },
  {
    id: "show_status",
    label: "Status",
    description: "現在の状態を表示します。",
    category: "chat",
    risk: "low",
  },
  {
    id: "set_mode_chat",
    label: "Chat Mode",
    description: "会話モードへ切り替えます。",
    category: "mode",
    risk: "low",
  },
  {
    id: "set_mode_coding",
    label: "Coding Mode",
    description: "codingモードへ切り替えます。",
    category: "mode",
    risk: "low",
  },
  {
    id: "set_mode_agent",
    label: "Agent Mode",
    description: "agentモードへ切り替えます。",
    category: "mode",
    risk: "low",
  },
  {
    id: "clear_composer_state",
    label: "Clear Draft",
    description: "入力欄と保留状態を消します。",
    category: "chat",
    risk: "low",
  },
  {
    id: "set_fast_mode",
    label: "Fast Mode",
    description: "高速候補モデルへ切り替えます。",
    category: "model",
    risk: "low",
    args: [{ name: "enabled", type: "boolean", required: false }],
  },
  {
    id: "set_price_mode",
    label: "Price Mode",
    description: "価格優先モデルへ切り替えます。",
    category: "model",
    risk: "low",
    args: [{ name: "tier", type: "enum", required: false, values: ["low", "high"] }],
  },
];

const ACTION_BY_ID = new Map(REGISTERED_SLASH_COMMAND_ACTIONS.map((item) => [item.id, item]));

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function normalizeRegisteredSlashCommandName(value: unknown): string {
  const normalized = String(value ?? "")
    .trim()
    .replace(/^\/+/, "")
    .toLowerCase()
    .replace(/\s+/g, "_")
    .replace(/[^a-z0-9_-]/g, "");
  return normalized.slice(0, 48);
}

export function normalizeRegisteredSlashCommandAliases(value: unknown): string[] {
  const rawAliases = Array.isArray(value)
    ? value
    : typeof value === "string"
      ? value.split(/[,\n]/)
      : [];
  const aliases = rawAliases
    .map(normalizeRegisteredSlashCommandName)
    .filter(Boolean);
  return [...new Set(aliases)].slice(0, 8);
}

function registeredAction(value: unknown): RegisteredSlashCommandActionOption | null {
  const actionId = String(value ?? "").trim() as RegisteredSlashCommandActionId;
  return ACTION_BY_ID.get(actionId) ?? null;
}

function commandIdentityTokens(command: ComposerCommandItem): string[] {
  return [command.id, command.name, ...(command.aliases ?? [])]
    .map((item) => String(item ?? "").trim().toLowerCase())
    .filter(Boolean);
}

function registerCommandIdentityTokens(
  tokenOwners: Map<string, number>,
  command: ComposerCommandItem,
  index: number,
  extraTokens: string[] = [],
): void {
  for (const token of [...commandIdentityTokens(command), ...extraTokens]) {
    tokenOwners.set(token, index);
  }
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function commandExecutionKey(command: ComposerCommandItem): string {
  return stableJson(command.execution ?? null);
}

export function registeredSlashCommandsFromSettings(value: unknown): ComposerCommandItem[] {
  const records = Array.isArray(value) ? value : [];
  const commands: ComposerCommandItem[] = [];
  const seenNames = new Set<string>();

  for (const item of records) {
    const record: Record<string, unknown> = isRecord(item) ? item : { name: item, action: "toggle_yolo" };
    if (record.enabled === false) continue;
    const name = normalizeRegisteredSlashCommandName(record.name ?? record.command ?? record.id);
    const action = registeredAction(record.action ?? record.frontend_action);
    if (!name || !action || seenNames.has(name)) continue;
    seenNames.add(name);
    const aliases = normalizeRegisteredSlashCommandAliases(record.aliases);
    const label = String(record.label ?? action.label).trim() || action.label;
    const description = String(record.description ?? action.description).trim() || action.description;
    commands.push({
      id: `registered:${name}`,
      name,
      aliases: aliases.filter((alias) => alias !== name),
      label,
      description,
      category: action.category,
      visibility: "default",
      risk: action.risk,
      modes: ["chat", "coding", "agent"],
      args: action.args,
      execution: { type: "frontend", action: action.id },
      source: REGISTERED_SLASH_COMMAND_SOURCE,
    });
  }

  return commands;
}

export function isRegisteredSlashCommand(command: ComposerCommandItem | undefined): boolean {
  return command?.source === REGISTERED_SLASH_COMMAND_SOURCE || command?.id.startsWith("registered:") === true;
}

export function mergeRegisteredSlashCommands(
  baseCommands: ComposerCommandItem[],
  registeredCommands: ComposerCommandItem[],
): ComposerCommandItem[] {
  if (!registeredCommands.length) return baseCommands;
  const merged = [...baseCommands];
  const tokenOwners = new Map<string, number>();
  merged.forEach((command, index) => {
    registerCommandIdentityTokens(tokenOwners, command, index);
  });

  for (const command of registeredCommands) {
    const tokens = commandIdentityTokens(command);
    const existingIndex = tokens.map((token) => tokenOwners.get(token)).find((index) => index !== undefined);
    if (existingIndex !== undefined) {
      const existing = merged[existingIndex];
      if (commandExecutionKey(existing) !== commandExecutionKey(command)) {
        continue;
      }
      const incomingAliases = [command.name, ...(command.aliases ?? [])].filter(
        (alias) => alias && alias !== existing.name && alias !== existing.id,
      );
      merged[existingIndex] = {
        ...existing,
        visibility: "default",
        aliases: [...new Set([...(existing.aliases ?? []), ...incomingAliases])],
      };
      registerCommandIdentityTokens(tokenOwners, merged[existingIndex], existingIndex, tokens);
      continue;
    }
    const nextIndex = merged.length;
    merged.push(command);
    registerCommandIdentityTokens(tokenOwners, command, nextIndex, tokens);
  }
  return merged;
}
