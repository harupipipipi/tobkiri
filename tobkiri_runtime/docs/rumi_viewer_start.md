# tobkiri_launcher Start Guide

`tobkiri_launcher` は Tauri 製の desktop shell です。開発起動では repo 内の `tobkiri_runtime/` を自動検出し、Python kernel を起動して panel UI へ接続します。
control panel frontend の source は `tobkiri_launcher/frontend` が所有し、kernel は build 済み artifact を `tobkiri_runtime/core_runtime/core_pack/core_control_panel/web` から `/panel/` として配信します。

## これを読むタイミング

- viewer を最短で起動したい
- viewer が kernel を見つけられず止まる
- panel は開くが画面遷移が崩れる
- `defaultspack` の frontend / panel まわりの起動経路を追いたい

## 最短の起動手順

repo ルートで次を実行します。

Windows PowerShell:

```powershell
cd tobkiri_launcher\frontend
npm install
npm run tauri -- info
npm run tauri -- dev
```

macOS / Linux:

```bash
cd tobkiri_launcher/frontend
npm install
npm run tauri -- info
npm run tauri -- dev
```

2 回目以降、`tobkiri_launcher/frontend/node_modules` が残っている場合は次だけで起動できます。

```bash
cd tobkiri_launcher/frontend
npm run tauri -- dev
```

Tauri CLI は `tobkiri_launcher/frontend` の dev dependency として入ります。`npm run tauri -- info` や `npm run tauri -- dev` が `tauri` 不在で失敗する場合は、`tobkiri_launcher/frontend` で `npm install` を再実行してください。

開発起動では viewer が次を自動で行います。

1. repo 内の `tobkiri_runtime/` を検出する
2. `~/Library/Application Support/dev.rumiai.app/venv` を用意する
3. `python -m app` で kernel を起動する
4. `http://127.0.0.1:8765/panel/` へ bootstrap する
5. viewer から `Open Defaultspack` を押すと `defaultspack` の独立 UI を開く

## 開発時の承認フロー

- repo checkout を検出しても、それだけでは pack 自動承認は有効になりません
- 開発環境として kernel へ `RUMI_ENVIRONMENT=development` は渡されます
- `RUMI_AUTO_APPROVE_LOCAL=true` を明示して viewer を起動したときだけ、開発用の自動承認が有効になります

例:

```bash
cd tobkiri_launcher/frontend
RUMI_AUTO_APPROVE_LOCAL=true npm run tauri -- dev
```

この opt-in を付けない通常の開発起動では、modified pack は再承認待ちのままです。

## 起動できたときの見え方

- 正常起動すると Tauri window が開きます
- 初回状態では `/health` が `needs_setup: true` を返すことがあり、その場合は setup 画面から始まります
- setup 完了後は panel UI に遷移します
- panel の Home から `Open Defaultspack` を押すと、viewer が `defaultspack` の browser UI を起動します

## defaultspack との関係

- viewer が直接開くのは kernel の control panel (`/panel/`) です
- frontend source は viewer 側にありますが、配信経路は kernel の `/panel/` のままです
- `defaultspack` 自体は kernel から component として読み込まれます
- `defaultspack` の独立 HTTP frontend は `DEFAULTS_HTTP_PORT` 既定値 `8766` ですが、viewer の初期導線とは別です
- 開発起動 (`npm run tauri -- dev`) では repo 同梱の `tobkiri_runtime/ecosystem/defaultspack/` を優先して開きます
- 配布版 / bundle 起動では `rumi_home/user_data/packs/defaultspack/current.json` を見て、移行互換として `app_data_dir/user_data/packs/defaultspack/current.json` も参照します
- そのため setup/更新済みの `Defaultspack v2` が managed pack として切り替わっていれば、配布版 viewer からその実体を開けます

## defaultspack を開発中に起動するときの注意

viewer 経由で検証するときは、まず `tobkiri_launcher` を起動し、viewer の Home から `Open Defaultspack` / `Launch Defaultspack` を押してください。
`pack-shell run defaultspack` を先に直接実行すると、pack-shell が別の kernel を `8765` に起動することがあります。その kernel は viewer が生成した `RUMI_PANEL_BOOTSTRAP_SECRET` を知らないため、あとから viewer を開いたときに bootstrap 401、黒画面、または「Tobkiri Launcher と Tobkiri/defaultspack が複数起動している」状態に見えます。

defaultspack の独立 UI は `8766` を使います。viewer 本体は `8765` の kernel を管理し、defaultspack は viewer から必要になったタイミングで別ウィンドウとして開かれるのが通常の流れです。
fresh checkout から確認するときは、`python -m rumi_ai` や `pack-shell run defaultspack` を先に起動するのではなく、viewer の Home から `Open Defaultspack` / `Launch Defaultspack` を押してください。

`Open Defaultspack` が `desktop_app.execute not granted` または `Pack not allowed for desktop app execution: defaultspack` で失敗するときは、pack 承認とは別に desktop app 起動用 capability grant が不足しています。開発環境でだけ、次のように署名付き grant を確認・修復できます。

```bash
cd tobkiri_runtime
python3 - <<'PY'
from core_runtime.capability_grant_manager import CapabilityGrantManager

mgr = CapabilityGrantManager()
mgr.grant_permission(
    "defaultspack",
    "desktop_app.execute",
    {
        "mode": "development",
        "source": "local_dev_launch",
        "setup_pack_id": "defaultspack",
        "allowed_packs": ["defaultspack"],
        "max_token_lifetime": 3600,
    },
)
check = mgr.check("defaultspack", "desktop_app.execute")
print({"allowed": check.allowed, "reason": check.reason, "config": check.config})
PY
```

この操作は `user_data/permissions/capabilities/defaultspack.json` を `CapabilityGrantManager` 経由で更新し、HMAC 署名も再計算します。手で JSON だけを書き換えると tamper 扱いになるため避けてください。

## よくある詰まり方

### `Kernel directory not found`

viewer が bundle 内の `app/` しか見ていないか、repo checkout を検出できていません。開発起動は repo ルート配下で行ってください。

### `panel bootstrap returned 401 Unauthorized`

bootstrap secret がずれているか、古い kernel がポート `8765` を掴んでいる可能性があります。以下で占有を確認します。

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

viewer で検証する場合、`8765` を掴んでいる古い `python -m rumi_ai`、`python -m app`、または `pack-shell run defaultspack` は終了してから `cd tobkiri_launcher/frontend && npm run tauri -- dev` を実行してください。

```bash
pgrep -fl 'rumi-viewer|python.*-m app|python.*rumi_ai|pack-shell run defaultspack|defaultspack.desktop_app'
```

`8766` に古い defaultspack が残っていると、新しい viewer から開く defaultspack と競合します。viewer から再起動して確認するときは、`8766` も空いている状態にします。

```bash
lsof -nP -iTCP:8766 -sTCP:LISTEN
```

ブラウザから Authority approval を QA するときも、承認 credential を URL、fragment、
`localStorage`、`sessionStorage` に入れてはいけません。`RUMI_AUTHORITY_BROWSER_TEST_TOKEN`
と `browser_approval_token` を使う旧経路はサーバー全体で無効化され、旧値を送ると
`LEGACY_BROWSER_APPROVAL_REVOKED` になります。旧 URL は credential を除いた同一 origin
URL へ `303` で移行し、`Referrer-Policy: no-referrer` と `Cache-Control: no-store` を返します。

通常の local Bearer は画面の閲覧/API利用資格であり、承認者資格ではありません。同一 origin
script が自己申告した window/device 情報だけで承認者へ昇格することを防ぐため、HTTP の
`browser-exchange` と `browser-ui-operator` は fail closed で無効です。ブラウザ画面は
読み取り専用として使い、承認は Viewer の native approval window で行ってください。

`local auth token required` が出る場合は、`8766` を掴んでいる古い defaultspack がないか、
viewer が生成した local auth と `RUMI_PANEL_BOOTSTRAP_SECRET` が同じプロセスに渡っているかを
確認してください。

### `Open Defaultspack` が 403 で失敗する

`defaultspack` の pack approval が済んでいても、desktop window として起動するには `desktop_app.execute` capability grant が別途必要です。`CapabilityGrantManager.check("defaultspack", "desktop_app.execute")` が `allowed: true` で、config の `allowed_packs` に `defaultspack` が含まれていることを確認してください。

### Home などを押すと真っ暗になる

panel frontend は `basename="/panel"` 前提です。リンクや `navigate()` で `/panel/...` を二重に付けると `/panel/panel` に飛んでルート不一致になります。frontend 側のルートは `/`, `/packs`, `/flows`, `/settings` のように basename 相対で持たせてください。

## 確認コマンド

kernel が起動しているかを確認:

```bash
curl http://127.0.0.1:8765/health
```

defaultspack 独立 frontend が起動しているかを確認:

```bash
curl http://127.0.0.1:8766/api/health
```

## 関連ファイル

- `tobkiri_launcher/src-tauri/src/config.rs`
- `tobkiri_launcher/src-tauri/src/kernel_manager.rs`
- `tobkiri_launcher/src-tauri/src/lib.rs`
- `tobkiri_launcher/frontend/src/App.tsx`
- `tobkiri_launcher/frontend/src/lib/routes.ts`
