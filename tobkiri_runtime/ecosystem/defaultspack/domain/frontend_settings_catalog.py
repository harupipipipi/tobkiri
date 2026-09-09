"""Pure settings-control definitions shared by legacy and canonical presentation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SettingsCatalogInputs:
    """Required owner-resolved display data; contains no authorization grants."""

    input_templates: list[dict[str, Any]]
    output_templates: list[dict[str, Any]]
    input_profile_options: list[dict[str, Any]]
    output_profile_options: list[dict[str, Any]]
    model_options: list[dict[str, Any]]
    model_route_options: list[dict[str, Any]]
    api_key_status: list[dict[str, Any]]


class SettingsSections:
    """Build existing controls solely from explicitly supplied display data."""

    def build(
        self,
        ui_surfaces: list[dict[str, Any]],
        extensions: list[dict[str, Any]],
        *,
        template_catalog: dict[str, Any] | None,
        inputs: SettingsCatalogInputs,
    ) -> list[dict[str, Any]]:
        """Project control definitions without registry or filesystem discovery."""
        input_templates = deepcopy(inputs.input_templates)
        output_templates = deepcopy(inputs.output_templates)
        input_profile_options = deepcopy(inputs.input_profile_options)
        output_profile_options = deepcopy(inputs.output_profile_options)
        model_options = deepcopy(inputs.model_options)
        model_route_options = deepcopy(inputs.model_route_options)
        api_key_status = deepcopy(inputs.api_key_status)
        sections: list[dict[str, object]] = [
            {
                "id": "general",
                "label": "General",
                "description": "defaultspack shell behavior shared across the app.",
                "fields": [
                    {
                        "id": "composer_placeholder",
                        "label": "Composer Placeholder",
                        "type": "text",
                        "default": "メッセージを入力...",
                        "help": "チャット入力欄の placeholder。",
                    },
                    {
                        "id": "show_activity_in_messages",
                        "label": "Activity In Chat",
                        "type": "toggle",
                        "default": True,
                        "help": "assistant メッセージ上部に activity 情報を表示する。",
                    },
                    {
                        "id": "keyboard_button_navigation",
                        "label": "Keyboard Button Navigation",
                        "type": "toggle",
                        "default": True,
                        "help": "Tab/Shift+Tabでcomposerや右サイドバーの操作へ移動できます。アクセシビリティのため既定で有効です。",
                    },
                    {
                        "id": "spotlight_shortcut_enabled",
                        "label": "Spotlight Shortcut",
                        "type": "toggle",
                        "default": True,
                        "help": "Enable the global conversation Spotlight shortcut.",
                    },
                    {
                        "id": "spotlight_shortcut",
                        "label": "Spotlight Keys",
                        "type": "text",
                        "default": "Ctrl+K",
                        "help": "Use combinations such as Ctrl+K, Ctrl+Alt+K, or Win+K where the browser receives Win-key events.",
                    },
                    {
                        "id": "spotlight_shortcut_text_input",
                        "label": "Shortcut In Text Inputs",
                        "type": "toggle",
                        "default": True,
                        "help": "Allow the Spotlight shortcut while an input or textarea is focused.",
                    },
                    {
                        "id": "language",
                        "label": "Language",
                        "type": "select",
                        "default": "ja",
                        "options": [
                            {"value": "ja", "label": "日本語"},
                            {"value": "en", "label": "English"},
                            {"value": "auto", "label": "Auto"},
                        ],
                        "help": "frontend の表示言語です。未翻訳の拡張項目は元の文言を表示します。",
                    },
                    {
                        "id": "voice_input_enabled",
                        "label": "音声入力",
                        "type": "toggle",
                        "default": True,
                        "help": "composer のマイクボタンでブラウザ音声入力を使います。",
                    },
                    {
                        "id": "voice_input_use_ai",
                        "label": "AI文字起こしモード",
                        "type": "toggle",
                        "default": False,
                        "help": "ON の時は入力文に「文字起こしして:」を付けて、モデルへ文字起こしタスクとして渡します。",
                    },
                    {
                        "id": "manual_runtime_mode_selection",
                        "label": "Manual Runtime Mode Selection",
                        "type": "toggle",
                        "default": False,
                        "help": (
                            "高度な設定: composerに実行モード選択を表示します。"
                            "OFFでは自律エージェントを使用します。"
                        ),
                        "advanced": True,
                        "control_center_section": "advanced",
                    },
                ],
            },
            {
                "id": "preview",
                "label": "Preview",
                "description": "右 preview pane と activity feed の挙動。",
                "fields": [
                    {"id": "auto_open", "label": "Auto Open", "type": "toggle", "default": False},
                    {
                        "id": "default_mode",
                        "label": "Preview Mode",
                        "type": "select",
                        "default": "auto",
                        "options": [
                            {"value": "auto", "label": "Auto"},
                            {"value": "manual", "label": "Manual"},
                        ],
                    },
                    {
                        "id": "max_items",
                        "label": "Preview Limit",
                        "type": "number",
                        "default": 12,
                        "min": 1,
                        "max": 50,
                    },
                ],
            },
            {
                "id": "mobile",
                "label": "Mobile",
                "description": "スマホ接続要求をauthoritative pairing recordで確認します。",
                "fields": [
                    {
                        "id": "pairing_review_id",
                        "label": "Mobile Pairing Review",
                        "type": "mobile_pairing_review",
                        "renderer": "MobilePairingApproval",
                        "default": "",
                        "help": "PCで作成したpairing IDを入力し、保留・拒否・キャンセルを明示的に選びます。",
                    },
                ],
            },
            {
                "id": "calendar",
                "label": "Calendar",
                "description": "カレンダー画面のクリック追加、週表示、予定色を調整します。",
                "fields": [
                    {
                        "id": "quick_add_enabled",
                        "label": "Click To Add",
                        "type": "toggle",
                        "default": True,
                        "help": "日付セルをクリックした時に、新規追加カードを開きます。",
                    },
                    {
                        "id": "default_item_type",
                        "label": "Default Item Type",
                        "type": "select",
                        "default": "task",
                        "options": [
                            {"value": "task", "label": "Task / 青"},
                            {"value": "event", "label": "Event / 緑"},
                            {"value": "reminder", "label": "Reminder / グレー"},
                        ],
                        "help": "新規追加カードで最初に選ばれる種類です。",
                    },
                    {
                        "id": "default_time",
                        "label": "Default Time",
                        "type": "text",
                        "default": "09:00",
                        "help": "新規追加カードの初期時刻です。例: 09:00 / 午前9:00",
                    },
                    {
                        "id": "time_slot_minutes",
                        "label": "Time Slot Minutes",
                        "type": "select",
                        "default": 15,
                        "options": [
                            {"value": 15, "label": "15 minutes"},
                            {"value": 30, "label": "30 minutes"},
                            {"value": 60, "label": "60 minutes"},
                        ],
                        "help": "時刻ドロップダウンの刻み幅です。",
                    },
                    {
                        "id": "show_time_picker",
                        "label": "Show Time Picker",
                        "type": "toggle",
                        "default": True,
                        "help": "時刻入力時にスクロール式の候補を表示します。",
                    },
                    {
                        "id": "agent_task_default",
                        "label": "Agent Task Default",
                        "type": "toggle",
                        "default": False,
                        "help": "Task作成時に、AI agent実行の候補を初期ONにします。",
                    },
                    {
                        "id": "agent_model",
                        "label": "Agent Model",
                        "type": "text",
                        "default": "",
                        "help": "空なら設定済みの非embeddingモデルを自動選択します。例: google/gemini-2.5-flash",
                    },
                    {
                        "id": "agent_current_chat",
                        "label": "Run In Current Chat",
                        "type": "toggle",
                        "default": False,
                        "help": "ONなら予定時刻に現在の会話へ送信します。OFFなら独立したagent実行にします。",
                    },
                    {
                        "id": "week_start",
                        "label": "Week Starts On",
                        "type": "select",
                        "default": "sunday",
                        "options": [
                            {"value": "sunday", "label": "Sunday"},
                            {"value": "monday", "label": "Monday"},
                        ],
                        "help": "月表示の左端の曜日を選びます。",
                    },
                    {
                        "id": "show_outside_days",
                        "label": "Show Outside Days",
                        "type": "toggle",
                        "default": True,
                        "help": "前月/翌月の日付を薄く表示します。",
                    },
                    {
                        "id": "dim_weekends",
                        "label": "Dim Weekends",
                        "type": "toggle",
                        "default": True,
                        "help": "土日セルをほんの少し暗くします。",
                    },
                    {
                        "id": "task_color",
                        "label": "Task Color",
                        "type": "select",
                        "default": "blue",
                        "options": [
                            {"value": "blue", "label": "Blue"},
                            {"value": "cyan", "label": "Cyan"},
                            {"value": "slate", "label": "Slate"},
                        ],
                        "help": "Taskバーの色。既定は青です。",
                    },
                    {
                        "id": "event_color",
                        "label": "Event Color",
                        "type": "select",
                        "default": "green",
                        "options": [
                            {"value": "green", "label": "Green"},
                            {"value": "blue", "label": "Blue"},
                            {"value": "slate", "label": "Slate"},
                        ],
                        "help": "Eventバーの色。既定は緑です。",
                    },
                    {
                        "id": "max_items_per_day",
                        "label": "Visible Items / Day",
                        "type": "number",
                        "default": 3,
                        "min": 1,
                        "max": 6,
                        "help": "1日に表示する予定バーの上限です。",
                    },
                ],
            },
            {
                "id": "chat_rendering",
                "label": "Chat Rendering",
                "description": "block / widget rendering rules for the conversation pane.",
                "fields": [
                    {
                        "id": "show_widgets",
                        "label": "Render Widgets",
                        "type": "toggle",
                        "default": True,
                    },
                    {
                        "id": "unknown_block_strategy",
                        "label": "Unknown Block Strategy",
                        "type": "select",
                        "default": "placeholder",
                        "options": [
                            {"value": "placeholder", "label": "Safe placeholder"},
                            {"value": "debug", "label": "Developer diagnostics (redacted)"},
                        ],
                    },
                ],
            },
            {
                "id": "models",
                "label": "Models",
                "description": "会話で使うモデルと thinking 設定。",
                "fields": [
                    {
                        "id": "main_model",
                        "label": "Main Model",
                        "type": "model_select",
                        "default": "stub/default",
                        "options": deepcopy(model_options),
                        "help": "Default model for normal conversations and new chats.",
                    },
                    {
                        "id": "lightweight_model",
                        "label": "Lightweight Model",
                        "type": "model_select",
                        "default": "",
                        "options": deepcopy(model_options),
                        "help": "Fast model for quick replies and delegated rough work. Leave empty for automatic selection.",
                    },
                    {
                        "id": "preferred_model",
                        "label": "Preferred Model",
                        "type": "select",
                        "default": "stub/default",
                        "options": deepcopy(model_options),
                        "help": "新しい会話と composer の既定モデルです。",
                        "advanced": True,
                    },
                    {
                        "id": "preferred_model_group",
                        "label": "Model Group",
                        "type": "select",
                        "default": "default",
                        "options": [
                            {"value": "default", "label": "標準"},
                            {"value": "fast", "label": "高速"},
                            {"value": "deep", "label": "深く考える"},
                            {"value": "vision", "label": "画像対応"},
                            {"value": "cheap", "label": "節約"},
                            {"value": "local", "label": "ローカル"},
                            {"value": "custom", "label": "カスタム"},
                        ],
                        "help": "個別モデルではなく、目的別グループ内で自動ルーティングします。",
                    },
                    {
                        "id": "auto_route_within_group",
                        "label": "Auto Route In Group",
                        "type": "toggle",
                        "default": True,
                        "help": "画像、tool、thinking、速度の条件に合わせてグループ内の実モデルを選びます。",
                    },
                    {
                        "id": "on_switch_to_non_vision_with_images",
                        "label": "Non-vision Image Switch",
                        "type": "select",
                        "default": "auto_bridge",
                        "options": [
                            {"value": "auto_bridge", "label": "Auto Bridge"},
                            {"value": "ask", "label": "Ask"},
                            {"value": "block", "label": "Block"},
                            {"value": "ignore", "label": "Ignore"},
                        ],
                        "help": "画像あり会話で画像非対応モデルへ切り替える時の挙動です。",
                    },
                    {
                        "id": "utility_models",
                        "label": "Utility Models",
                        "type": "textarea",
                        "default": "{}",
                        "help": "tool_selector / vision_ocr / prompt_compactor などの雑用モデル割り当て。空なら自動選択します。",
                        "advanced": True,
                    },
                    {
                        "id": "model_api_routes",
                        "label": "Model API Variants",
                        "type": "model_api_routes",
                        "default": "",
                        "options": model_route_options,
                        "api_keys": api_key_status,
                        "help": "モデルごとに使う API key を選びます。複数選んだら、各 API key ごとに別 model variant として composer に並びます。",
                    },
                    {
                        "id": "api_routes",
                        "label": "Structured API Routes",
                        "type": "textarea",
                        "default": "[]",
                        "help": "高度設定: JSON配列/オブジェクトで model と apis を定義します。旧 Model API Priority も読み取り互換です。",
                        "advanced": True,
                    },
                    {
                        "id": "api_bound_profiles",
                        "label": "API-bound Profiles",
                        "type": "textarea",
                        "default": "[]",
                        "help": "高度設定: このAPI keyだけで使えるモデル profile をJSONで追加します。",
                        "advanced": True,
                    },
                    {
                        "id": "composite_models",
                        "label": "Composite Models",
                        "type": "textarea",
                        "default": "[]",
                        "help": "高度設定: fallback_chain / ensemble の合体モデルをJSONで定義します。",
                        "advanced": True,
                    },
                    {
                        "id": "model_notes",
                        "label": "Model Notes",
                        "type": "textarea",
                        "default": "{}",
                        "help": "高度設定: モデルごとの特徴を自分の言葉で書き、検索とルーティングの判断材料にします。",
                        "advanced": True,
                    },
                    {
                        "id": "thinking_level",
                        "label": "Thinking Level",
                        "type": "select",
                        "default": "medium",
                        "options": [
                            {"value": "none", "label": "Off"},
                            {"value": "low", "label": "Low"},
                            {"value": "medium", "label": "Medium"},
                            {"value": "high", "label": "High"},
                            {"value": "xhigh", "label": "Extra High"},
                        ],
                        "help": "Rumi は none/low/medium/high/xhigh を送り、各 provider が対応する API パラメータへ変換します。Gemini/Gemma では未対応の値を自動で近い値へ落とします。",
                    },
                    {
                        "id": "deepthink_enabled",
                        "label": "DeepThink",
                        "type": "toggle",
                        "default": False,
                        "help": "thinker型のDeepThink loopを有効にします。タスクには数時間かかる可能性があります。",
                    },
                    {
                        "id": "favorite_profiles",
                        "label": "Composer Model Pins",
                        "type": "textarea",
                        "default": "stub/default",
                        "help": "高度設定: composer に優先表示する profile_id。通常は Preferred Model だけで十分です。",
                        "advanced": True,
                    },
                    {
                        "id": "thinking_level_by_profile",
                        "label": "Per-profile Thinking Map",
                        "type": "textarea",
                        "default": '{"stub/default":"medium"}',
                        "help": "高度設定: profile_id ごとの上書き。通常は Thinking Level を使います。",
                        "advanced": True,
                    },
                ],
            },
            {
                "id": "continuity",
                "label": "Continuity",
                "description": "API provider route, checkpoint, and device/cloud handoff controls.",
                "fields": [
                    {
                        "id": "handoff",
                        "label": "Cloud / Device Handoff",
                        "type": "continuity",
                        "default": {
                            "sandbox_id": "logical-sandbox",
                            "mode": "move",
                            "destination_node_id": "",
                            "route_id": "",
                        },
                        "help": "Pairs destination nodes, probes provider route portability, and starts fenced handoff operations.",
                    },
                ],
            },
            {
                "id": "apis",
                "label": "APIs / Tokens",
                "description": "LLM の API キーも、LINE / Discord / Slack の token も、ここで一元管理します。値は再表示しません。",
                "fields": [
                    {
                        "id": "api_keys",
                        "label": "API Keys / Tokens",
                        "type": "api_keys",
                        "default": [],
                        "help": "provider を選び、名前と値を貼って Save。LINE / Discord / Slack を選ぶと外部送信側の token としても自動で利用できます。",
                    },
                ],
            },
            {
                "id": "line",
                "label": "LINE",
                "description": "LINE 受信時の反応条件。",
                "fields": [
                    {
                        "id": "mention_policy",
                        "label": "Mention Policy",
                        "type": "textarea",
                        "default": '{"group_room_mention_required":true}',
                        "help": "group/room では既定でメンション時のみ反応します。1:1 は従来通り反応します。",
                    },
                ],
            },
            {
                "id": "commands",
                "label": "Commands",
                "description": "Slash command visibility and command palette behavior.",
                "fields": [
                    {
                        "id": "show_advanced_commands",
                        "label": "Show Advanced Commands",
                        "type": "toggle",
                        "default": False,
                        "help": "Advanced slash commandsを候補に含めます。hidden command は直接入力か将来の管理UI向けです。",
                    },
                ],
            },
            {
                "id": "external_input",
                "label": "External Input",
                "description": "Webhookで受ける入口。LINE は Messaging API channel の webhook として受けます。",
                "fields": [
                    {
                        "id": "input_setup_guide",
                        "label": "Setup Flow",
                        "type": "readonly",
                        "default": (
                            "1. Providerを選ぶ\n"
                            "2. Temporary Public URLでWebhook URLを発行する\n"
                            "3. ProviderのWebhook URL欄へコピーする\n"
                            "4. LINE Messaging API Channel Secret / Access Tokenを貼る\n"
                            "5. line-main endpointを有効化し、受信元ルールを確認する"
                        ),
                    },
                    {
                        "id": "endpoint_summary",
                        "label": "Input Endpoints",
                        "type": "readonly",
                        "default": "No endpoints",
                    },
                    {
                        "id": "input_provider",
                        "label": "Input Provider",
                        "type": "select",
                        "default": "line",
                        "options": self._provider_options(
                            input_templates, fallback=["line", "discord", "slack", "generic"]
                        ),
                        "help": "ビルトイン provider は選択だけで切り替えます。独自 provider は External Custom から追加します。",
                    },
                    {
                        "id": "input_template_id",
                        "label": "Input Template",
                        "type": "select",
                        "default": "line.input.default",
                        "options": self._template_options(input_templates, include_custom=False),
                        "help": "LINE/Discord/Slack は YAML 編集なしでテンプレートを選ぶだけにします。",
                    },
                    {
                        "id": "input_profile_id",
                        "label": "Input Profile",
                        "type": "select",
                        "default": "line.default",
                        "options": input_profile_options,
                        "help": "受信 payload を Rumi 入力へ変換する既定 profile です。",
                    },
                    {
                        "id": "input_endpoint_id",
                        "label": "Endpoint ID",
                        "type": "text",
                        "default": "line-main",
                        "help": "Rumi 側の endpoint 識別子です。LINE の channel ID ではありません。",
                    },
                    {
                        "id": "public_url_launcher",
                        "label": "Temporary Public URL",
                        "type": "public_url",
                        "default": {
                            "provider_id": "cloudflare_quick_tunnel",
                            "local_url": "http://127.0.0.1:8766",
                            "route_path": "/api/integrations/line/webhook",
                        },
                        "help": "LINE/Slack/DiscordのWebhook URL欄へ貼る一時公開URLを発行します。Cloudflareはprovider実装の1つです。",
                    },
                    {
                        "id": "provider_route_copy",
                        "label": "Route Paths",
                        "type": "readonly",
                        "default": (
                            "LINE: /api/integrations/line/webhook\n"
                            "Discord: /api/integrations/discord/interactions, /api/integrations/discord/events\n"
                            "Slack: /api/integrations/slack/events"
                        ),
                        "help": "公開URLを作ったら、この path を provider 側 webhook URL の末尾としてコピペします。",
                    },
                    {
                        "id": "input_template_summary",
                        "label": "Input Templates",
                        "type": "readonly",
                        "default": "LINE / Discord / Slack / Generic / Custom",
                    },
                    {
                        "id": "input_profile_summary",
                        "label": "Input Profiles",
                        "type": "readonly",
                        "default": "No profiles",
                    },
                    {
                        "id": "include_source_context",
                        "label": "Include Source Context",
                        "type": "toggle",
                        "default": True,
                        "help": "外部入力をchatへ渡す時に、LINE/Discord/Slackなど送信元を既定で伝えます。",
                    },
                    {
                        "id": "default_response_mode",
                        "label": "Default Response",
                        "type": "select",
                        "default": "same_response",
                        "options": [
                            {"value": "same_response", "label": "Reply to source conversation"},
                            {"value": "custom_prompt", "label": "Custom prompt"},
                            {"value": "store_only", "label": "Store only"},
                        ],
                        "help": "LINE では replyToken を使って受信元の個人/グループ/複数人トークへ返信します。",
                    },
                    {
                        "id": "input_response_preset",
                        "label": "Input Response Preset",
                        "type": "select",
                        "default": "same_source_reply",
                        "options": [
                            {"value": "same_source_reply", "label": "Same source reply"},
                            {"value": "store_only", "label": "Store only"},
                            {
                                "value": "push_to_remembered_source",
                                "label": "Push to remembered source",
                            },
                            {"value": "line_to_discord", "label": "LINE -> Discord"},
                            {"value": "line_to_web", "label": "LINE -> Web/local"},
                            {"value": "browser_then_reply", "label": "Browser use -> reply"},
                            {"value": "python_then_reply", "label": "Python -> reply"},
                            {"value": "computer_use_line_biz", "label": "Computer use -> LINE Biz"},
                        ],
                        "help": "Same source reply は送信先ID入力不要です。Push は保存済み source の許可がある時だけ使います。",
                    },
                    {
                        "id": "policy_summary",
                        "label": "Audience Policies",
                        "type": "readonly",
                        "default": "line.production: verified text only, saved source allowed, unknown source denied.",
                    },
                    {
                        "id": "saved_sources_summary",
                        "label": "Saved Sources",
                        "type": "readonly",
                        "default": "No saved sources",
                        "help": "LINE の user/group/room source は webhook 受信時に自動保存されます。push は許可済み source のみ使います。",
                    },
                ],
            },
            {
                "id": "external_output",
                "label": "External Output",
                "description": "返信・転送先。LINE/Discord/Slack/Webを選び、秘密値はExternal Tokensに貼ります。",
                "fields": [
                    {
                        "id": "output_setup_guide",
                        "label": "Send Modes",
                        "type": "readonly",
                        "default": (
                            "LINE: Messaging API Channel Access Tokenで受信元へreply。push fallbackは既定OFF\n"
                            "Discord Bot + Channel: Bot Tokenを保存し、Channel IDをTarget IDへ貼る\n"
                            "Discord Webhook URL: Channel Webhook URLをExternal Tokensへ保存する\n"
                            "Slack: Bot Tokenを保存し、Channel ID / Thread TSをTarget IDへ貼る\n"
                            "Web/local: 外部投稿せず、chat historyやlocal保存に寄せる"
                        ),
                    },
                    {
                        "id": "external_tokens",
                        "label": "External Tokens (read-only)",
                        "type": "external_tokens",
                        "default": [],
                        "help": "ここでは設定しません。APIs / Tokens で provider に LINE / Discord / Slack を選んで保存してください。保存済みのものはここに自動で表示されます。",
                    },
                    {
                        "id": "output_provider",
                        "label": "Output Provider",
                        "type": "select",
                        "default": "line",
                        "options": self._provider_options(
                            output_templates,
                            fallback=["line", "discord", "slack", "generic", "web"],
                        ),
                        "help": "返信・転送先 provider を選びます。LINE の送信先は channel ではなく source conversation です。",
                    },
                    {
                        "id": "output_template_id",
                        "label": "Output Template",
                        "type": "select",
                        "default": "line.output.default",
                        "options": self._template_options(output_templates, include_custom=False),
                        "help": "Discord は bot+channel と webhook URL を選択で切り替えます。",
                    },
                    {
                        "id": "output_profile_id",
                        "label": "Output Profile",
                        "type": "select",
                        "default": "line.default",
                        "options": output_profile_options,
                        "help": "送信能力、文字数上限、reply/push mode を決める response profile です。",
                    },
                    {
                        "id": "output_send_mode",
                        "label": "Send Mode",
                        "type": "select",
                        "default": "reply_to_origin",
                        "options": [
                            {"value": "reply_to_origin", "label": "Reply to source conversation"},
                            {"value": "push_to_saved_origin", "label": "Push to remembered source"},
                            {
                                "value": "push_to_explicit_target",
                                "label": "Push to explicit target",
                            },
                            {"value": "discord_bot_channel", "label": "Discord bot + channel_id"},
                            {"value": "discord_webhook_url", "label": "Discord webhook URL"},
                            {"value": "slack_channel", "label": "Slack channel/thread"},
                            {"value": "generic_webhook", "label": "Generic webhook"},
                            {"value": "web_local", "label": "Web / local only"},
                            {"value": "tool_external_send", "label": "Tool: external_send"},
                        ],
                    },
                    {
                        "id": "output_target_id",
                        "label": "Explicit Target ID",
                        "type": "text",
                        "default": "",
                        "help": "明示送信時だけ使います。LINE は userId / groupId / roomId、Discord/Slack は channel_id。Webhook URLはExternal Tokensへ保存します。",
                    },
                    {
                        "id": "output_callback_token_id",
                        "label": "Token ID To Use",
                        "type": "text",
                        "default": "main",
                        "help": "webhook URL や bot token は External Tokens に保存し、ここには token_id だけを書きます。",
                    },
                    {
                        "id": "output_template_summary",
                        "label": "Output Templates",
                        "type": "readonly",
                        "default": "Discord bot/channel or webhook URL, LINE source reply or explicit push, Slack channel, Generic webhook, Web/local.",
                    },
                    {
                        "id": "output_profile_summary",
                        "label": "Output Profiles",
                        "type": "readonly",
                        "default": "Provider capabilities drive response planning.",
                    },
                    {
                        "id": "response_summary",
                        "label": "Response Prompt Policy",
                        "type": "readonly",
                        "default": "Prompt decisions create action plans; tools/adapters execute after policy checks.",
                    },
                    {
                        "id": "response_prompt_preset",
                        "label": "Response Prompt Preset",
                        "type": "select",
                        "default": "same_source_reply",
                        "options": [
                            {"value": "same_source_reply", "label": "Same source reply"},
                            {"value": "summarize_then_reply", "label": "Summarize then reply"},
                            {
                                "value": "run_browser_use",
                                "label": "Browser use when current info is needed",
                            },
                            {
                                "value": "run_python",
                                "label": "Python for calculation / file processing",
                            },
                            {
                                "value": "run_computer_use_approval",
                                "label": "Computer use with approval",
                            },
                            {
                                "value": "send_file_if_allowed",
                                "label": "Send file if provider allows",
                            },
                            {"value": "store_only", "label": "Store only"},
                        ],
                        "help": "プロンプト routing もビルトインはプリセット選択にします。自由文は External Custom 側に置きます。",
                    },
                    {
                        "id": "public_url_summary",
                        "label": "Temporary Public URLs",
                        "type": "readonly",
                        "default": "Providers: static, cloudflare_quick_tunnel",
                    },
                ],
            },
            {
                "id": "external_custom",
                "label": "External Custom",
                "description": "Custom input/output templates loaded from registration API or extension files.",
                "fields": [
                    {
                        "id": "custom_template_path",
                        "label": "Template Extension Path",
                        "type": "readonly",
                        "default": "user_data/shared/external_io_templates",
                    },
                    {
                        "id": "custom_profile_paths",
                        "label": "Profile Extension Paths",
                        "type": "readonly",
                        "default": "user_data/shared/input_profiles, user_data/shared/output_profiles",
                    },
                    {
                        "id": "custom_prompt_examples",
                        "label": "Custom Prompt Examples",
                        "type": "textarea",
                        "default": "",
                        "help": "例: Google Chromeをcomputer_useで操作して起動し、指定のLINE Official Account Manager URLにアクセスして返答する。",
                    },
                ],
            },
            {
                "id": "triggers",
                "label": "Triggers",
                "description": "発火判断と、入力に関係ない候補を落とすための設定。",
                "fields": [
                    {
                        "id": "mode",
                        "label": "Trigger Mode",
                        "type": "select",
                        "default": "vector",
                        "options": [
                            {"value": "vector", "label": "Vector / memo match"},
                            {"value": "llm", "label": "LLM decides"},
                        ],
                        "help": "発火要因をベクトル/メモ照合で見るか、LLMに判断させるかを選びます。",
                    },
                    {
                        "id": "filter_unrelated",
                        "label": "Filter Unrelated",
                        "type": "toggle",
                        "default": False,
                        "help": "LLM判断時に、発火候補と入力が無関係なら候補を落とすためのフラグです。",
                    },
                    {
                        "id": "model",
                        "label": "Trigger LLM",
                        "type": "text",
                        "default": "",
                        "help": "空なら現在の既定モデルを継承します。",
                        "advanced": True,
                    },
                    {
                        "id": "vector_threshold",
                        "label": "Vector Threshold",
                        "type": "number",
                        "default": 0.1,
                        "min": 0,
                        "max": 1,
                        "help": "vector mode の発火候補スコアしきい値です。外部返信の既定動作は維持します。",
                        "advanced": True,
                    },
                ],
            },
            {
                "id": "tools",
                "label": "機能と接続",
                "description": "機能の既定動作、権限、接続、高度な選定方式。",
                "fields": [
                    {
                        "id": "default_target",
                        "label": "Default Target",
                        "type": "text",
                        "default": "",
                        "help": "Backcompat value for tool UIs that still read a shared default_target.",
                        "advanced": True,
                    },
                    {
                        "id": "default_mode",
                        "label": "既定の使い方",
                        "type": "select",
                        "default": "auto",
                        "options": [
                            {"value": "auto", "label": "自動で選ぶ"},
                            {"value": "review", "label": "使う前に確認"},
                            {"value": "manual", "label": "自分で選ぶ"},
                            {"value": "none", "label": "機能を使わない"},
                        ],
                    },
                    {
                        "id": "selection_strategy",
                        "label": "選定方式",
                        "type": "select",
                        "default": "hybrid",
                        "options": [
                            {"value": "hybrid", "label": "自動選定・高精度"},
                            {"value": "semantic", "label": "意味検索"},
                            {"value": "catalog_ai", "label": "別AIに全体から選ばせる"},
                            {"value": "all_with_hints", "label": "全機能＋おすすめ"},
                            {"value": "all_schemas", "label": "全schemaを公開・デバッグ"},
                            {"value": "lexical", "label": "軽量検索"},
                        ],
                        "help": "通常は自動選定・高精度のままで構いません。",
                        "advanced": True,
                    },
                    {
                        "id": "show_selection_summary",
                        "label": "選んだ機能を回答内に表示",
                        "type": "toggle",
                        "default": True,
                    },
                    {
                        "id": "show_selection_reasons",
                        "label": "選定理由を常に展開して表示",
                        "type": "toggle",
                        "default": False,
                    },
                    {
                        "id": "semantic_backend",
                        "label": "Semantic backend",
                        "type": "select",
                        "default": "auto",
                        "options": [
                            {"value": "auto", "label": "自動"},
                            {"value": "embedding", "label": "Embedding"},
                            {"value": "lexical", "label": "軽量検索"},
                        ],
                        "advanced": True,
                    },
                    {
                        "id": "selector_trace",
                        "label": "Trace",
                        "type": "select",
                        "default": "summary",
                        "options": [
                            {"value": "none", "label": "保存しない"},
                            {"value": "summary", "label": "要約のみ"},
                            {"value": "full", "label": "完全トレース"},
                        ],
                        "advanced": True,
                    },
                    {
                        "id": "final_tool_limit",
                        "label": "最終機能数",
                        "type": "number",
                        "default": 8,
                        "min": 1,
                        "max": 24,
                        "advanced": True,
                    },
                    {
                        "id": "semantic_candidate_limit",
                        "label": "Semantic候補数",
                        "type": "number",
                        "default": 32,
                        "min": 8,
                        "max": 64,
                        "advanced": True,
                    },
                ],
            },
            {
                "id": "computer_use_haze",
                "label": "Computer Use Haze",
                "description": "Visible edge glow while computer-use performs screen-mutating actions.",
                "fields": [
                    {
                        "id": "enabled",
                        "label": "Enable Haze",
                        "type": "toggle",
                        "default": True,
                        "help": "computer use の可視操作中、画面端にクリック透過のもやもやを表示します。",
                    },
                    {
                        "id": "preset",
                        "label": "Gradient Preset",
                        "type": "select",
                        "default": "aurora",
                        "options": [
                            {"value": "aurora", "label": "Aurora"},
                            {"value": "ocean", "label": "Ocean"},
                            {"value": "ember", "label": "Ember"},
                            {"value": "custom", "label": "Custom"},
                        ],
                    },
                    {
                        "id": "start_color",
                        "label": "Start Color",
                        "type": "color",
                        "default": "#6EE7F9",
                    },
                    {
                        "id": "end_color",
                        "label": "End Color",
                        "type": "color",
                        "default": "#A78BFA",
                    },
                    {
                        "id": "accent_color",
                        "label": "Accent Color",
                        "type": "color",
                        "default": "#F0ABFC",
                    },
                    {
                        "id": "opacity",
                        "label": "Opacity",
                        "type": "number",
                        "default": 0.36,
                        "min": 0.05,
                        "max": 0.9,
                    },
                    {
                        "id": "edge_width",
                        "label": "Edge Width",
                        "type": "number",
                        "default": 150,
                        "min": 40,
                        "max": 420,
                        "advanced": True,
                    },
                    {
                        "id": "animation_speed",
                        "label": "Animation Speed",
                        "type": "number",
                        "default": 1,
                        "min": 0.1,
                        "max": 4,
                        "advanced": True,
                    },
                ],
            },
            {
                "id": "debug",
                "label": "Debug",
                "description": "モデル呼び出しとcomputer use調査用のログ設定。",
                "fields": [
                    {
                        "id": "ai_request_logging",
                        "label": "AI Request Logs",
                        "type": "toggle",
                        "default": False,
                        "help": "AIに渡すmessages/tools/paramsと添付画像を会話workspace/debug/ai_requestsへ保存します。",
                    },
                ],
            },
            {
                "id": "system_info",
                "label": "System Info",
                "description": "App version and macOS privacy permissions used by Computer Use.",
                "fields": [],
            },
        ]

        sections.extend(self._config_list(ui_surfaces, "settings_sections"))
        sections.extend(self._config_list(extensions, "settings_sections"))

        return self._suppress_template_owned_base_settings(sections, template_catalog)

    @staticmethod
    def _template_settings_field_ids(
        template_catalog: dict[str, Any] | None,
    ) -> set[tuple[str, str]]:
        if not isinstance(template_catalog, dict):
            return set()
        owned: set[tuple[str, str]] = set()
        sections = template_catalog.get("settings_sections")
        if not isinstance(sections, list):
            return owned
        for section in sections:
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("id") or "").strip()
            if not section_id:
                continue
            fields = section.get("fields")
            if not isinstance(fields, list):
                continue
            for field in fields:
                if not isinstance(field, dict):
                    continue
                field_id = str(field.get("id") or "").strip()
                if field_id:
                    owned.add((section_id, field_id))
        return owned

    def _suppress_template_owned_base_settings(
        self,
        sections: list[dict[str, Any]],
        template_catalog: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        template_owned = self._template_settings_field_ids(template_catalog)
        if not template_owned:
            return sections
        filtered_sections: list[dict[str, Any]] = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("id") or "").strip()
            fields = section.get("fields")
            if not section_id or not isinstance(fields, list):
                filtered_sections.append(section)
                continue
            next_section = dict(section)
            next_section["fields"] = [
                field
                for field in fields
                if not (
                    isinstance(field, dict)
                    and (section_id, str(field.get("id") or "").strip()) in template_owned
                )
            ]
            filtered_sections.append(next_section)
        return filtered_sections

    def _config_list(self, manifests: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for manifest in manifests:
            config = manifest.get("config", manifest)
            if not isinstance(config, dict):
                continue
            items = config.get(key, [])
            if not isinstance(items, list):
                continue
            values.extend(item for item in items if isinstance(item, dict))
        return values

    @staticmethod
    def _provider_options(templates: list[Any], *, fallback: list[str]) -> list[dict[str, str]]:
        providers: list[str] = []
        for item in templates:
            if not isinstance(item, dict):
                continue
            if item.get("origin") == "custom" or item.get("provider") == "custom":
                continue
            provider = str(item.get("provider") or "").strip()
            if provider and provider not in providers:
                providers.append(provider)
        if not providers:
            providers = list(fallback)
        return [{"value": provider, "label": provider} for provider in providers]

    @staticmethod
    def _template_options(templates: list[Any], *, include_custom: bool) -> list[dict[str, str]]:
        options: list[dict[str, str]] = []
        for item in templates:
            if not isinstance(item, dict):
                continue
            if not include_custom and (
                item.get("origin") == "custom" or item.get("provider") == "custom"
            ):
                continue
            template_id = str(item.get("id") or "").strip()
            if not template_id:
                continue
            provider = str(item.get("provider") or "").strip()
            display_name = str(item.get("display_name") or template_id).strip()
            options.append(
                {
                    "value": template_id,
                    "label": f"{provider} / {display_name}" if provider else display_name,
                }
            )
        return options or [{"value": "", "label": "No templates"}]
