# Tobkiri Launcher Frontend

Tobkiri のコントロールパネル用フロントエンドアプリケーション。
このディレクトリが `/panel/` UI の canonical source です。

`npm run build` は Vite の成果物を `../../tobkiri_runtime/core_runtime/core_pack/core_control_panel/web` にコピーします。viewer と browser はどちらも kernel が配信する同じ `/panel/` artifact を使用します。Tauri の `splash` は kernel 起動前の viewer 専用画面で、panel frontend とは別です。

リリースまたは Tauri の CI ビルドでは `TOBKIRI_PANEL_BUILD_DIR` に隔離された出力先を指定できます。この場合、ビルド成果物は tracked runtime source を変更せず、Tauri の staging が同じ `/panel/` runtime path にコピーします。指定された出力先が存在しない場合、Tauri staging は checked-in bundle への fallback を拒否します。

## 技術スタック

- React 19 + TypeScript
- Vite
- Tailwind CSS v4
- Zustand (状態管理)
- React Flow (フローエディタ)

## 開発

### 前提条件

- Node.js 22.22+（React Router 8 の実行要件）
- npm

### セットアップ

```bash
npm install
```

### 開発サーバー起動

```bash
npm run dev
```

http://localhost:3000/panel/ でアクセスできます。
バックエンド API（http://localhost:8765）へのリクエストは Vite proxy で自動転送されます。

### Home の更新と通知

Home の Profile 一覧は、表示中に30秒間隔で自動更新されます。
画面への復帰やネットワークの再接続時にも取得し、失敗時は5秒から最大60秒まで
間隔を延ばして再試行します。非表示・オフライン時は定期取得を停止します。
Profile の変更中は一覧の取得を止め、変更前の応答が結果を上書きしないようにします。
この再試行は読み取り専用で、Profile の作成・削除・有効化を再実行しません。

通知は画面上部に表示され、約3秒で上へ消えます。同じ取得エラーが続いている間は
通知を繰り返しません。未解決 Profile のエラーは Launch の×印で示し、
隣の「Copy error」ボタンで詳細を取得できます。
Home に実行基盤の統計は表示せず、統計用の概要 API も取得しません。

サイドバーの Graph、Flow などを表示するには、Settings の「Show Devtools」を
オンにします。この表示設定はブラウザ・Launcher ごとに保存されます。

### ビルド

```bash
npm run build
```

### 型チェック

```bash
npm run lint
```

### テスト

```bash
npm test
```

テストは Node.js の組み込みテストランナーと `tsx` で実行します。React の表示確認は SSR または JSDOM を使うため、Vitest 固有の実行環境は必要ありません。

## ディレクトリ構成

```
src/
├── components/    UI コンポーネント
├── hooks/         カスタムフック
├── lib/           ユーティリティ・API クライアント・型定義
├── pages/         ページコンポーネント
├── store.ts       Zustand ストア
└── main.tsx       エントリーポイント
```

## Graph Editor Extensions

`Flows` ページの graph editor は、単純な縦並び step 表示から次の拡張に対応しました。

- `rumi_start` を起点にした graph 編集
- ノードごとの複数ポート
- ポートごとの `contracts`（独自規格タグ）による接続制約
- `rumi_graph` メタデータとして YAML 内へ editor 状態を保持
- `basepack` を flow メタデータとして保持

`rumi_graph` はランタイム互換を壊さないための editor 向けメタデータです。既存ランタイムが読める `steps` も同時に出力しつつ、viewer ではポート/接続情報を復元できます。

### Profile configuration and personal settings

Home’s **Edit Packs** opens the selected execution Profile at
`/panel/profile?profile_id=<id>#profile-packs`. **Rename** only changes its display
name. Add and Duplicate open the new Profile’s Pack selection. The personal name
and avatar form is separate at `/panel/account` (**Your profile**).

Pack choices come from the Host’s verified artifact catalog, independently of
which Profile is running. **Save Pack selection** appends a definition revision;
it does not activate a Profile or grant permission. Review, approval, and
activation follow using the saved definition. Dependencies remain included when
another selected Pack requires them. Unsaved choices survive in-app navigation;
a changed source revision requires discarding the stale draft before another save.

The composition editor requires the matching Host endpoints:
`GET /api/v4/profiles/catalog` and the `composition` payload on
`POST /api/v4/profiles/update`. An older backend cannot save compositions; the
editor reports the unavailable catalog instead of presenting a working selector.
The registry also publishes `active_profile_definition_revision` so newly saved
changes are not mistaken for the configuration that is currently running.

Activation waits for the Host to publish its next runtime capture. Desktop
Launcher renews the panel session through its native bridge when that capture
changes. A browser-only preview cannot mint that credential: if its session
expires, reopen the panel through Launcher. The UI preserves unresolved request
identities and explains reconnection instead of replaying an uncertain write.
