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

http://localhost:3000 でアクセスできます。
バックエンド API（http://localhost:8765）へのリクエストは Vite proxy で自動転送されます。

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

## Workflow graph status

The historical Launcher graph editor is retired. The current Flow page is a
read-only projection of exact Pack-declared compositions and can invoke only
fresh, authoritative Contract operations. It does not save, compile, or
simulate `rumi_graph` documents.

Graph-capable clients must use `tobkiri_workflow_pack`'s finite
`graph.compile-preview` operation. That backend operation validates port
contracts and emits Workflow v4 steps without granting execution authority.
Adding a future visual editor must preserve this Contract boundary and must not
restore the removed panel graph endpoints or client-only execution path.
