# Tutorial: Launcher startup and runtime checks

このチュートリアルでは Launcher、Profile activation、PackVM、Host の疎通を
別々に確認します。Python コマンドだけで Defaults を起動できる手順ではありません。

`python -m tobkiri` は Launcher 注入の Pack v4 activation snapshot がなければ
意図的に fail closed します。`python -m app` は内部の Host composition entrypoint
であり、Launcher の初期設定や PackVM provisioning を代替しません。

## Step 1. Launcher を起動

インストーラーを使う場合は [Launcher start guide](../tobkiri_launcher_start.md) を
参照してください。ソースから開発起動する場合は、先に repo の
[Setup](../../../README.md#setup) を完了します。Node.js 22.22.0+、Cargo Tauri CLI 2.x、
platform native build prerequisites、repo の `.venv`、clean committed source が必要です。

macOS では repo ルートから:

```bash
source .venv/bin/activate
cd tobkiri_launcher/frontend
npm run tauri -- dev --config src-tauri/tauri.macos.dev.conf.json
```

Linux / Windows のコマンドは [README の Start](../../../README.md#start) にあります。
開発セッション中はこのターミナルを動かしたままにしてください。

`npm run dev` だけでは native Launcher は起動しません。別の Host や
`pack-shell` を先に起動せず、Launcher に bootstrap と panel 接続を任せます。

## Step 2. Profile と PackVM を確認

1. **Open Setup** が表示されたら Defaults Profile を確認して
   **Activate Defaults Profile** を一度だけ実行する
2. Host の再起動と検証を待つ。表示された場合は **Verify activation** を使い、
   activation を重複送信しない
3. **Packs** → **PackVM lifecycle** で plan と doctor の状態を確認する
4. verified plan が表示された場合、ダウンロード、digest、空き容量を確認して
   明示的に同意し、provisioning を実行する
5. **Healthy and attested** を確認してから **Home** → **Launch Defaults Profile** を選ぶ

**Not ready** や activation エラーが残る場合は、表示された理由を記録してください。
Profile が active でも、VM assets や device accelerator が利用できなければ Defaults
Chat / Pack 実行は利用可能とは言えません。platform ごとの条件は
[PackVM and platform readiness](../tobkiri_launcher_start.md#packvm-and-platform-readiness)
にあります。ホスト実行への切替や attestation の手動編集で回避しないでください。

## Step 3. 起動中の Host を診断

別ターミナルを repo ルートで開き、同じ `.venv` を有効にして:

```bash
python -m app --health
```

`--health` は起動中の Host の `http://127.0.0.1:8765/health` を probe するだけです。
Host を起動せず、listen port を取りません。Host 未起動の `status: "down"` は
非ゼロの exit code になります。probe 先のポートは `RUMI_PORT` に従います。

response の `runtime_ready`、`needs_setup`、エラーも確認してください。HTTP response
が返ることは、Profile activation、VM boot、Defaults window の成功を証明しません。
通常のブラウザで `/panel/` が表示されても native approval authority は得られません。

## Step 4. 開発セッションを停止

起動用ターミナルで `Ctrl+C` を実行します。開発プロセス停止後に残った debug app を
開くことは、準備済み standalone Developer bundle の起動と同じではありません。

## ソース確認と native 検証を区別する

runtime を起動しない parser 確認は、repo ルートの同じ `.venv` で実行できます:

```bash
python -m app --help
```

これは CLI が import できる確認であり、Host、Profile、PackVM は起動しません。
`--headless` も Launcher の activation snapshot を作成したり、承認を省略したりする
コマンドではありません。古い tutorial の screenshot / 実行ログは現在の native 起動の
証拠として使わないでください。native 検証では source revision / artifact、Launcher
window、activation、device の PackVM 状態をそれぞれ記録します。

## 次に読む

- Launcher の platform 条件と起動トラブル: [../tobkiri_launcher_start.md](../tobkiri_launcher_start.md)
- 仕組みを追う: [../concepts/system-mechanism.md](../concepts/system-mechanism.md)
- 運用/API 詳細: [../operations.md](../operations.md)
