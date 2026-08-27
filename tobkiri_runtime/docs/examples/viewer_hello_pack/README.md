# Viewer Hello Pack

Rumi AI OS の **viewer:display** capability を使うサンプル Pack です。
Tobkiri Launcher 内に Hello World フロントエンドを表示します。

Pack 開発者がコピーして改造できるテンプレートとしても機能します。

---

## ディレクトリ構成

```
viewer_hello_pack/
├── ecosystem.json   # Pack マニフェスト
├── web/
│   ├── index.html   # フロントエンド（Hello World ページ）
│   └── app.js       # Kernel API 通信サンプル
└── README.md        # このファイル
```

---

## viewer:display capability とは

`viewer:display` は Tobkiri の core capability の一つで、Pack が Tobkiri Launcher（Tauri ベースのデスクトップ UI）にフロントエンドを表示するための権限です。

この capability を持つ Pack は:

1. `web_mount` で指定したディレクトリの静的ファイルが Viewer から配信される
2. Kernel が短期トークンを発行し、Viewer がそのトークンで認証する
3. フロントエンドから Kernel API（`localhost:8765`）を呼び出せる

capability の定義は `core_runtime/core_pack/core_viewer_capability/` にあります。

---

## 使い方

### 1. Pack を配置する

このディレクトリを `ecosystem/` にコピーします:

```bash
cp -r docs/examples/viewer_hello_pack/ ecosystem/viewer_hello_pack/
```

### 2. Kernel を起動する

```bash
python -m rumi_ai
```

Kernel が起動すると `ecosystem/viewer_hello_pack/ecosystem.json` を自動でスキャンします。

### 3. Pack を承認する

ecosystem Pack は初回で承認が必要です（core_pack と異なり自動承認されません）。
Kernel の API または管理画面から Pack を承認してください。

### 4. Grant を取得する

`viewer.display` permission の Grant が必要です。
以下の手順で Grant を設定します:

- Kernel の GrantManager に `viewer_hello_pack` への `viewer.display` Grant を追加
- Grant が設定されると、viewer:display function を通じてフロントエンドが表示可能になります

### 5. Viewer で表示する

Tobkiri Launcher を起動すると、承認・Grant 済みの Pack のフロントエンドが表示されます。
`web/index.html` が Viewer 内にレンダリングされ、Kernel API との通信デモが動作します。

---

## ecosystem.json の解説

```json
{
  "pack_id": "viewer_hello_pack",
  "capabilities": ["viewer.display"],
  "web_mount": "web"
}
```

| フィールド | 説明 |
|-----------|------|
| `capabilities` | 要求する capability のリスト。`viewer.display` を指定すると Viewer 表示が可能になる |
| `web_mount` | 静的ファイルを配信するディレクトリ。Pack ルートからの相対パス |

---

## Kernel API 通信

`web/app.js` に Kernel API への fetch サンプルが含まれています。

```javascript
fetch("http://localhost:8765/api/health", {
  method: "GET",
  headers: { "Accept": "application/json" }
})
  .then(response => response.json())
  .then(data => console.log(data));
```

Kernel API のデフォルトポートは `8765` です。

---

## カスタマイズのヒント

- **UI を変更する**: `web/index.html` の HTML/CSS を編集します。外部 CSS フレームワークを追加することも可能です
- **API 呼び出しを追加する**: `web/app.js` に新しい fetch 呼び出しを追加します
- **Functions を追加する**: `ecosystem.json` の `functions` セクションと `functions/` ディレクトリに Function を追加すると、バックエンド処理も実装できます
- **複数ページ**: `web/` ディレクトリにページを追加し、SPA ルーティングや複数 HTML ファイルで対応できます
- **Pack 名を変更する**: `ecosystem.json` の `pack_id` と `pack_identity` を変更してください

---

## 関連ドキュメント

- [Pack 開発ガイド](../../pack-development.md)
- [多言語 Pack 開発ガイド](../../multilang_pack_guide.md)
- [core_viewer_capability](../../../core_runtime/core_pack/core_viewer_capability/)
