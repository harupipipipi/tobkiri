# AI Providers ガイド

## 1. Provider Source Of Truth

defaultspack の ai_client は manifest-first で provider を解決する。OpenAI-compatible provider は
`extensions/llm/providers/<provider_id>/manifest.json` と `models/*.json` だけで追加できる。
Python provider class は独自プロトコルが必要な場合だけ使う。

runtime 内の curated table は互換 fallback であり、新規 provider を追加する主経路ではない。

## 2. 対応プロバイダー一覧

| プロバイダー ID | 説明 |
|---|---|
| `openai` | OpenAI API（GPT-4o, GPT-4o-mini, o3, o4-mini 等） |
| `anthropic` | Anthropic API（Claude Opus 4, Sonnet 4, Haiku 3 等） |
| `google` | Google Gemini API（Gemini 2.5 Pro, Gemini 2.5 Flash 等） |
| `openrouter` | OpenRouter API（defaultspack bundled catalog allowlist） |
| `gitlawb-opengateway` | Gitlawb OpenGateway（MiMo の固定allowlist。全モデルで API key 必須） |
| `opencode-zen` | OpenCode Zen（account-visible live inventory。API key 必須） |
| `groq` | Groq OpenAI-compatible API |
| `cerebras` | Cerebras OpenAI-compatible API |
| `nvidia` | NVIDIA NIM OpenAI-compatible API |
| `moonshotai` | Moonshot AI OpenAI-compatible API |
| `xiaomi-mimo` | Xiaomi MiMo direct API の地域別 catalog entry（初期状態は実行不可） |
| `stub` | テスト用スタブ。固定レスポンスを返す |
| `rumi` | rumi 独自のメタプロバイダー（パイプライン、ルーティング、評価） |


## 3. 各プロバイダーの環境変数設定

### OpenAI

```bash
OPENAI_API_KEY=sk-...
# オプション:
OPENAI_BASE_URL=https://api.openai.com/v1    # カスタムエンドポイント
OPENAI_ORG_ID=org-...                         # 組織ID
```

### Anthropic

```bash
ANTHROPIC_API_KEY=sk-ant-...
# オプション:
ANTHROPIC_BASE_URL=https://api.anthropic.com  # カスタムエンドポイント
```

### Google

```bash
GOOGLE_API_KEY=AIza...
# または:
GEMINI_API_KEY=AIza...
# オプション:
GOOGLE_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
```

### OpenRouter

defaultspack の OpenRouter 統合は、bundled catalog allowlist に載っているモデルを実行対象にする。
API キーはデスクトップ UI の Settings から保存できる。値は `user_data/secrets/OPENROUTER_API_KEY.json`
に暗号化保存され、フロントエンドには保存済みかどうかだけが返る。

```bash
OPENROUTER_API_KEY=sk-or-...
# オプション:
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
```

### Gitlawb OpenGateway

Gitlawb OpenGateway は OpenAI-compatible な外部 gateway。現在は全モデルで API key が必須なので、Rumi runtime では `GITLAWB_OPENGATEWAY_API_KEY` として保存・送信する。クラウド provider として扱うため、runtime で使うには明示的な opt-in が必要。

```bash
RUMI_DEFAULTSPACK_ENABLE_CLOUD_PROVIDERS=1
GITLAWB_OPENGATEWAY_API_KEY=ogw_live_...
# オプション:
GITLAWB_OPENGATEWAY_BASE_URL=https://opengateway.gitlawb.com/v1
```

利用できるモデルは固定 allowlist のみ。

| モデル名 | 特徴 |
|---|---|
| `gitlawb-opengateway/mimo-v2.5-pro` | reasoning 用の MiMo V2.5 Pro |
| `gitlawb-opengateway/mimo-v2-flash` | 高速な MiMo V2 Flash |
| `gitlawb-opengateway/mimo-v2-omni` | 画像入力対応の MiMo V2 Omni |
| `gitlawb-opengateway/mimo-v2-pro` | reasoning 用の MiMo V2 Pro |
| `gitlawb-opengateway/mimo-v2.5` | reasoning 用の MiMo V2.5 |

`mimo` 系は Gitlawb OpenGateway の OpenAI-compatible `POST /v1/chat/completions` に送信され、違いは `model` のみ。`mimo-v2-omni` の画像認識では OpenAI 互換の `content` 配列形式を使う。runtime は gateway 互換性のため Browser User-Agent を付与する。

汎用 OpenAI-compatible client から直接使う場合は、base URL と API key を OpenAI 互換の環境変数に割り当てられる。

```bash
OPENAI_BASE_URL=https://opengateway.gitlawb.com/v1
OPENAI_API_KEY=ogw_live_...
OPENAI_MODEL=mimo-v2-omni
```

### OpenCode Zen

OpenCode Zen は account-visible な `/v1/models` inventory を authority とし、
`OPENCODE_ZEN_API_KEY` が設定された host provider 経路だけで実行する。

```bash
OPENCODE_ZEN_API_KEY=...
# オプション:
OPENCODE_ZEN_BASE_URL=https://opencode.ai/zen
OPENCODE_ZEN_USER_AGENT="Mozilla/5.0 ..."
```

Cloudflare 互換性のため、chat-completions と messages の両方に
browser-compatible User-Agent を既定で送る。`OPENCODE_ZEN_USER_AGENT` は
明示設定時だけその値で上書きする。

`opencode-zen/mimo-v2.5-free` の公開 `stream()` は、長時間 pending SSE を避ける
ため bounded non-stream chat completion を実行し、結果を標準 stream event に
変換する。空 completion、nameless tool call、provider/network failure は成功として
終端せず、runtime error として返す。他の account-visible model は native SSE path を
維持する。

### Cloud OpenAI-compatible providers

Groq / Cerebras / NVIDIA NIM / Moonshot AI は manifest-first provider として追加されている。API key が設定されている場合、`detect_available_providers()` で runtime provider として検出される。

```bash
GROQ_API_KEY=...
CEREBRAS_API_KEY=...
NVIDIA_API_KEY=...      # または NGC_API_KEY
MOONSHOT_API_KEY=...
```

service tier や preview/enterprise-only 条件は metadata に保持するだけで、初期実装では request body に自動注入しない。

### Xiaomi MiMo direct API

`xiaomi-mimo` は Gitlawb OpenGateway とは分離された umbrella catalog entry。direct API は `xiaomi-mimo-global` と `xiaomi-mimo-cn` に地域分割し、公式 base URL / auth / token grant 条件が確認されるまでは runtime provider として自動有効化しない。

MiMo token plan は provider catalog の `subscription_plans` として保持する。現時点では `mimo_orbit_100t_grant_if_available` を catalog metadata として公開するだけで、manual signup / region scoped / do not auto enable の扱いにする。

警告: この provider を実行有効化すると、プロンプト、会話履歴、tool結果などのコンテキストが Xiaomi MiMo direct API に送信される。

### stub

環境変数不要。テスト用。

### rumi

```bash
# rumi プロバイダーは他のプロバイダーの API キーを使用する。
# rumi 固有の設定は user_data/shared/ai_models/rumi/ に配置。
```

環境変数の設定は `user_data/config.json` の `ai.providers` セクション、または OS の環境変数で行う。`config.json` の値が優先される。


## 4. 各プロバイダーで使えるモデル一覧

### OpenAI

| モデル名 | 特徴 |
|---|---|
| `gpt-4o` | フラグシップ。マルチモーダル対応 |
| `gpt-4o-mini` | 軽量・高速・低コスト |
| `o3` | 推論特化（reasoning tokens） |
| `o4-mini` | 推論特化・軽量版 |
| `gpt-4.1` | 最新世代（利用可能な場合） |
| `gpt-4.1-mini` | 最新世代・軽量版 |
| `gpt-4.1-nano` | 最新世代・最軽量 |

### Anthropic

| モデル名 | 特徴 |
|---|---|
| `claude-opus-4-20250514` | 最高性能。extended thinking 対応 |
| `claude-sonnet-4-20250514` | バランス型。extended thinking 対応 |
| `claude-haiku-3-20250307` | 高速・低コスト |

### Google

| モデル名 | 特徴 |
|---|---|
| `gemini-2.5-pro` | フラグシップ。thinking 対応 |
| `gemini-2.5-flash` | 高速・低コスト |
| `gemini-2.0-flash` | 安定版 |

### stub

| モデル名 | 特徴 |
|---|---|
| `stub/echo` | 入力をそのまま返す |
| `stub/fixed` | 固定テキストを返す |
| `stub/error` | 常にエラーを返す |

### rumi

| モデル名 | 特徴 |
|---|---|
| `rumi/pipeline` | 複数モデルをパイプライン実行 |
| `rumi/router` | タスクに応じたモデル自動選択 |
| `rumi/moa` | Mixture of Agents（複数モデルの合議） |
| `rumi/eval` | 生成結果の自動評価・リランキング |


## 5. モデル指定方法

### "provider/model" 形式

モデルは `"provider/model"` 形式の文字列で指定する。

```
"openai/gpt-4o"
"anthropic/claude-sonnet-4-20250514"
"google/gemini-2.5-pro"
"stub/echo"
"rumi/router"
```

provider を省略した場合、ai_client が既知のモデル名からプロバイダーを自動推定する。`"gpt-4o"` は `"openai/gpt-4o"` に解決される。

### プロファイル名

`user_data/shared/ai_models/` に配置したプロファイル名でも指定できる。

```
"fast"          → config で定義されたプロファイル
"reasoning"     → config で定義されたプロファイル
"coding"        → config で定義されたプロファイル
```

これらは agent.json の `model` セクションで使われる。

```json
{
  "model": {
    "default": "claude-sonnet-4-20250514",
    "fallback": "gpt-4o-mini",
    "fast": "claude-haiku",
    "reasoning": "claude-opus-4-20250514"
  }
}
```

## 6. Tokenizer metadata と prompt token count

Prompt Studio と右サイドバーの prompt widget は、選択中の LLM
プロファイルに合わせて prompt token 数を再計算する。Studio は
`model_profile_id` を明示的に選択でき、widget はチャット composer で現在選択されている
モデルを使う。widget 側にはモデル選択 UI を置かない。

Provider / model profile は任意で tokenizer metadata を持てる。

```yaml
metadata:
  tokenizer:
    kind: char_divisor
    characters_per_token: 3.2
    tokenizer_id: provider.model.approx
```

利用できる安全な形式:

- `metadata.tokenizer.kind: whitespace`
- `metadata.tokenizer.kind: char_divisor` と `characters_per_token`
- `metadata.tokenizer.kind: byte_divisor` と `bytes_per_token`
- `metadata.tokenizer.encoding` または `tokenizer_id`（ローカル tokenizer ライブラリがある場合）
- `metadata.tokenizer_profile_id` / `metadata.tokenizer_model_profile_id`
  による別プロファイル tokenizer の参照

同じ model id を複数 provider が提供している場合、選択中プロファイルに tokenizer がなくても、
同じ `same_model_across_providers_key` を持つ別 provider profile に tokenizer があればそちらを使う。
この場合 API は `tokenizer.source: "same_model_provider"` を返す。

見つからない場合は `defaultspack.approximate` を使い、
`warning_code: "missing_tokenizer"` を返す。UI はモデル名の近くに警告を表示し、
hover で「モデルの tokenizer が見つからないため、デフォルトの tokenizer を使用しています。
大きくズレる可能性があります。」相当の説明を出す。

Tokenizer は純粋なカウント用 metadata であり、権限・tool・provider routing・chat state を変更しない。
prompt text から tool 実行や provider 呼び出しを許可してはいけない。


## 7. プロファイルの設定方法

プロファイルは `user_data/shared/ai_models/{provider_id}/profiles/{profile_name}/` に配置する。

```
user_data/shared/ai_models/
├── openai/
│   └── profiles/
│       ├── gpt-4o/
│       │   ├── profile.json
│       │   └── ui/
│       │       └── events.ui.yaml
│       └── o3/
│           └── profile.json
├── anthropic/
│   └── profiles/
│       ├── claude-sonnet-4/
│       │   ├── profile.json
│       │   └── ui/
│       │       └── events.ui.yaml
│       └── claude-opus-4/
│           └── profile.json
└── rumi/
    └── profiles/
        └── router/
            └── profile.json
```

### profile.json の構造

```json
{
  "model_id": "claude-sonnet-4-20250514",
  "provider": "anthropic",
  "display_name": "Claude Sonnet 4",
  "capabilities": {
    "tool_calls": true,
    "vision": true,
    "thinking": true,
    "streaming": true,
    "json_mode": true
  },
  "context_length": 200000,
  "max_output_tokens": 64000,
  "pricing": {
    "input_per_1m_tokens": 3.00,
    "output_per_1m_tokens": 15.00,
    "cached_input_per_1m_tokens": 1.50,
    "currency": "USD"
  },
  "default_params": {
    "temperature": 0.7,
    "max_tokens": 8192
  },
  "thinking_config": {
    "budget_tokens": 10000
  }
}
```

`ui/events.ui.yaml` はストリーミング中のアニメーション Widget を定義する任意ファイルである。詳細は ai_client.md を参照。


## 8. 対応機能マトリクス

| 機能 | OpenAI | Anthropic | Google | stub | rumi |
|---|---|---|---|---|---|
| `defaults.ai.complete` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `defaults.ai.stream` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `defaults.ai.embed` | ✅ | ❌ | ✅ | ❌ | ❌ |
| `defaults.ai.image_gen` | ✅ (DALL-E) | ❌ | ✅ (Imagen) | ❌ | ❌ |
| `defaults.ai.image_analyze` | ✅ (vision) | ✅ (vision) | ✅ (vision) | ❌ | ✅ |
| `defaults.ai.transcribe` | ✅ (Whisper) | ❌ | ✅ | ❌ | ❌ |
| `defaults.ai.tts` | ✅ | ❌ | ✅ | ❌ | ❌ |
| tool_calls | ✅ | ✅ | ✅ | ✅ | ✅ |
| thinking/reasoning | ✅ (o3系) | ✅ (extended thinking) | ✅ (Gemini 2.5) | ❌ | ✅ |
| vision | ✅ | ✅ | ✅ | ❌ | ✅ |
| caching | ❌ | ✅ (ephemeral cache) | ✅ (context cache) | ❌ | ❌ |
| json_mode | ✅ | ✅ | ✅ | ❌ | ✅ |
| streaming | ✅ | ✅ | ✅ | ✅ | ✅ |


## 7. rumi モデルの概要

rumi プロバイダーは他のプロバイダーのモデルを組み合わせるメタプロバイダーである。ai_client から見ると通常のプロバイダーと同じインターフェースを持つが、内部で複数のモデルを呼び出す。

### rumi/pipeline — パイプライン

複数モデルを直列に実行する。前段の出力を後段の入力に渡す。

```json
{
  "model_id": "rumi/pipeline",
  "config": {
    "stages": [
      {"model": "openai/o3", "role": "planner"},
      {"model": "anthropic/claude-sonnet-4", "role": "executor"}
    ]
  }
}
```

### rumi/router — ルーティング

タスクの種類に応じてモデルを自動選択する。分類にはルーティングモデル（軽量モデル）を使用する。

```json
{
  "model_id": "rumi/router",
  "config": {
    "classifier_model": "openai/gpt-4o-mini",
    "routes": {
      "coding": "anthropic/claude-sonnet-4",
      "reasoning": "openai/o3",
      "creative": "anthropic/claude-opus-4",
      "simple": "openai/gpt-4o-mini"
    }
  }
}
```

### rumi/moa — Mixture of Agents

同一のプロンプトを複数モデルに送信し、結果を合議して最終回答を生成する。

```json
{
  "model_id": "rumi/moa",
  "config": {
    "agents": [
      "openai/gpt-4o",
      "anthropic/claude-sonnet-4",
      "google/gemini-2.5-pro"
    ],
    "synthesizer": "anthropic/claude-opus-4"
  }
}
```

### rumi/eval — 評価

生成結果を評価モデルで採点し、最も高スコアの結果を返す。

```json
{
  "model_id": "rumi/eval",
  "config": {
    "generator": "anthropic/claude-sonnet-4",
    "evaluator": "openai/o3",
    "num_candidates": 3
  }
}
```

rumi プロバイダーの定義は全て `user_data/shared/ai_models/rumi/profiles/` に配置される。defaults は rumi プロバイダーの実行エンジンの仕組みだけを提供し、具体的なパイプライン構成は user_data 側で定義する。
