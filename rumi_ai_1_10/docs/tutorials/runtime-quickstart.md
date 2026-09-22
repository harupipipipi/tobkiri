# Tutorial: Runtime Quickstart

このチュートリアルは **「今のリポジトリで runtime が動くところまで」** を最短で確認する手順です。

> Tobkiri の公開名への移行中のため、このブランチでは互換 CLI
> `python -m rumi_ai` を使用します。

## 前提

- repo ルートで作業する
- Python が使える

## Step 1. ヘルスチェックを実行

```bash
python -m rumi_ai --health
```

`status: "UP"` なら正常で exit code 0 です。`DEGRADED` と `DOWN` は JSON を出力しますが、
CI / 監視では異常として exit code 1 になります。

## Step 2. runtime を起動

```bash
python -m rumi_ai
```

`[Rumi] startup.success` が出れば起動完了です。

このコマンドは HTTP server を起動したままにします。

## Step 3. API の疎通確認

別ターミナルで:

```bash
curl http://127.0.0.1:8765/health
```

HTTP 200 と JSON が返れば API は利用可能です。

## Step 4. panel ルート確認（任意）

ブラウザで `http://127.0.0.1:8765/panel/` を開き、画面が表示されることを確認します。

## Step 5. 停止

起動したターミナルで `Ctrl+C`。

## 補足: headless 初期化だけを確認する場合

```bash
python -m rumi_ai --headless
```

`--headless` は HTTP server を起動せず、初期化後に終了します。そのため、このモードでは
`/health` と `/panel/` の確認は行いません。

## 検証スクリーンショット

> 実行確認で取得した画像です。環境により表示は多少変わります。

### /health（ブラウザ表示）

![Runtime health screenshot](../assets/tutorials/runtime-health.png)

### /panel（ブラウザ表示）

![Runtime panel screenshot](../assets/tutorials/runtime-panel.png)

## 実行ログ

実行時の生ログは以下に保存しています。

- [../assets/tutorials/runtime-quickstart.log](../assets/tutorials/runtime-quickstart.log)

## 次に読む

- 仕組みを追う: [../concepts/system-mechanism.md](../concepts/system-mechanism.md)
- 運用/API 詳細: [../operations.md](../operations.md)
- viewer 側の起動経路: [../rumi_viewer_start.md](../rumi_viewer_start.md)
