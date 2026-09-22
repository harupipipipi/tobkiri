import type { SettingsSection } from "../lib/api";
import { normalizeLocale, type LocaleSetting } from "../lib/i18n";
import { settingsFieldSearchText } from "../lib/settingsSearch";

export type ControlCenterSectionId =
  | "quick_setup"
  | "models_api"
  | "accounts_connections"
  | "tools_mcp"
  | "computer_automation"
  | "workspace_ui"
  | "profiles"
  | "privacy_security"
  | "packs_extensions"
  | "advanced"
  | "diagnostics";

export type ControlCenterField = SettingsSection["fields"][number] & {
  controlSectionId: ControlCenterSectionId;
  sourceSectionId: string;
  sourceSectionLabel: string;
  sourceSectionDescription?: string;
};

export type ControlCenterSection = {
  id: ControlCenterSectionId;
  label: string;
  description: string;
  help: string;
  order: number;
  fields: ControlCenterField[];
  sourceSections: SettingsSection[];
};

type SettingsField = SettingsSection["fields"][number];
type SettingsValues = Record<string, Record<string, unknown>>;

export type AccountConnectionScopeModeOption = {
  id: string;
  label: string;
  description: string;
  scopes: string[];
  services: string[];
  restricted: boolean;
  warning: string;
};

export type AccountConnectionPreludeCard = {
  providerId: string;
  providerKind: string;
  authType: string;
  authMethods: Array<Record<string, unknown>>;
  platformApiKeyRequired: boolean;
  label: string;
  description: string;
  connected: boolean;
  statusLabel: string;
  status: string;
  canConnect: boolean;
  connectAction?: {
    providerId: string;
    scopeMode?: string;
    services?: string[];
  };
  primaryLabel: string;
  disabledReason: string;
  officialAppDescription: string;
  selfHostDescription: string;
  configureSectionId: ControlCenterSectionId;
  configureLabel: string;
  scopeMode?: string;
  services: string[];
  scopes: string[];
  capabilities: string[];
  approvalRequiredCapabilities: string[];
  rejectedCapabilities: string[];
  provisioning: Record<string, unknown>;
  credentialRef: string;
  expiresAt: string;
  scopeModes: AccountConnectionScopeModeOption[];
  credential?: {
    kind: "codex_access_token";
    configured: boolean;
    canClear: boolean;
    placeholder: string;
    saveLabel: string;
    clearLabel: string;
  };
};

export type CodexAppServerPrelude = {
  providerId: string;
  providerKind: string;
  authType: string;
  authMethods: Array<Record<string, unknown>>;
  configured: boolean;
  enabled: boolean;
  transport: "off" | "stdio" | "unix" | "websocket_loopback" | "websocket_remote";
  statusLabel: string;
  status: string;
  blockedReason: string;
  baseUrl: string;
  websocketUrl: string;
  unixSocketPath: string;
  loopback: boolean;
  authRequired: boolean;
  authConfigured: boolean;
  authSource: string;
  authKind: string;
  wsTokenFile: string;
  sharedSecretFile: string;
  toolSourceStatus: string;
  automationEndpointStatus: string;
  accountLabel: string;
  accountType: string;
  accountProviderId: string;
  accountAuthMethod: string;
  accountAuthMethodLabel: string;
  accountEmail: string;
  accountPlanType: string;
  requiresOpenaiAuth: boolean;
};

const BLOCKED_RAW_LABELS = new Map([
  ["mimo", "Mimo model preset"],
  ["computer_use_gradient", "Automation visual indicator"],
  ["openrouter_auto", "OpenRouter auto routing"],
]);

const BLOCKED_RAW_LABEL_PATTERNS: Array<[RegExp, string]> = [
  [/\bmimo(?:[_ -]?(?:model|preset|coding|company|v\d+(?:\.\d+)?))*\b/i, "Mimo model preset"],
  [/\bcomputer[_ -]?use[_ -]?gradient(?:[_ -]?(?:enabled|color|opacity|mode))*\b/i, "Automation visual indicator"],
  [/\bopenrouter[_ -]?auto(?:[_ -]?(?:mode|routing|fallback))*\b/i, "OpenRouter auto routing"],
];

const SECTION_META: Array<Omit<ControlCenterSection, "fields" | "sourceSections">> = [
  {
    id: "quick_setup",
    label: "Quick Setup",
    description: "Setup blockers and the first choices that make Rumi usable.",
    help: "Shows model, API, account, MCP, computer approval, and cloud continuation setup first.",
    order: 10,
  },
  {
    id: "models_api",
    label: "Models & API",
    description: "Model roles, providers, API keys, routing, and fallback.",
    help: "Model ids and provider quirks stay here instead of leaking into unrelated sections.",
    order: 20,
  },
  {
    id: "accounts_connections",
    label: "Accounts & Connections",
    description: "OAuth and account connections for Cloudflare, Google, Gmail, Drive, and future providers.",
    help: "Connections own credentials and identity. Tool execution policy lives in Tools & MCP.",
    order: 30,
  },
  {
    id: "tools_mcp",
    label: "Tools & MCP",
    description: "Installed tools, MCP servers, discovered tools, visibility, and approval policy.",
    help: "MCP servers can require a connection, but the login flow belongs to Accounts & Connections.",
    order: 40,
  },
  {
    id: "computer_automation",
    label: "Computer & Automation",
    description: "Screen observation, browser automation, local permissions, approvals, and cloud continuation.",
    help: "Computer control is shown as a high-impact permission surface, not as a normal tool row.",
    order: 50,
  },
  {
    id: "workspace_ui",
    label: "Workspace & UI",
    description: "Theme, layout, panes, shortcuts, composer behavior, and visual indicators.",
    help: "Visual-only settings live here, including automation indicators.",
    order: 60,
  },
  {
    id: "profiles",
    label: "Profiles",
    description: "Profile-specific runtime presets for models, tools, credentials, and policy.",
    help: "Profile-aware settings should show configured, missing, disabled, and unapproved states.",
    order: 70,
  },
  {
    id: "privacy_security",
    label: "Privacy & Security",
    description: "Credentials, approvals, audit logs, data retention, and dangerous action policy.",
    help: "Write-like actions, secrets, and approvals stay visible and policy-aware.",
    order: 80,
  },
  {
    id: "packs_extensions",
    label: "Packs & Extensions",
    description: "Pack install, update, enable, disable, and settings contributions.",
    help: "Packs contribute settings through a registry contract instead of mutating UI directly.",
    order: 90,
  },
  {
    id: "advanced",
    label: "Advanced",
    description: "Rare power-user and compatibility settings.",
    help: "Low-frequency knobs stay below the daily setup path.",
    order: 100,
  },
  {
    id: "diagnostics",
    label: "Diagnostics",
    description: "Health checks, logs, raw state, migration reports, and developer diagnostics.",
    help: "Debug and raw JSON settings are intentionally parked here.",
    order: 110,
  },
];

const JA_SECTION_COPY: Record<ControlCenterSectionId, Pick<ControlCenterSection, "label" | "description" | "help">> = {
  quick_setup: { label: "はじめに", description: "Rumiを使い始めるために必要な項目です。", help: "モデル、接続、機能、コンピュータ操作の準備状況を最初に確認できます。" },
  models_api: { label: "モデルとAPI", description: "普段使うモデル、接続先、切り替え方を選びます。", help: "プロバイダー固有の値は、必要な場合だけ詳細設定に表示します。" },
  accounts_connections: { label: "アカウントと接続", description: "Google、GitHubなどの外部サービスを接続します。", help: "ここではログイン情報を管理します。機能を実行する際の確認方法は「機能とMCP」で設定します。" },
  tools_mcp: { label: "機能とMCP", description: "利用できる機能、MCPサーバー、実行前の確認方法を管理します。", help: "サービスへのログインと、機能を実行する権限は別々に管理されます。" },
  computer_automation: { label: "コンピュータ操作", description: "画面・ブラウザ操作、自動化、端末間の引き継ぎを管理します。", help: "画面や入力を操作する機能は影響が大きいため、許可状態をここで確認できます。" },
  workspace_ui: { label: "表示と操作", description: "言語、見た目、入力欄、ショートカットを変更します。", help: "日常的な表示・操作の設定をまとめています。" },
  profiles: { label: "プロファイル", description: "用途ごとのモデル、機能、ポリシーの組み合わせを管理します。", help: "現在使われているプロファイルと不足している設定を確認できます。" },
  privacy_security: { label: "プライバシーと安全", description: "認証情報、承認、履歴、危険な操作の扱いを管理します。", help: "書き込み操作や秘密情報は、承認ルールを保ったまま管理されます。" },
  packs_extensions: { label: "パックと拡張機能", description: "追加機能のインストール、有効化、更新を管理します。", help: "拡張機能が追加した設定もここから確認できます。" },
  advanced: { label: "詳細設定", description: "互換性や特殊な利用方法のための設定です。", help: "通常は変更する必要のない項目をまとめています。" },
  diagnostics: { label: "診断", description: "動作状況、ログ、移行結果を確認します。", help: "問題調査に使う内部情報はここだけに表示します。" },
};

type LocalizedFieldCopy = { label: string; help?: string; options?: Record<string, string> };

const JA_FIELD_COPY: Record<string, LocalizedFieldCopy> = {
  "general.composer_placeholder": { label: "入力欄の案内文", help: "メッセージ入力欄が空のときに表示する案内文です。" },
  "general.show_activity_in_messages": { label: "回答に処理状況を表示", help: "回答の上部に、処理の進み具合や利用した機能を表示します。" },
  "general.keyboard_button_navigation": { label: "キーボードでボタンを移動", help: "Tabキーで入力欄やサイドバーのボタンへ移動できるようにします。" },
  "general.spotlight_shortcut_enabled": { label: "会話検索のショートカットを使う", help: "どの画面からでもショートカットで会話検索を開けます。" },
  "general.spotlight_shortcut": { label: "会話検索のキー", help: "会話検索を開くキーの組み合わせを指定します。" },
  "general.spotlight_shortcut_text_input": { label: "入力中も会話検索を開く", help: "入力欄にカーソルがあるときも会話検索のショートカットを使えます。" },
  "general.language": { label: "表示言語", help: "Tobkiriの画面で使う言語を選びます。拡張機能に翻訳がない場合は元の文言を表示します。", options: { auto: "端末に合わせる" } },
  "preview.default_mode": { label: "プレビューの表示方法" },
  "chat_rendering.unknown_block_strategy": { label: "未対応の内容の表示", help: "未対応形式は安全な案内だけを表示します。開発者向け情報にも値や秘密は含まれません。", options: { placeholder: "安全な案内", debug: "開発者向け情報（制限済み）" } },
  "models.preferred_model": { label: "普段使うモデル", help: "新しい会話で最初に使うモデルを選びます。" },
  "models.on_switch_to_non_vision_with_images": { label: "画像非対応モデルへ切り替えるとき", help: "画像のある会話で、画像を読めないモデルを選んだときの動作です。", options: { auto_bridge: "画像を読み取って引き継ぐ", ask: "切り替える前に確認", block: "切り替えない", ignore: "画像を渡さず切り替える" } },
  "tools.semantic_backend": { label: "機能候補の探し方", options: { embedding: "意味が近い機能を探す", lexical: "名前や説明から探す" } },
  "tools.selector_trace": { label: "機能選定の記録" },
  "tools.semantic_candidate_limit": { label: "確認する機能候補の上限" },
  "computer_use_haze.enabled": { label: "操作中の画面表示", help: "Rumiが画面を操作している間、画面端に色を表示します。" },
  "computer_use_haze.preset": { label: "操作中に表示する配色" },
  "computer_use_haze.start_color": { label: "開始色" },
  "computer_use_haze.end_color": { label: "終了色" },
  "computer_use_haze.accent_color": { label: "強調色" },
  "computer_use_haze.opacity": { label: "表示の濃さ" },
  "computer_use_haze.edge_width": { label: "表示する幅" },
  "computer_use_haze.animation_speed": { label: "動きの速さ" },
  "*.auto_open": { label: "自動で開く" },
  "*.max_items": { label: "表示する件数" },
  "*.quick_add_enabled": { label: "日付を押して追加" },
  "*.default_item_type": { label: "最初に選ぶ項目の種類" },
  "*.default_time": { label: "既定の時刻" },
  "*.time_slot_minutes": { label: "時刻の間隔（分）" },
  "*.show_time_picker": { label: "時刻選択を表示" },
  "*.agent_task_default": { label: "AIタスクとして追加" },
  "*.agent_model": { label: "カレンダーで使うモデル" },
  "*.agent_current_chat": { label: "現在の会話で実行" },
  "*.week_start": { label: "週の開始曜日" },
  "*.show_outside_days": { label: "前後の月の日付も表示" },
  "*.dim_weekends": { label: "週末を控えめに表示" },
  "*.task_color": { label: "タスクの色" },
  "*.event_color": { label: "予定の色" },
  "*.max_items_per_day": { label: "1日に表示する件数" },
  "*.show_widgets": { label: "ウィジェットを表示" },
  "*.main_model": { label: "メインモデル" },
  "*.lightweight_model": { label: "軽量モデル" },
  "*.preferred_model_group": { label: "モデルグループ" },
  "*.auto_route_within_group": { label: "用途に合わせてグループ内で自動選択" },
  "*.model_api_routes": { label: "モデルごとのAPI接続" },
  "*.thinking_level": {
    label: "考える深さ",
    help: "現在有効なモデル／プロファイルで、1回の応答ごとの推論の深さを指定します。DeepThinkのレビューループ（deepthink_enabled）を有効にしたり、セクション上限（deepthink_max_sections）を変更したりはしません。",
  },
  "*.deepthink_enabled": {
    label: "長時間の深い検討を使う",
    help: "長時間の複数ステップのレビューフローを有効にします。反復回数（deepthink_max_review_iterations）とセクション上限（deepthink_max_sections）は、モデルの推論の深さ（thinking_level）とは独立しています。",
  },
  "*.handoff": { label: "クラウド・別端末へ引き継ぐ" },
  "*.api_keys": { label: "APIキーとトークン" },
  "*.mention_policy": { label: "メンションの扱い" },
  "*.show_advanced_commands": { label: "詳細コマンドを表示" },
  "*.input_setup_guide": { label: "受け取り設定の手順" },
  "*.endpoint_summary": { label: "受け取り口" },
  "*.input_provider": { label: "受け取り元" },
  "*.input_template_id": { label: "受け取り形式" },
  "*.input_profile_id": { label: "受け取り用プロファイル" },
  "*.input_endpoint_id": { label: "受け取り口のID" },
  "*.public_url_launcher": { label: "一時公開URL" },
  "*.provider_route_copy": { label: "接続先のパス" },
  "*.input_template_summary": { label: "利用できる受け取り形式" },
  "*.input_profile_summary": { label: "利用できる受け取り用プロファイル" },
  "*.include_source_context": { label: "受け取り元の情報を含める" },
  "*.default_response_mode": { label: "既定の返信方法" },
  "*.input_response_preset": { label: "返信内容のプリセット" },
  "*.policy_summary": { label: "送信先ごとのルール" },
  "*.saved_sources_summary": { label: "保存済みの受け取り元" },
  "*.output_setup_guide": { label: "送信方法" },
  "*.external_tokens": { label: "外部サービスのトークン（確認のみ）" },
  "*.output_provider": { label: "送信先サービス" },
  "*.output_template_id": { label: "送信形式" },
  "*.output_profile_id": { label: "送信用プロファイル" },
  "*.output_send_mode": { label: "送信方法" },
  "*.output_target_id": { label: "送信先ID" },
  "*.output_callback_token_id": { label: "送信に使うトークンID" },
  "*.output_template_summary": { label: "利用できる送信形式" },
  "*.output_profile_summary": { label: "利用できる送信用プロファイル" },
  "*.response_summary": { label: "返信内容のルール" },
  "*.response_prompt_preset": { label: "返信内容のプリセット" },
  "*.public_url_summary": { label: "一時公開URLの状態" },
  "*.custom_template_path": { label: "追加テンプレートの場所" },
  "*.custom_profile_paths": { label: "追加プロファイルの場所" },
  "*.custom_prompt_examples": { label: "返信例" },
  "*.mode": { label: "起動の判断方法" },
  "*.filter_unrelated": { label: "関係のない候補を除外" },
  "*.ai_request_logging": { label: "AIへのリクエストを記録" },
};

const JA_SOURCE_SECTION_COPY: Record<string, string> = {
  general: "表示と操作",
  preview: "プレビュー",
  chat_rendering: "会話の表示",
  models: "モデル",
  apis: "API接続",
  accounts_connections: "アカウントと接続",
  tools: "機能とMCP",
  computer_use_haze: "コンピュータ操作中の表示",
};

/** Return user-facing source copy without exposing registry labels in Japanese. */
export function localizedSettingsSourceLabel(
  sourceSectionId: string,
  sourceLabel: unknown,
  locale: LocaleSetting = "en",
): string {
  if (normalizeLocale(locale) !== "ja") {
    return safeSettingsLabel(sourceLabel, sourceSectionId);
  }
  return JA_SOURCE_SECTION_COPY[sourceSectionId] ?? "拡張機能の設定";
}

function localizedSectionMeta(locale: LocaleSetting): Array<Omit<ControlCenterSection, "fields" | "sourceSections">> {
  if (normalizeLocale(locale) !== "ja") return SECTION_META;
  return SECTION_META.map((section) => ({ ...section, ...JA_SECTION_COPY[section.id] }));
}

const SECTION_ID_ALIASES: Record<string, ControlCenterSectionId> = {
  quick_setup: "quick_setup",
  setup: "quick_setup",
  onboarding: "quick_setup",
  models: "models_api",
  model: "models_api",
  model_routing: "models_api",
  ai_model: "models_api",
  apis: "models_api",
  api: "models_api",
  providers: "models_api",
  provider: "models_api",
  accounts: "accounts_connections",
  app: "accounts_connections",
  apps: "accounts_connections",
  connections: "accounts_connections",
  integrations: "accounts_connections",
  mobile: "accounts_connections",
  pairing: "accounts_connections",
  oauth: "accounts_connections",
  tools: "tools_mcp",
  tool: "tools_mcp",
  mcp: "tools_mcp",
  computer: "computer_automation",
  computer_use: "computer_automation",
  browser: "computer_automation",
  automation: "computer_automation",
  ambient: "computer_automation",
  continuity: "computer_automation",
  system_info: "computer_automation",
  general: "workspace_ui",
  preview: "workspace_ui",
  sidebar: "workspace_ui",
  history: "workspace_ui",
  composer: "workspace_ui",
  commands: "workspace_ui",
  theme: "workspace_ui",
  layout: "workspace_ui",
  calendar: "workspace_ui",
  workspace: "workspace_ui",
  ui: "workspace_ui",
  profile: "profiles",
  profiles: "profiles",
  adaptive: "profiles",
  privacy: "privacy_security",
  security: "privacy_security",
  permissions: "privacy_security",
  approvals: "privacy_security",
  authority: "privacy_security",
  packs: "packs_extensions",
  pack: "packs_extensions",
  extensions: "packs_extensions",
  extension: "packs_extensions",
  advanced: "advanced",
  diagnostics: "diagnostics",
  debug: "diagnostics",
  logs: "diagnostics",
};

const FIELD_TOKEN_ALIASES: Array<[RegExp, ControlCenterSectionId]> = [
  [/\b(model|provider|api[_ -]?key|api[_ -]?route|token|openrouter)\b/i, "models_api"],
  [/\b(oauth|account|connection|connect|gmail|drive|google|cloudflare|codex|github)\b/i, "accounts_connections"],
  [/\b(computer|browser|screen|click|type|scroll|desktop|accessibility|continuity|automation|ambient|camera|microphone)\b/i, "computer_automation"],
  [/\b(mcp|tool|approval|allowlist|denylist|permission[_ -]?overrides)\b/i, "tools_mcp"],
  [/\b(theme|layout|sidebar|preview|composer|shortcut|command|gradient|indicator|calendar|language|voice)\b/i, "workspace_ui"],
  [/\b(profile|runtime|adaptive)\b/i, "profiles"],
  [/\b(privacy|security|audit|retention|secret|credential|dangerous|authority)\b/i, "privacy_security"],
  [/\b(pack|extension|template)\b/i, "packs_extensions"],
  [/\b(debug|diagnostic|health|log|raw|migration)\b/i, "diagnostics"],
];

export function controlCenterSectionMeta(locale: LocaleSetting = "en"): ControlCenterSection[] {
  return localizedSectionMeta(locale).map((section) => ({ ...section, fields: [], sourceSections: [] }));
}

export function safeSettingsLabel(value: unknown, fallback: unknown = ""): string {
  const label = String(value ?? fallback ?? "").trim();
  const exact = BLOCKED_RAW_LABELS.get(label.toLowerCase());
  if (exact) return exact;
  for (const [pattern, replacement] of BLOCKED_RAW_LABEL_PATTERNS) {
    if (pattern.test(label)) return replacement;
  }
  return label;
}

export function normalizeSettingsField(field: SettingsField, sourceSectionId = "", locale: LocaleSetting = "en"): SettingsField {
  const normalized = { ...field };
  normalized.label = safeSettingsLabel(field.label, field.id);
  if (Array.isArray(field.options)) {
    normalized.options = field.options.map((option) => ({
      ...option,
      label: safeSettingsLabel(option.label, option.value),
    }));
  }
  if (normalizeLocale(locale) === "ja") {
    const copy = JA_FIELD_COPY[`${sourceSectionId}.${field.id}`] ?? JA_FIELD_COPY[`*.${field.id}`];
    if (copy) {
      normalized.label = copy.label;
      if (copy.help) normalized.help = copy.help;
      if (copy.options && Array.isArray(normalized.options)) {
        normalized.options = normalized.options.map((option) => ({
          ...option,
          label: copy.options?.[String(option.value)] ?? option.label,
        }));
      }
    }
  }
  return normalized;
}

export function mapSettingsSectionId(sectionId: string | null | undefined): ControlCenterSectionId | null {
  if (!sectionId) return null;
  const normalized = sectionId.trim().toLowerCase();
  return SECTION_ID_ALIASES[normalized] ?? null;
}

export function controlCenterSectionForField(section: SettingsSection, field: SettingsField): ControlCenterSectionId {
  const fieldRecord = field as SettingsField & Record<string, unknown>;
  const explicit = mapSettingsSectionId(String(fieldRecord.control_center_section ?? fieldRecord.section ?? ""));
  if (explicit) return explicit;
  const sectionMatch = mapSettingsSectionId(section.id);
  if (
    sectionMatch === "computer_automation"
    || sectionMatch === "accounts_connections"
    || sectionMatch === "privacy_security"
  ) {
    return sectionMatch;
  }
  const haystack = [
    field.id,
    field.label,
    field.help ?? "",
    field.type,
  ].join(" ");
  for (const [pattern, target] of FIELD_TOKEN_ALIASES) {
    if (pattern.test(haystack)) return target;
  }
  return sectionMatch ?? "packs_extensions";
}

export function buildControlCenterSections(settingsSections: SettingsSection[], locale: LocaleSetting = "en"): ControlCenterSection[] {
  const sections = controlCenterSectionMeta(locale);
  const byId = new Map(sections.map((section) => [section.id, section]));
  for (const sourceSection of settingsSections) {
    const sourceSectionTargets = new Set<ControlCenterSectionId>();
    for (const rawField of sourceSection.fields) {
      const targetId = controlCenterSectionForField(sourceSection, rawField);
      sourceSectionTargets.add(targetId);
      const target = byId.get(targetId);
      if (!target) continue;
      const field = normalizeSettingsField(rawField, sourceSection.id, locale) as ControlCenterField;
      field.controlSectionId = targetId;
      field.sourceSectionId = sourceSection.id;
      field.sourceSectionLabel = localizedSettingsSourceLabel(
        sourceSection.id,
        sourceSection.label,
        locale,
      );
      field.sourceSectionDescription = sourceSection.description;
      target.fields.push(field);
    }
    const fallbackTargetId = mapSettingsSectionId(sourceSection.id) ?? "packs_extensions";
    sourceSectionTargets.add(fallbackTargetId);
    for (const targetId of sourceSectionTargets) {
      byId.get(targetId)?.sourceSections.push(sourceSection);
    }
  }
  const quickSetup = byId.get("quick_setup");
  if (quickSetup) {
    quickSetup.fields = selectQuickSetupFields(sections);
  }
  return sections;
}

export function filterControlCenterSections(
  sections: ControlCenterSection[],
  searchQuery: string,
): ControlCenterSection[] {
  const query = searchQuery.trim().toLowerCase();
  if (!query) return sections;
  return sections.filter((section) => {
    const sectionText = [section.id, section.label, section.description, section.help].join(" ").toLowerCase();
    return sectionText.includes(query) || section.fields.some((field) => settingsFieldSearchText(field).includes(query));
  });
}

function selectQuickSetupFields(sections: ControlCenterSection[]): ControlCenterField[] {
  const wanted = new Set(["models_api", "accounts_connections", "tools_mcp", "computer_automation"]);
  const scored = sections
    .filter((section) => wanted.has(section.id))
    .flatMap((section) => section.fields.map((field) => ({ field, score: quickSetupScore(field) })))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score || a.field.sourceSectionLabel.localeCompare(b.field.sourceSectionLabel));

  const seen = new Set<string>();
  const selected: ControlCenterField[] = [];
  for (const { field } of scored) {
    const key = `${field.sourceSectionId}.${field.id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    selected.push({ ...field, controlSectionId: "quick_setup" });
    if (selected.length >= 6) break;
  }
  return selected;
}

function quickSetupScore(field: ControlCenterField): number {
  const text = [field.id, field.label, field.help ?? "", field.type].join(" ").toLowerCase();
  const fieldType = String(field.type);
  let score = 0;
  if (/default|preferred|api[_ -]?key|provider|model/.test(text)) score += 50;
  if (/cloudflare|google|gmail|drive|codex|connection|oauth|continuity/.test(text)) score += 40;
  if (/computer|browser|screen|accessibility|approval|mcp/.test(text)) score += 30;
  if (fieldType === "secret" || fieldType === "api_key_setup" || fieldType === "model_select") score += 20;
  if ((field as SettingsField & { advanced?: boolean }).advanced) score -= 40;
  return score;
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

const GOOGLE_ACCOUNT_SCOPE_MODE_IDS = new Set([
  "google_identity",
  "google_drive",
  "google_gmail_labels",
  "google_gmail_metadata",
  "google_gmail_readonly",
]);

const GOOGLE_ACCOUNT_SCOPE_MODE_FALLBACKS: AccountConnectionScopeModeOption[] = [
  {
    id: "google_identity",
    label: "Google identity",
    description: "Basic sign-in identity only.",
    scopes: ["openid", "email", "profile"],
    services: ["identity"],
    restricted: false,
    warning: "",
  },
  {
    id: "google_drive",
    label: "Google Drive selected files",
    description: "Drive file scope for files explicitly selected or shared with Rumi.",
    scopes: ["openid", "email", "profile", "https://www.googleapis.com/auth/drive.file"],
    services: ["identity", "drive_file"],
    restricted: false,
    warning: "",
  },
  {
    id: "google_gmail_labels",
    label: "Gmail labels",
    description: "Read Gmail labels without message bodies.",
    scopes: ["openid", "email", "profile", "https://www.googleapis.com/auth/gmail.labels"],
    services: ["identity", "gmail_labels"],
    restricted: false,
    warning: "",
  },
  {
    id: "google_gmail_metadata",
    label: "Gmail metadata/search",
    description: "Restricted metadata/search scope for Gmail.",
    scopes: ["openid", "email", "profile", "https://www.googleapis.com/auth/gmail.metadata"],
    services: ["identity", "gmail_metadata"],
    restricted: true,
    warning: "Restricted Gmail scopes require explicit self-host acknowledgement or Google verification review.",
  },
  {
    id: "google_gmail_readonly",
    label: "Gmail read-only bodies",
    description: "Restricted read-only access to Gmail message bodies.",
    scopes: ["openid", "email", "profile", "https://www.googleapis.com/auth/gmail.readonly"],
    services: ["identity", "gmail_readonly"],
    restricted: true,
    warning: "Restricted Gmail scopes can expose message content and may require Google security review.",
  },
];

const JA_GOOGLE_SCOPE_COPY: Record<string, Pick<AccountConnectionScopeModeOption, "label" | "description" | "warning">> = {
  google_identity: { label: "Googleへのログインのみ", description: "氏名やメールアドレスなど、ログインに必要な基本情報だけを使います。", warning: "" },
  google_drive: { label: "選択したGoogle Driveファイル", description: "Rumiで明示的に選択または共有したファイルだけを扱います。", warning: "" },
  google_gmail_labels: { label: "Gmailのラベル", description: "メール本文を読まず、Gmailのラベルだけを取得します。", warning: "" },
  google_gmail_metadata: { label: "Gmailの検索とメタデータ", description: "メールの検索とメタデータを扱います。本文は読みません。", warning: "この権限は制限付きです。セルフホストでの明示的な承認、またはGoogleの審査が必要になる場合があります。" },
  google_gmail_readonly: { label: "Gmail本文の読み取り", description: "Gmailのメール本文を読み取ります。メールの変更や送信は行いません。", warning: "メール本文がRumiに共有されます。この権限はGoogleのセキュリティ審査が必要になる場合があります。" },
};

function localizeScopeMode(
  option: AccountConnectionScopeModeOption,
  locale: LocaleSetting,
): AccountConnectionScopeModeOption {
  if (normalizeLocale(locale) !== "ja") return option;
  const copy = JA_GOOGLE_SCOPE_COPY[option.id];
  return copy ? { ...option, ...copy } : option;
}

function accountScopeModeOptions(
  value: unknown,
  locale: LocaleSetting = "en",
): AccountConnectionScopeModeOption[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const row = recordValue(item);
    const id = String(row.id ?? "").trim();
    const surface = String(row.surface ?? "").trim();
    if (!id || !GOOGLE_ACCOUNT_SCOPE_MODE_IDS.has(id) || surface === "models_api") return [];
    return [localizeScopeMode({
      id,
      label: String(row.label || id),
      description: String(row.description || ""),
      scopes: stringList(row.scopes),
      services: stringList(row.services),
      restricted: Boolean(row.restricted),
      warning: String(row.warning || ""),
    }, locale)];
  });
}

const JA_CONNECTION_STATUS: Record<string, string> = {
  connected: "接続済み",
  disconnected: "未接続",
  not_connected: "未接続",
  missing_scope_config: "接続設定が必要",
  missing_self_host_config: "OAuthクライアント設定が必要",
  needs_official_app: "公式アプリが必要",
  missing_token: "アクセストークンが必要",
  ready: "接続できます",
};

function localizedConnectionStatus(
  status: string,
  connected: boolean,
  fallback: unknown,
  locale: LocaleSetting,
): string {
  if (normalizeLocale(locale) !== "ja") {
    return String(fallback || (connected ? "Connected" : "Disconnected"));
  }
  return JA_CONNECTION_STATUS[status] ?? (connected ? "接続済み" : "設定を確認してください");
}

function oauthStatusForProvider(settingsValues: SettingsValues, providerId: string): Record<string, unknown> {
  const apiRows = Array.isArray(settingsValues.apis?.api_keys) ? settingsValues.apis.api_keys : [];
  for (const row of apiRows) {
    const provider = recordValue(row);
    if (String(provider.provider_id ?? "").trim() !== providerId) continue;
    return recordValue(provider.oauth);
  }
  const connections = recordValue(settingsValues.accounts_connections?.providers);
  return recordValue(connections[providerId]);
}

export function buildAccountConnectionPrelude(
  settingsValues: SettingsValues = {},
  locale: LocaleSetting = "en",
): AccountConnectionPreludeCard[] {
  const japanese = normalizeLocale(locale) === "ja";
  const definitions: Array<{
    providerId: AccountConnectionPreludeCard["providerId"];
    label: string;
    description: string;
    fallbackStatus: Record<string, unknown>;
    scopeMode?: string;
    configureSectionId: ControlCenterSectionId;
    configureLabel: string;
    credential?: (status: Record<string, unknown>) => AccountConnectionPreludeCard["credential"];
  }> = [
    {
      providerId: "cloudflare",
      label: "Cloudflare",
      description: "Continue Rumi tasks in the user's Cloudflare account when this computer is offline.",
      fallbackStatus: {
        backend_supported: false,
        connect_enabled: false,
        connected: false,
        connection_status: "missing_scope_config",
        status_label: "Missing scope config",
        disabled_reason: "Configure self-host OAuth",
        scopes: [],
      },
      scopeMode: undefined,
      configureSectionId: "accounts_connections" as const,
      configureLabel: "Configure self-host OAuth",
    },
    {
      providerId: "google",
      label: "Google",
      description: "Connect Google identity, Drive selected files, Gmail labels, or explicit restricted Gmail modes.",
      fallbackStatus: {
        backend_supported: true,
        connect_enabled: false,
        connected: false,
        connection_status: "missing_self_host_config",
        status_label: "Client config needed",
        disabled_reason: "Configure self-host OAuth",
        scopes: [],
        scope_mode: "google_identity",
        scope_modes: GOOGLE_ACCOUNT_SCOPE_MODE_FALLBACKS,
      },
      scopeMode: "google_identity",
      configureSectionId: "models_api" as const,
      configureLabel: "Configure self-host OAuth",
    },
    {
      providerId: "github",
      label: "GitHub",
      description: "Connect GitHub identity, repositories, and workflow scopes through a credential bundle.",
      fallbackStatus: {
        supported: true,
        backend_supported: false,
        connect_enabled: false,
        connected: false,
        connection_status: "missing_self_host_config",
        status_label: "Credential needed",
        disabled_reason: "Import credential bundle",
        scopes: [],
      },
      configureSectionId: "accounts_connections" as const,
      configureLabel: "Import JSON",
    },
    {
      providerId: "codex",
      label: "Codex",
      description: "Save the local/programmatic Codex workflow access credential.",
      fallbackStatus: {
        supported: true,
        backend_supported: true,
        connect_enabled: false,
        connected: false,
        configured: false,
        token_configured: false,
        can_clear: false,
        connection_status: "missing_token",
        status_label: "Token needed",
        disabled_reason: "Save Codex access token",
      },
      configureSectionId: "privacy_security" as const,
      configureLabel: "Review credential policy",
      credential: (status) => ({
        kind: "codex_access_token",
        configured: Boolean(status.token_configured ?? status.configured ?? status.connected),
        canClear: Boolean(status.can_clear),
        placeholder: "Codex access token",
        saveLabel: Boolean(status.token_configured ?? status.configured ?? status.connected) ? "Update token" : "Save token",
        clearLabel: "Clear token",
      }),
    },
  ];
  return definitions.map((definition) => {
    const status = {
      ...definition.fallbackStatus,
      ...oauthStatusForProvider(settingsValues, definition.providerId),
    };
    const connected = Boolean(status.connected);
    const canConnect = Boolean(status.connect_enabled);
    const credential = definition.credential?.(status);
    const disabledReason = connected || canConnect ? "" : String(status.disabled_reason || status.status_label || "Configure self-host OAuth");
    const providedScopeModes = accountScopeModeOptions(status.scope_modes, locale);
    const scopeModes = definition.providerId === "google"
      ? providedScopeModes.length
        ? providedScopeModes
        : GOOGLE_ACCOUNT_SCOPE_MODE_FALLBACKS.map((option) => localizeScopeMode(option, locale))
      : [];
    const requestedScopeMode = String(status.scope_mode || definition.scopeMode || "").trim();
    const selectedScopeMode = scopeModes.some((option) => option.id === requestedScopeMode)
      ? requestedScopeMode
      : scopeModes[0]?.id ?? definition.scopeMode;
    const selectedScopeModeOption = scopeModes.find((option) => option.id === selectedScopeMode);
    const selectedServices = selectedScopeModeOption?.services ?? [];
    const credentialRef = recordValue(status.credential_ref);
    return {
      providerId: definition.providerId,
      providerKind: String(status.provider_kind || definition.providerId),
      authType: String(status.auth_type || ""),
      authMethods: Array.isArray(status.auth_methods) ? status.auth_methods.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object") : [],
      platformApiKeyRequired: Boolean(status.platform_api_key_required),
      label: definition.label,
      description: japanese ? {
        cloudflare: "このコンピュータがオフラインの間も、Cloudflare上でTobkiriのタスクを継続します。",
        google: "Googleへのログイン、選択したDriveファイル、または明示的に選んだGmail権限を接続します。",
        github: "GitHubのアカウント、リポジトリ、ワークフロー権限を認証情報セットで接続します。",
        codex: "Codexワークフロー用のアクセストークンをこの端末へ安全に保存します。",
      }[definition.providerId] ?? "外部サービスへの接続を管理します。" : definition.description,
      connected,
      statusLabel: localizedConnectionStatus(
        String(status.connection_status || (connected ? "connected" : "disconnected")),
        connected,
        status.status_label,
        locale,
      ),
      status: String(status.connection_status || (connected ? "connected" : "disconnected")),
      canConnect,
      connectAction: canConnect && !credential
        ? { providerId: definition.providerId, scopeMode: selectedScopeMode, services: selectedServices }
        : undefined,
      primaryLabel: japanese
        ? definition.providerId === "google"
          ? connected ? "選択した権限で再接続" : "選択した権限で接続"
          : connected ? `${definition.label}を再接続` : `${definition.label}に接続`
        : definition.providerId === "google"
          ? connected ? "Reconnect selected mode" : "Connect selected mode"
          : connected ? `Reconnect ${definition.label}` : `Connect ${definition.label}`,
      disabledReason: japanese && disabledReason ? "接続するには設定が必要です。" : disabledReason,
      officialAppDescription: credential
        ? japanese ? "この端末の秘密情報ストレージへ保存し、画面には設定済みかどうかだけを表示します。" : "Stored through local secret storage and only exposed as configured status."
        : japanese ? "ホスト型の接続を使うには公式アプリが必要です。" : "Official app required for hosted broker mode.",
      selfHostDescription: credential
        ? japanese ? "モデル用のAPIキーやWorkspace Agentトークンとは別に管理されます。" : "Separate from Platform API keys and Workspace Agent tokens."
        : disabledReason === "Configure self-host OAuth"
        ? japanese ? "セルフホストでは、利用する権限を明示してOAuthクライアントを設定してください。" : "Configure self-host OAuth with explicit scopes before connecting."
        : japanese ? "セルフホストでもOAuthクライアントと権限を設定すれば接続できます。" : "Self-host OAuth remains available when a client and scopes are configured.",
      configureSectionId: definition.configureSectionId,
      configureLabel: japanese ? {
        cloudflare: "セルフホストOAuthを設定",
        google: "セルフホストOAuthを設定",
        github: "認証情報を読み込む",
        codex: "認証情報の扱いを確認",
      }[definition.providerId] ?? "接続設定を開く" : definition.configureLabel,
      scopeMode: selectedScopeMode,
      services: selectedServices,
      scopes: selectedScopeModeOption?.scopes.length ? selectedScopeModeOption.scopes : stringList(status.scopes),
      capabilities: stringList(status.capabilities),
      approvalRequiredCapabilities: stringList(status.approval_required_capabilities),
      rejectedCapabilities: stringList(status.rejected_capabilities),
      provisioning: recordValue(status.provisioning),
      credentialRef: String(credentialRef.credential_id || ""),
      expiresAt: String(status.expires_at || ""),
      scopeModes,
      credential: credential && japanese ? {
        ...credential,
        placeholder: "Codexアクセストークン",
        saveLabel: credential.configured ? "トークンを更新" : "トークンを保存",
        clearLabel: "トークンを削除",
      } : credential,
    };
  });
}

export function buildCodexAppServerPrelude(settingsValues: SettingsValues = {}): CodexAppServerPrelude {
  const toolsMcp = recordValue(settingsValues.tools_mcp);
  const appServer = recordValue(toolsMcp.codex_app_server);
  const toolSource = recordValue(appServer.tool_source);
  const automationEndpoint = recordValue(appServer.automation_endpoint);
  const account = recordValue(appServer.account);
  const configured = Boolean(appServer.configured);
  const enabled = Boolean(appServer.enabled);
  const status = String(appServer.connection_status || (configured ? "configured" : "not_configured"));
  const transport = String(appServer.transport || "off");
  const normalizedTransport: CodexAppServerPrelude["transport"] = (
    transport === "stdio"
    || transport === "unix"
    || transport === "websocket_loopback"
    || transport === "websocket_remote"
  ) ? transport : "off";
  return {
    providerId: String(appServer.provider_id || account.provider_id || "codex"),
    providerKind: String(appServer.provider_kind || account.provider_kind || "codex"),
    authType: String(appServer.auth_type || "codex"),
    authMethods: Array.isArray(appServer.auth_methods) ? appServer.auth_methods.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object") : [],
    configured,
    enabled,
    transport: normalizedTransport,
    statusLabel: String(appServer.status_label || (configured ? "Configured" : "Not configured")),
    status,
    blockedReason: String(appServer.blocked_reason || ""),
    baseUrl: String(appServer.base_url || ""),
    websocketUrl: String(appServer.websocket_url || ""),
    unixSocketPath: String(appServer.unix_socket_path || ""),
    loopback: appServer.loopback !== false,
    authRequired: Boolean(appServer.auth_required),
    authConfigured: Boolean(appServer.auth_configured),
    authSource: String(appServer.auth_source || "missing"),
    authKind: String(appServer.auth_kind || ""),
    wsTokenFile: String(appServer.ws_token_file || ""),
    sharedSecretFile: String(appServer.shared_secret_file || ""),
    toolSourceStatus: String(toolSource.status || "disabled"),
    automationEndpointStatus: String(automationEndpoint.status || "disabled"),
    accountLabel: String(account.account_label || account.email || ""),
    accountType: String(account.type || ""),
    accountProviderId: String(account.provider_id || appServer.provider_id || "codex"),
    accountAuthMethod: String(account.auth_method || ""),
    accountAuthMethodLabel: String(account.auth_method_label || ""),
    accountEmail: String(account.email || ""),
    accountPlanType: String(account.plan_type || account.planType || ""),
    requiresOpenaiAuth: Boolean(account.requires_openai_auth ?? account.requiresOpenaiAuth),
  };
}
