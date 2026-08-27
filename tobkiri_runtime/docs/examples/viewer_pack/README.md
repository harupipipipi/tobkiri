# Viewer Example Pack

`viewer:display` capability を使って Tobkiri Launcher にフロントエンドを表示する Pack の最小限の例です。

## 概要

この Pack は以下を示します:

- `viewer.display` capability の宣言方法（`manifest.json` の `requires` と `vocab_aliases`）
- `grant_config` による Grant 設定
- `web_mount` によるフロントエンドの配信
- `calling_convention: "block"` の Function スタブ

## ディレクトリ構造

```
viewer_pack/
├── ecosystem.json                    # Pack 定義（web_mount, functions）
├── functions/
│   └── request_display/
│       ├── manifest.json             # viewer.display capability 宣言
│       └── main.py                   # Function スタブ（block）
├── web/
│   ├── index.html                    # Pack フロントエンド
│   └── style.css                     # スタイル
└── README.md                         # このファイル
```

## 各ファイルの役割

### ecosystem.json

Pack のマニフェストです。`pack_id`、`metadata`、`web_mount`、`functions` を定義します。
`web_mount` フィールドにより、`web/` ディレクトリの内容が Tobkiri Launcher 内で配信されます。

### functions/request_display/manifest.json

`viewer.display` capability の詳細な宣言です。以下のフィールドが重要です:

- **`requires`**: `["viewer.display"]` — この Function が必要とする capability
- **`vocab_aliases`**: `["viewer.display"]` — FunctionRegistry でエイリアス解決に使用
- **`grant_config`**: Grant 設定（`allowed_packs`、`max_token_lifetime`）
- **`calling_convention`**: `"block"` — Kernel の DI ハンドラ経由で実行

### functions/request_display/main.py

`calling_convention: "block"` のスタブです。実行時は Kernel の DI ハンドラ
（`handle_display`）が処理するため、このファイルが直接実行されることはありません。
Pack 構造の完全性のために存在します。

### web/index.html, web/style.css

Pack のフロントエンドです。Tobkiri Launcher の sandbox WebView 内にロードされます。
CDN は使用せず、素の HTML/CSS で記述しています。

## viewer:display capability の仕組み

1. Pack が `viewer.display` capability を要求
2. `capability_executor` が `FunctionRegistry.resolve_by_alias("viewer.display")` で解決
3. `grant_config` が設定されている場合、`capability_grant_manager` が Grant を確認
4. `calling_convention: "block"` → Kernel の DI ハンドラ（`handle_display`）が実行
5. トークンと `web_mount_url` が返却される
6. Tobkiri Launcher が `web_mount_url` を sandbox WebView にロード

## Grant の取得

この Pack が `viewer.display` capability を使用するには、Grant が必要です。
Grant は `capability_grant_manager` によって管理されます。

- **`allowed_packs`**: 空配列 `[]` の場合、全ての Pack からの要求を許可
- **`max_token_lifetime`**: トークンの最大有効期限（秒）

Grant の仕組みの詳細は [Pack 開発ガイド セクション 6](../../pack-development.md) を参照してください。

## 関連ドキュメント

- [Pack 開発ガイド](../../pack-development.md) — Pack の構造、ライフサイクル、Capability の詳細
- [多言語 Pack 開発ガイド](../../multilang_pack_guide.md) — Python 以外の言語で Pack を開発する方法
