import type { SettingsSection } from "../lib/api";
import { normalizeLocale, type LocaleSetting } from "../lib/i18n";
import { settingsFieldSearchText } from "../lib/settingsSearch";

export type ControlCenterSectionId =
  | "quick_setup"
  | "models_api"
  | "accounts_connections"
  | "coding_backends"
  | "features"
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
    label: "AI Assistant",
    description: "Understand, compare, and safely change settings through a guided conversation.",
    help: "The assistant can prepare changes, while you keep final control over every value that is applied.",
    order: 0,
  },
  {
    id: "models_api",
    label: "Models",
    description: "Default models, task assignments, model sets, and fallback behavior.",
    help: "Connections and credentials are managed separately from model choice.",
    order: 10,
  },
  {
    id: "workspace_ui",
    label: "Display & Input",
    description: "Language, composer, input methods, shortcuts, responses, and previews.",
    help: "Only everyday presentation and input controls belong here.",
    order: 20,
  },
  {
    id: "accounts_connections",
    label: "Connections",
    description: "AI providers, accounts, credentials, channels, webhooks, and devices.",
    help: "Connections own identity and secrets. Model choice and tool policy remain separate.",
    order: 30,
  },
  {
    id: "features",
    label: "Features",
    description: "Calendar, commands, ambient capture, and other optional product features.",
    help: "Feature-specific behavior stays with the feature that owns it.",
    order: 40,
  },
  {
    id: "coding_backends",
    label: "Coding Backends",
    description: "Agent runtimes, coding sessions, workspace policy, and live diagnostics.",
    help: "Coding backends are separate from ordinary model API providers and remain Authority-controlled.",
    order: 45,
  },
  {
    id: "tools_mcp",
    label: "Tools",
    description: "Chat tools, tool selection, MCP sources, transparency, and tool permissions.",
    help: "A tool can require a connection, but sign-in remains in Connections.",
    order: 50,
  },
  {
    id: "computer_automation",
    label: "Automation & Permissions",
    description: "Approval rules, computer control, OS permissions, triggers, and continuation.",
    help: "High-impact automation is explicit about scope, risk, and the next action.",
    order: 60,
  },
  {
    id: "privacy_security",
    label: "Safety & Data",
    description: "External data flow, retention, logs, credential safety, and administrator policy.",
    help: "Security state is explained by impact and action rather than raw counters.",
    order: 70,
  },
  {
    id: "profiles",
    label: "Profiles",
    description: "Compare and apply per-profile overrides for models, tools, connections, and permissions.",
    help: "Inherited and overridden values are shown with their effective scope.",
    order: 80,
  },
  {
    id: "packs_extensions",
    label: "Packs & Extensions",
    description: "Installed packs, trust, permissions, updates, and pack-owned settings.",
    help: "Pack-owned settings stay with their owner and expose reset boundaries.",
    order: 90,
  },
  {
    id: "advanced",
    label: "Advanced Settings",
    description: "Custom models, routing, compatibility, context, and developer tuning.",
    help: "Raw IDs, internal paths, and expert controls stay out of the standard experience.",
    order: 100,
  },
  {
    id: "diagnostics",
    label: "Diagnostics & Support",
    description: "Actionable problems, connection tests, system state, logs, and support information.",
    help: "Diagnostics starts with a fixable problem, not a dump of internal state.",
    order: 110,
  },
];

const JA_SECTION_COPY: Record<ControlCenterSectionId, Pick<ControlCenterSection, "label" | "description" | "help">> = {
  quick_setup: { label: "AIアシスタント", description: "AIと対話しながら、設定を探す・比較する・安全に変更するための画面です。", help: "AIは変更案を準備し、実際に適用する値はユーザーが確認できます。" },
  models_api: { label: "モデル", description: "会話で使うモデル、用途別の割り当て、自動選択とフォールバックを設定します。", help: "接続や認証情報は「接続」で管理します。" },
  workspace_ui: { label: "表示と入力", description: "表示言語、入力方法、ショートカット、回答とプレビューの見え方を設定します。", help: "日常的に変更する表示・入力項目だけをまとめています。" },
  accounts_connections: { label: "接続", description: "AIプロバイダー、アカウント、外部サービス、Webhook、デバイスの接続を管理します。", help: "認証情報は専用の安全な保存経路を使用します。" },
  coding_backends: { label: "コーディングバックエンド", description: "エージェントruntime、コーディングsession、workspace policy、稼働状態を管理します。", help: "通常のモデルAPI providerとは分離し、Authorityによる承認を維持します。" },
  features: { label: "機能", description: "カレンダー、コマンド、指で録音など、追加機能ごとの動作を設定します。", help: "各機能に固有の項目を、その所有機能ごとにまとめています。" },
  tools_mcp: { label: "ツール", description: "チャットで使えるツール、MCP、ツール候補、透明性を管理します。", help: "ログインは「接続」、実行承認は「自動化と権限」で管理します。" },
  computer_automation: { label: "自動化と権限", description: "承認ルール、コンピュータ操作、OS権限、トリガー、継続実行を管理します。", help: "影響の大きい操作は、必要な機能・リスク・適用範囲と一緒に表示します。" },
  privacy_security: { label: "安全とデータ", description: "外部へ送るデータ、保持、ログ、認証情報、管理者ポリシーを管理します。", help: "件数ではなく、原因・影響・解決操作のある問題だけを表示します。" },
  profiles: { label: "プロファイル", description: "モデル、ツール、接続、承認ルールの上書きを比較・適用します。", help: "継承値と上書き値、適用範囲を確認できます。" },
  packs_extensions: { label: "Pack・拡張機能", description: "Packの提供元、信頼、権限、更新、Pack固有設定を管理します。", help: "Pack由来の項目を通常設定から隔離し、所有元とリセット範囲を示します。" },
  advanced: { label: "詳細設定", description: "カスタムモデル、詳細ルーティング、互換性、内部チューニングを表示します。", help: "通常は変更不要な上級者・開発者向け項目です。" },
  diagnostics: { label: "診断・サポート", description: "解決可能な問題、接続テスト、ログ、サポート情報を表示します。", help: "問題の原因、影響、修復操作を一つの場所で確認します。" },
};

type LocalizedFieldCopy = { label: string; help?: string; options?: Record<string, string> };

const JA_FIELD_COPY: Record<string, LocalizedFieldCopy> = {
  "general.composer_placeholder": { label: "入力欄の案内文", help: "メッセージ入力欄が空のときに表示する案内文です。" },
  "general.show_activity_in_messages": { label: "回答に処理状況を表示", help: "回答の上部に、処理の進み具合や利用した機能を表示します。" },
  "general.keyboard_button_navigation": { label: "キーボードでボタンを移動", help: "Tabキーで入力欄やサイドバーのボタンへ移動できるようにします。" },
  "general.manual_runtime_mode_selection": {
    label: "実行モードを手動選択できるようにする",
    help: "通常はオフのまま、自律エージェントを使用します。オンにすると入力欄に実行モード選択を表示します。",
  },
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
  "*.model_api_routes": {
    label: "モデル別の接続先",
    help: "必要な場合だけ、モデルごとに使用するAPIキーを指定します。通常はプロバイダーの既定キーが使われます。",
  },
  "*.thinking_level": { label: "考える深さ" },
  "*.deepthink_enabled": { label: "長時間の深い検討を使う" },
  "*.model_allowlist": { label: "利用するモデル", help: "モデル選択画面に表示し、Tobkiriが自動選択できるモデルを選びます。" },
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

const DEPRECATED_SETTING_KEYS = new Set([
  "external_input.input_setup_guide",
  "external_input.input_template_summary",
  "external_input.input_profile_summary",
  "external_output.output_setup_guide",
  "tools.default_target",
  "computer_use_haze.enabled",
  "mimo_coding_company.run_initial_review_now",
]);

const ADVANCED_FIELD_PATTERNS = [
  /(^|_)(route|routing|provider_order|provider_only|provider_ignore|primary_provider)($|_)/,
  /(^|_)(provider_select|provider_mode|provider_filter|gateway_routing_target|gateway_provider_sort|gateway_allow_fallbacks|allow_gateway_fallbacks)($|_)/,
  /(^|_)(latency|tokens_per_second|fast_min_samples|target_chars)($|_)/,
  /(^|_)(custom_template_path|custom_profile_paths|custom_prompt_examples)($|_)/,
  /(^|_)(semantic_backend|semantic_candidate_limit|selector_trace|final_limit)($|_)/,
  /(^|_)(docker_worker_count|docker_personas)($|_)/,
  /(^|_)(edge_width|animation_speed)($|_)/,
];

const ADVANCED_SOURCE_SECTIONS = new Set([
  "external_input",
  "external_output",
  "line",
  "mobile",
]);

const JA_SOURCE_SECTION_COPY: Record<string, string> = {
  general: "表示と操作",
  preview: "プレビュー",
  chat_rendering: "会話の表示",
  models: "モデル",
  apis: "API接続",
  accounts_connections: "アカウントと接続",
  tools: "機能とMCP",
  computer_use_haze: "コンピュータ操作中の表示",
  calendar: "カレンダー",
  ambient: "指で録音",
  operations_company: "業務エージェント",
  mimo_coding_company: "MiMo Coding",
  mobile: "モバイル連携",
  line: "LINE連携",
  external_input: "外部からの受信・Webhook",
  external_output: "外部への返信・送信",
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
  models_api: "models_api",
  workspace_ui: "workspace_ui",
  accounts_connections: "accounts_connections",
  coding_backends: "coding_backends",
  tools_mcp: "tools_mcp",
  computer_automation: "computer_automation",
  privacy_security: "privacy_security",
  profiles: "profiles",
  packs_extensions: "packs_extensions",
  advanced: "advanced",
  diagnostics: "diagnostics",
  settings_home: "quick_setup",
  home: "quick_setup",
  setup: "quick_setup",
  onboarding: "quick_setup",
  models: "models_api",
  model: "models_api",
  model_routing: "models_api",
  ai_model: "models_api",
  apis: "accounts_connections",
  api: "accounts_connections",
  providers: "accounts_connections",
  provider: "accounts_connections",
  accounts: "accounts_connections",
  app: "accounts_connections",
  apps: "accounts_connections",
  connections: "accounts_connections",
  integrations: "accounts_connections",
  coding_backend: "coding_backends",
  codex_app_server: "coding_backends",
  mobile: "accounts_connections",
  pairing: "accounts_connections",
  oauth: "accounts_connections",
  external_input: "accounts_connections",
  external_output: "accounts_connections",
  external_channel: "accounts_connections",
  line: "accounts_connections",
  features: "features",
  feature: "features",
  calendar: "features",
  commands: "features",
  ambient: "features",
  tools: "tools_mcp",
  tool: "tools_mcp",
  mcp: "tools_mcp",
  computer: "computer_automation",
  computer_use: "computer_automation",
  browser: "computer_automation",
  automation: "computer_automation",
  triggers: "computer_automation",
  continuity: "computer_automation",
  system_info: "computer_automation",
  general: "workspace_ui",
  preview: "workspace_ui",
  sidebar: "workspace_ui",
  history: "workspace_ui",
  composer: "workspace_ui",
  theme: "workspace_ui",
  layout: "workspace_ui",
  workspace: "workspace_ui",
  ui: "workspace_ui",
  profile: "profiles",
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
  debug: "diagnostics",
  logs: "diagnostics",
};

const FIELD_TOKEN_ALIASES: Array<[RegExp, ControlCenterSectionId]> = [
  [/\b(model|model[_ -]?route|fallback|thinking|reasoning)\b/i, "models_api"],
  [/\b(provider|api[_ -]?key|token|oauth|account|connection|connect|gmail|drive|google|cloudflare|codex|github|webhook|endpoint)\b/i, "accounts_connections"],
  [/\b(calendar|command|ambient|voice[_ -]?capture)\b/i, "features"],
  [/\b(computer|browser|screen|click|type|scroll|desktop|accessibility|continuity|automation|ambient|camera|microphone)\b/i, "computer_automation"],
  [/\b(mcp|tool|approval|allowlist|denylist|permission[_ -]?overrides)\b/i, "tools_mcp"],
  [/\b(theme|layout|sidebar|preview|composer|shortcut|gradient|indicator|language|voice)\b/i, "workspace_ui"],
  [/\b(profile|runtime|adaptive)\b/i, "profiles"],
  [/\b(privacy|security|audit|retention|secret|credential|dangerous|authority)\b/i, "privacy_security"],
  [/\b(pack|extension|template)\b/i, "packs_extensions"],
  [/\b(debug|diagnostic|health|log|raw|migration)\b/i, "diagnostics"],
];

const SOURCE_SECTION_ROUTES: Record<string, ControlCenterSectionId> = {
  general: "workspace_ui",
  preview: "workspace_ui",
  chat_rendering: "workspace_ui",
  models: "models_api",
  apis: "accounts_connections",
  accounts_connections: "accounts_connections",
  coding_backends: "coding_backends",
  external_input: "accounts_connections",
  external_output: "accounts_connections",
  line: "accounts_connections",
  mobile: "accounts_connections",
  calendar: "features",
  commands: "features",
  ambient: "features",
  tools: "tools_mcp",
  computer_use_haze: "computer_automation",
  triggers: "computer_automation",
  continuity: "computer_automation",
  system_info: "computer_automation",
  permissions: "computer_automation",
  approvals: "computer_automation",
  privacy: "privacy_security",
  security: "privacy_security",
  profiles: "profiles",
  profile: "profiles",
  adaptive: "profiles",
  operations_company: "packs_extensions",
  mimo_coding_company: "packs_extensions",
  external_custom: "advanced",
  context_compaction: "advanced",
  debug: "diagnostics",
  diagnostics: "diagnostics",
};

const FIELD_ROUTE_OVERRIDES: Record<string, ControlCenterSectionId> = {
  "calendar.agent_model": "features",
  "ambient.agent_model": "models_api",
  "ambient.model": "models_api",
  "ambient.provider": "accounts_connections",
  "ambient.connection": "accounts_connections",
  "ambient.confirm_before_ai_send": "computer_automation",
  "external_input.include_source_context": "privacy_security",
  "external_input.policy_summary": "privacy_security",
  "models.api_keys": "accounts_connections",
  "models.external_tokens": "accounts_connections",
  "models.public_url_launcher": "accounts_connections",
  "models.public_url_summary": "accounts_connections",
  "models.input_provider": "accounts_connections",
  "models.output_provider": "accounts_connections",
  "models.input_endpoint_id": "accounts_connections",
  "models.output_target_id": "accounts_connections",
  "models.output_callback_token_id": "accounts_connections",
  // Keep the Pack-owned storage keys, but surface these choices where users
  // look for them in the task-oriented settings IA.
  "operations_company.model_allowlist": "models_api",
  "operations_company.tool_denylist": "tools_mcp",
  "mimo_coding_company.model_allowlist": "models_api",
};

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
  if (ADVANCED_FIELD_PATTERNS.some((pattern) => pattern.test(String(field.id)))) {
    normalized.advanced = true;
  }
  if (ADVANCED_SOURCE_SECTIONS.has(sourceSectionId)) {
    normalized.advanced = true;
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
  const exact = FIELD_ROUTE_OVERRIDES[`${section.id}.${field.id}`];
  if (exact) return exact;
  const sectionMatch = SOURCE_SECTION_ROUTES[section.id] ?? mapSettingsSectionId(section.id);
  if (
    sectionMatch === "computer_automation"
    || sectionMatch === "accounts_connections"
    || sectionMatch === "coding_backends"
    || sectionMatch === "features"
    || sectionMatch === "privacy_security"
    || sectionMatch === "workspace_ui"
    || sectionMatch === "models_api"
    || sectionMatch === "profiles"
    || sectionMatch === "advanced"
    || sectionMatch === "diagnostics"
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
      if (DEPRECATED_SETTING_KEYS.has(`${sourceSection.id}.${rawField.id}`)) continue;
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
  const connections = byId.get("accounts_connections");
  const models = byId.get("models_api");
  const sharedApiKeyField = connections?.fields.find((field) => (
    field.sourceSectionId === "apis"
    && field.id === "api_keys"
    && (field.type === "api_keys" || String(field.type) === "api_key_setup")
  ));
  if (models && sharedApiKeyField) {
    const modelApiKeyField = {
      ...sharedApiKeyField,
      label: normalizeLocale(locale) === "ja" ? "AI APIキー" : "AI API keys",
      help: normalizeLocale(locale) === "ja"
        ? "メインモデルなどを選んだあと、AIモデルで使うAPIキーをここで登録します。"
        : "After choosing your main models, register the API keys those AI models can use here.",
      type: "api_key_setup",
      renderer: "api_key_setup",
      provider_scope: "llm",
      controlSectionId: "models_api" as const,
    } as ControlCenterField;
    models.fields.push(modelApiKeyField);
    const apiSource = settingsSections.find((section) => section.id === "apis");
    if (apiSource && !models.sourceSections.some((section) => section.id === apiSource.id)) {
      models.sourceSections.push(apiSource);
    }
    const modelFieldRank = (field: ControlCenterField): number => {
      if (["main_model", "lightweight_model", "preferred_model", "preferred_model_group", "auto_route_within_group"].includes(field.id)) {
        return 100;
      }
      if (field.sourceSectionId === "apis" && field.id === "api_keys") return 200;
      if (field.id === "model_api_routes") return 900;
      return 400;
    };
    models.fields = models.fields
      .map((field, index) => ({ field, index }))
      .sort((left, right) => (
        modelFieldRank(left.field) - modelFieldRank(right.field)
        || left.index - right.index
      ))
      .map(({ field }) => field);
  }
  const quickSetup = byId.get("quick_setup");
  if (quickSetup) {
    quickSetup.fields = [];
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
      configureSectionId: "accounts_connections" as const,
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
      configureSectionId: "accounts_connections" as const,
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
  const codingBackends = recordValue(settingsValues.coding_backends);
  const toolsMcp = recordValue(settingsValues.tools_mcp);
  const appServer = recordValue(codingBackends.codex_app_server);
  const legacyAppServer = recordValue(toolsMcp.codex_app_server);
  const resolvedAppServer = Object.keys(appServer).length ? appServer : legacyAppServer;
  const toolSource = recordValue(resolvedAppServer.tool_source);
  const automationEndpoint = recordValue(resolvedAppServer.automation_endpoint);
  const account = recordValue(resolvedAppServer.account);
  const configured = Boolean(resolvedAppServer.configured);
  const enabled = Boolean(resolvedAppServer.enabled);
  const status = String(resolvedAppServer.connection_status || (configured ? "configured" : "not_configured"));
  const transport = String(resolvedAppServer.transport || "off");
  const normalizedTransport: CodexAppServerPrelude["transport"] = (
    transport === "stdio"
    || transport === "unix"
    || transport === "websocket_loopback"
    || transport === "websocket_remote"
  ) ? transport : "off";
  return {
    providerId: String(resolvedAppServer.provider_id || account.provider_id || "codex"),
    providerKind: String(resolvedAppServer.provider_kind || account.provider_kind || "codex"),
    authType: String(resolvedAppServer.auth_type || "codex"),
    authMethods: Array.isArray(resolvedAppServer.auth_methods) ? resolvedAppServer.auth_methods.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object") : [],
    configured,
    enabled,
    transport: normalizedTransport,
    statusLabel: String(resolvedAppServer.status_label || (configured ? "Configured" : "Not configured")),
    status,
    blockedReason: String(resolvedAppServer.blocked_reason || ""),
    baseUrl: String(resolvedAppServer.base_url || ""),
    websocketUrl: String(resolvedAppServer.websocket_url || ""),
    unixSocketPath: String(resolvedAppServer.unix_socket_path || ""),
    loopback: resolvedAppServer.loopback !== false,
    authRequired: Boolean(resolvedAppServer.auth_required),
    authConfigured: Boolean(resolvedAppServer.auth_configured),
    authSource: String(resolvedAppServer.auth_source || "missing"),
    authKind: String(resolvedAppServer.auth_kind || ""),
    wsTokenFile: String(resolvedAppServer.ws_token_file || ""),
    sharedSecretFile: String(resolvedAppServer.shared_secret_file || ""),
    toolSourceStatus: String(toolSource.status || "disabled"),
    automationEndpointStatus: String(automationEndpoint.status || "disabled"),
    accountLabel: String(account.account_label || account.email || ""),
    accountType: String(account.type || ""),
    accountProviderId: String(account.provider_id || resolvedAppServer.provider_id || "codex"),
    accountAuthMethod: String(account.auth_method || ""),
    accountAuthMethodLabel: String(account.auth_method_label || ""),
    accountEmail: String(account.email || ""),
    accountPlanType: String(account.plan_type || account.planType || ""),
    requiresOpenaiAuth: Boolean(account.requires_openai_auth ?? account.requiresOpenaiAuth),
  };
}
