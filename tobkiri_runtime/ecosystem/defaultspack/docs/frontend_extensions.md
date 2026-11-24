# defaultspack Frontend Extensions

`defaultspack` の standalone frontend は「具体 UI を本体が知る」のではなく、backend が返す registry を読んで shell layout・右バー・設定・chat renderer を構成する。

新しい settings field、composer input、AI input、tool policy、context policy、
slash command、backend binding を組み合わせる場合は、まず
[templates.md](templates.md) の RumiTemplate catalog を使う。frontend extension
manifest は、承認済み pack が独自 renderer/module を配布する必要がある場合の
別レイヤーであり、template JSON から任意 module を直接実行する経路ではない。

## まず知っておくこと

- backend contract は `domain/frontend/registry.py` (`FrontendRegistry.build_catalog`)
- standalone frontend は `webapp/src/App.tsx`
- right sidebar は `webapp/src/components/RightSidebar.tsx`
- settings は `/api/ui/settings`
- preview feed は `/api/ui/conversations/{id}/preview`
- shell layout は active な Pack v4 Profile/ShellDefinition と、有効化された
  pack の manifest (`shell_layout` / `shell_renderers`) だけで決まる。
  **mutable な `user_data` JSON は layout authority ではない**
  (`FrontendRegistry._load_shell_config()` は常に `{}` を返す)。

## Sealed model: 読み込み順と有効化

backend extension manifest (`extensions/` 配下の JSON) は次の順で読み込まれる
(`domain/extensions/runtime.py` の `build_extensions_roots`)。

1. `ecosystem/defaultspack/extensions/`
2. active Profile の effective set に含まれる sibling pack の `extensions/`

frontend extension manifest (`*.ui.json`) は次の 2 系統だけから読み込まれる
(`domain/frontend/registry.py` の `_frontend_extension_dirs`)。

1. **有効化された pack の `frontend_extensions/`**
   `ecosystem/<pack_id>/frontend_extensions/*.ui.json`。対象は
   `selected_extension_pack_ids()` が返す pack ID、つまり検証済み Pack v4
   activation の effective set (`core_runtime.resolved_profile_scope.effective_pack_ids()`)
   に含まれ、かつ `pack.v4.json` の `pack.id` がディレクトリ名と一致する
   pack だけ。
2. **digest 固定の ui_projection root**
   active Profile の `content_projections` のうち `kind="ui_projection"` の
   各 projection について、`profile_projections/<artifact_root>/frontend_extensions/*.ui.json`
   (`selected_projection_roots`)。projection root は読み込み時に
   `content_digest` で再検証され、digest がずれていれば fail closed する。

この loader が読まないもの (書いても何も起きない):

- `user_data/shared/frontend_extensions/*.ui.json` — user overlay 経路は廃止。
- `user_data/shared/frontend_shell.json` — `_load_shell_config()` は常に `{}`。
- effective set に入っていない sibling pack の `frontend_extensions/`。
- `user_data/settings/setup_pack_selection.json` — 旧 selection file は
  live な選択 authority ではなく、Profile への一方通行の移行入力に過ぎない。
  selection file が無い dev 環境で「全 sibling pack を読む」fallback も存在しない。
  Profile が active でない限り `effective_pack_ids()` は空で、pack の
  `frontend_extensions/` は一切読まれない。

テストで一時 manifest を使う場合だけ、backend manifest は
`build_extensions_roots(..., extra_roots=...)` に明示的に渡す。frontend
`.ui.json` 側の test seam は `domain.frontend.registry.selected_extension_pack_ids`
の patch (例: `tests/test_defaultspack_ui_registry.py`)。

## 拡張ポイント

### 0. Shell layout / renderer を差し替える

`shell_layout` と `shell_renderers` の schema 自体は有効だが、置き場所は
**有効化された pack の `frontend_extensions/*.ui.json`** (または component の
`ui_surfaces` manifest の `ui` config) である。

```json
{
  "shell_layout": {
    "id": "compact",
    "regions": [
      { "id": "title_bar", "part_id": "app_chrome", "renderer": "title_bar", "slot": "top", "order": 10, "enabled": true },
      { "id": "history", "part_id": "conversation_history", "renderer": "history_board", "slot": "left", "order": 20, "enabled": false },
      { "id": "chat_messages", "part_id": "ai_chat", "renderer": "chat_messages", "slot": "main", "order": 40, "enabled": true },
      { "id": "composer", "part_id": "ai_chat", "renderer": "composer", "slot": "bottom", "order": 50, "enabled": true }
    ]
  }
}
```

`shell_renderers` は renderer ID と frontend component 名の契約を表す。builtin
renderer は `webapp/src/renderers/` に分かれている。`module` による lazy load
は frontend 側でさらに厳しく gate されている (`webapp/src/renderers/trustedRendererLoader.tsx`):
same-origin の `/static/renderers/` と `/static/assets/renderers/` 配下の
`.js` だけが対象で、さらに `verified: true` と backend 検証済みの builtin
provenance (`provenance.source: "builtin"`, `content_hash`, `build_id`) が必須。
`/static/user_renderers/` は frontend の trust list に無く、これらの prefix は
sealed frontend bundle 内を指すため、manifest から任意 JS を載せる経路は
事実上存在しない。読み込み条件を満たさない renderer は error boundary /
quarantine で builtin fallback に戻る。

```json
{
  "shell_renderers": [
    {
      "id": "composer",
      "component": "Composer",
      "regions": ["composer"],
      "fallback": "hidden"
    }
  ]
}
```

`/api/ui/catalog` は壊れた `parts`, `component_bindings`, `shell_layout`, `shell_renderers` を `diagnostics` として返す。frontend は診断を表示・記録できるが、manifest 全体を強制的には拒否しない。

### 1. 右バーに項目を追加する

有効化された pack の `frontend_extensions/<name>.ui.json` に `sidebar_items` を追加する
(現行 Profile で実際に読まれている例:
`ecosystem/rumi_model_catalog_pack/frontend_extensions/provider_catalog.ui.json`)。

```json
{
  "sidebar_items": [
    {
      "id": "weather-widget",
      "label": "Weather",
      "category": "widget",
      "description": "天気 widget の状態と設定",
      "panel": {
        "kind": "info",
        "title": "Weather",
        "notes": [
          "ここに widget の説明や導線を置ける"
        ],
        "fields": [
          {
            "id": "city",
            "label": "City",
            "type": "text",
            "default": "Tokyo"
          }
        ]
      }
    }
  ]
}
```

`category` の既知値は `widget`, `activity`, `capability`, `integration`,
`system`, `tool` (sort 順は `_sidebar_item_sort_key`)。未知の category は最後に回る。

### 2. 設定を追加する

同じ manifest に `settings_sections` を追加する。

```json
{
  "settings_sections": [
    {
      "id": "weather",
      "label": "Weather",
      "description": "天気系 widget の共通設定",
      "fields": [
        {
          "id": "units",
          "label": "Units",
          "type": "select",
          "default": "metric",
          "options": [
            { "value": "metric", "label": "Metric" },
            { "value": "imperial", "label": "Imperial" }
          ]
        }
      ]
    }
  ]
}
```

frontend は schema を見て form を自動生成し、値は `PUT /api/ui/settings` で
明示的に bind された settings owner 経由で永続化される。managed install での
durable file は `$RUMI_USER_DATA/defaultspack/shared/frontend_settings.json`
(`RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH` で override 可)。
`pack_root/user_data/shared/frontend_settings.json` は明示的な pack_root
binding (主にテスト) の場合の互換 path であり、書き込み authority を与えない。

### 3. Chat 描画を拡張する

registry は `chat_renderers` で「どの block/widget type をどの renderer が担当するか」の metadata を返す。

```json
{
  "chat_renderers": [
    {
      "id": "weather-card",
      "component": "WeatherCard",
      "block_types": ["weather"],
      "fallback": "json"
    }
  ]
}
```

この metadata 自体は契約で、実際の renderer 実装は builtin renderer registry
(`webapp/src/renderers/`, 現状の dispatch は `ChatMessagesRenderer.tsx`) に追加する。

今の builtin renderer:

- `text`, `markdown`
- `code`
- `image`
- `widget` fallback
- unknown block fallback (`json` / `text` / `hidden`)

### 4. Tool schema を右バーへ自動反映する

`ToolRegistry` に登録された tool は自動で right sidebar item になる。tool ごとの `schema.parameters` は panel field に変換される。

つまり tool を増やすだけでも右バーに項目が増える。

### 5. Preview feed を増やす

preview feed は次のソースを集約している。

- `Inspector` の `tools_called`
- `context_info.knowledge_results`
- `context_info.memory_results`
- message の `widget`
- message の `content` に含まれる `code` / `image`

新しい preview を増やしたいときは `domain/frontend/registry.py` の `_preview_from_log()` または `_preview_from_message()` を拡張する。

## 開発フロー: sidebar item を足すには

`.ui.json` は pack の sealed artifact set の一部 (`artifact-index.v4.json` に
`role: "asset"` として digest 列挙される) なので、編集後は reseal と
re-activation が必要。

1. `ecosystem/<pack_id>/frontend_extensions/<name>.ui.json` を編集する。
2. pack の v4 artifact を再生成して `artifact-index.v4.json` の digest を
   更新する (`python scripts/migrate_pack_artifacts_v4.py`)。
3. pack-set / Profile の activation ceremony (resolve → review → Authority
   approval → activation) をやり直して、新しい digest を active Profile に
   commit する。`/api/ui/catalog` は request ごとに `FrontendRegistry` を
   組み立てるので、activation 後の再起動不要で反映される。

ui_projection 側 (`profile_projections/<id>/frontend_extensions/`) を編集する
場合は、projection の `content_digest` が読み込み時に再検証されるため、
`scripts/generate_profile_artifacts.py` で Profile artifact を再生成してから
activate する。

まだ effective set に入っていない新しい pack は、source tree を置くだけでは
読まれない。Host-owned admission と pack-set transaction (`/api/setup/packs`
ceremony) を通して install/enable する必要がある
(docs/pack-development-guide.md 参照)。

## 設計方針

- frontend は「tool が何か」を知らない
- backend は「画面の完成形」を知らない
- 両者は registry/schema/preview contract だけで結ばれる
- 追加は manifest と renderer 実装の 2 箇所で済ませる
- manifest の供給元はすべて検証済み Pack v4 activation に縛られる。
  mutable な user_data ファイルは UI の layout/extension authority にならない

## 変更時の確認

```bash
cd tobkiri_runtime/ecosystem/defaultspack/webapp
npm test
npm run lint
npm run build

cd ../../..
python -m pytest tests/test_defaultspack_ui_registry.py
```
