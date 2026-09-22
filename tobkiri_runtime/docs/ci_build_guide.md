# CI/CD ビルドガイド — tobkiri_launcher デスクトップアプリ

最終更新: 2026-03-29

tobkiri_launcher（Tauri v2 デスクトップアプリ）の CI ビルド・リリースの手順と、過去の障害記録をまとめたドキュメント。

---

## 1. 概要

GitHub Actions の `release.yml` が tag push をトリガーに 4 プラットフォーム同時ビルドを行い、GitHub Releases に draft としてアーティファクトをアップロードする。

| プラットフォーム | ランナー | ターゲット | 成果物 |
|-----------------|---------|-----------|--------|
| macOS ARM | macos-latest | aarch64-apple-darwin | .dmg |
| macOS Intel | macos-15-intel | x86_64-apple-darwin | .dmg |
| Windows | windows-latest | x86_64-pc-windows-msvc | .exe (NSIS) |
| Linux | ubuntu-latest | x86_64-unknown-linux-gnu | .deb, .AppImage |

---

## 2. リリース手順

### 2.1 通常リリース

```bash
# 1. バージョンを更新（tauri.conf.json と Cargo.toml の version）
#    tauri.conf.json: "version": "0.2.0"
#    Cargo.toml:      version = "0.2.0"

# 2. コミット
git add tobkiri_launcher/src-tauri/tauri.conf.json tobkiri_launcher/src-tauri/Cargo.toml
git commit -m "release: v0.2.0"

# 3. tag push（これが CI トリガー）
git tag v0.2.0
git push origin master
git push origin v0.2.0

# 4. GitHub Actions が自動で 4 プラットフォームビルド
#    → GitHub Releases に draft release が作られる

# 5. GitHub の Releases ページで draft を確認 → 公開
```

### 2.2 テストリリース（CI 動作確認用）

```bash
# test tag はインクリメントする（v0.1.0-test.1, .2, .3, ...）
# 既存の test tag を確認
git tag -l "v0.1.0-test*"

# 次の番号で tag push
git tag v0.1.0-test.4
git push origin v0.1.0-test.4

# CI の結果を確認
# https://github.com/harupipipipi/rumiai/actions
```

### 2.3 CI 結果の確認方法

```bash
# ブラウザで確認
# https://github.com/harupipipipi/rumiai/actions

# API で確認（ログイン不要）
curl -s https://api.github.com/repos/harupipipipi/rumiai/actions/runs?per_page=3 \
  | python3 -c "
import json, sys
runs = json.load(sys.stdin)['workflow_runs']
for r in runs:
    print(f\"{r['head_branch']:20s} {r['status']:12s} {r['conclusion'] or '':10s} {r['created_at']}\")
"

# ジョブ単位の確認
curl -s https://api.github.com/repos/harupipipipi/rumiai/actions/runs/<RUN_ID>/jobs \
  | python3 -c "
import json, sys
jobs = json.load(sys.stdin)['jobs']
for j in jobs:
    print(f\"{j['name']:50s} {j['status']:12s} {j['conclusion'] or '':10s}\")
"
```

---

## 3. release.yml の構造

```
.github/workflows/release.yml
```

- **トリガー**: `push.tags: ["v*"]` — `v` で始まる tag push
- **マトリクス**: 4 つの os × target の組み合わせ
- **主要ステップ**:
  1. Checkout
  2. Set up Python / Rust / Node
  3. Build panel frontend and defaultspack frontend
  4. Build `pack-shell` for the target platform
  5. Prepare `tobkiri_launcher/src-tauri/gen/app` from `tobkiri_runtime`
  6. Build (`cargo tauri build --target $target`)
  7. Upload release artifacts (`softprops/action-gh-release`)

`tobkiri_launcher/src-tauri/gen/app` は Git 管理しない。CI では
`.github/scripts/prepare_tauri_resources.py` が runtime tools を stage し、Tauri
の `build.rs` も同じ除外ルールで `gen/app` を再生成する。生成対象には
`app.py`, `core_runtime/`, canonical `ecosystem/defaultspack/`, build 済み panel/defaultspack UI,
`bundled/uv`, `bundled/pack-shell` が入る。`.venv`, `node_modules`,
`user_data`, `__pycache__`, `.rumi_snapshots`, `tests/` は配布物から除外する。

resource staging の preflight は Defaultspack v4 のみを authority とする。
`pack.v4.json`, `contracts.v4.json`, `artifact-index.v4.json`,
`executables.v4.json`, `v4/bundle.lock.json`, `v4/defaults.profile.v4.json` を
必須入力とし、bundle lock・artifact catalog の declared implementation
digest と staged bytes を strict に照合する。欠落、改変、hash drift、unlisted
file、path traversal、symlink は fail closed になる。旧 `ecosystem.json` と
`rumi.pack.v3.json` は staged resource に含めないため、これらを復活させて
package authority として扱ってはならない。

PR 上で配布物を確認したい場合は、手動実行もできる
`.github/workflows/desktop-installers.yml` を使う。Windows NSIS, macOS DMG, Linux
DEB/AppImage を Actions artifact としてアップロードする。代表的な出力先は以下。

- Windows: `tobkiri_launcher/src-tauri/target/x86_64-pc-windows-msvc/release/bundle/nsis/*.exe`
- macOS: `tobkiri_launcher/src-tauri/target/{target}/release/bundle/dmg/*.dmg`
- Linux: `tobkiri_launcher/src-tauri/target/x86_64-unknown-linux-gnu/release/bundle/{deb,appimage}/`

### ランナー選定の注意

GitHub Actions のランナーは定期的に廃止される。廃止されたランナーを指定するとジョブがキューに入ったまま失敗する。

| 廃止されたランナー | 廃止日 | 代替 |
|-------------------|--------|------|
| macos-12 | 2024年後半 | macos-13 → macos-15 |
| macos-13 | 2025-12 | macos-15-intel |

**確認方法**: https://github.com/actions/runner-images を参照。

---

## 4. アイコンファイル管理

### 4.1 必須ファイル

Tauri v2 のビルドには以下のアイコンファイルが必要:

```
tobkiri_launcher/src-tauri/icons/
├── 32x32.png         — 32×32 RGBA PNG
├── 128x128.png       — 128×128 RGBA PNG
├── 128x128@2x.png    — 256×256 RGBA PNG（Retina 用）
├── icon.png          — 512×512 RGBA PNG（アプリアイコン元画像）
├── icon.ico          — Windows 用 ICO（16/32/48/256 サイズ埋め込み）
└── icon.icns         — macOS 用 ICNS（128/256/512 サイズ埋め込み）
```

### 4.2 絶対に守ること

- **PNG は RGBA (color_type=6) でなければならない**。RGB (color_type=2) だと Tauri の `generate_context!()` マクロがコンパイル時にパニックする
- **PNG は正方形 (width == height) でなければならない**。非正方形だと tauri-bundler が AppImage バンドル時にパニックする
- **icon.ico は必須**。存在しないと Windows の `build.rs` でコンパイルエラー
- **tauri.conf.json の bundle.icon にパスを列挙する**。未設定だとデフォルトパスを探しに行き、見つからなければエラー

### 4.3 現在のアイコン

プレースホルダー（単色 R=100, G=100, B=200 の青い四角）。正式アイコンが決まったら差し替える。

### 4.4 アイコン差し替え手順

正式アイコンを用意したら:

```bash
# 方法 1: cargo tauri icon コマンド（Tauri CLI がインストール済みの場合）
# 1024x1024 以上の正方形 RGBA PNG を用意
cargo tauri icon path/to/new_icon.png

# 方法 2: 手動で各サイズを生成
# 画像編集ソフトで 32x32, 128x128, 256x256, 512x512 の RGBA PNG を書き出し
# ICO と ICNS は専用ツールで生成

# 差し替え後は必ず test tag で CI 確認
git add tobkiri_launcher/src-tauri/icons/
git commit -m "chore: update app icons"
git push origin master
git tag v0.x.y-test.1
git push origin v0.x.y-test.1
```

### 4.5 tauri.conf.json の bundle.icon 設定

```json
{
  "bundle": {
    "icon": [
      "icons/32x32.png",
      "icons/128x128.png",
      "icons/128x128@2x.png",
      "icons/icon.icns",
      "icons/icon.ico"
    ]
  }
}
```

icon.png は bundle.icon に含めなくてよい（trayIcon.iconPath で使用）。

---

## 5. アップデート機構

### 5.1 現状: 未実装

2026-03-29 時点で、アプリの自動アップデート機構は **未実装**。

- `tauri-plugin-updater` は Cargo.toml に含まれていない
- `tauri.conf.json` に `plugins.updater` セクションなし
- `capabilities/default.json` に updater パーミッションなし

ユーザーがアップデートするには、GitHub Releases から新しいバイナリを手動でダウンロードして再インストールする必要がある。

### 5.2 将来計画: Phase U

roadmap.md のアップデート計画で実装予定:

- **U-1**: バージョン管理（現在のバージョン取得、最新バージョンの取得）
- **U-2**: アップデートチェック API（Cloudflare Workers or R2）
- **U-3**: Rust ランチャーのセルフアップデート
- **U-4**: Kernel（Python ソースコード）のアップデート
- **U-5**: Pack のアップデート

### 5.3 Tauri v2 の updater プラグイン（参考）

Tauri v2 には公式の updater プラグインがある。導入する場合のステップ:

```
1. cargo add tauri-plugin-updater  (Cargo.toml)
2. tauri.conf.json に plugins.updater を追加
3. capabilities/default.json に "updater:default" を追加
4. アップデートサーバー（JSON エンドポイント）を用意
5. Rust 側で updater::Builder を初期化
```

ただし、Rumi AI のアーキテクチャでは Rust ランチャーだけでなく Python Kernel と Packs のアップデートも必要なため、Tauri 標準の updater だけでは不十分。Phase U で独自のアップデートフローを設計する。

---

## 6. 障害記録

### 6.1 v0.1.0-test.1 — 初回 CI 実行（全滅）

**日時**: 2026-03-28 19:17 UTC
**結果**: 手動キャンセル（4ジョブ中、成功前にキャンセル）
**原因**: 3 つの独立した問題が同時に発生

#### 問題 1: macOS Intel ランナー廃止

- **症状**: `macos-13` ランナーを指定したジョブがキューに入ったまま進まない
- **原因**: GitHub Actions が `macos-13` ランナーを 2025年12月に完全削除済み
- **根拠**: GitHub 公式のランナーイメージ廃止スケジュール

#### 問題 2: Windows の icon.ico 不在

- **症状**: Windows ビルドで `build.rs` がコンパイルエラー
- **原因**: `tauri-build` の `build.rs` が `icons/icon.ico` を必須としている。リポジトリには 83 バイトの 16×16 `icon.png` しかなかった
- **根拠**: Tauri v2 の `build.rs` はリソースとして `.ico` を Windows バイナリに埋め込む

#### 問題 3: Linux の AppImage バンドル失敗

- **症状**: `tauri-bundler` が AppImage バンドル時にパニック
- **原因**: `tauri-bundler` が正方形 PNG (width == height) を icons ディレクトリからフィルタした結果 0 件になった。既存の `icon.png` は 16×16 だったが、bundler が要求する最小サイズを満たさなかった可能性、または bundler が `icon.png` を見つけられなかった
- **備考**: deb/rpm バンドルは成功していた。AppImage のみ失敗

### 6.2 v0.1.0-test.2 — ランナー修正 + アイコン生成（RGB 版）

**日時**: 2026-03-28 20:15 UTC
**結果**: 4ジョブ中 2 失敗、2 成功予想だったが最終的に全滅

| ジョブ | 結果 | 失敗ステップ |
|--------|------|------------|
| macOS ARM (macos-latest) | failure | Build with cargo tauri |
| macOS Intel (macos-15-intel) | failure | Build with cargo tauri |
| Linux (ubuntu-latest) | failure | Build with cargo tauri |
| Windows (windows-latest) | failure | Build with cargo tauri |

**修正内容（v0.1.0-test.2 で適用したもの）**:
- `macos-13` → `macos-15-intel` に置換 → **ランナー問題は解決**（ジョブが起動してビルドまで進んだ）
- Python 標準ライブラリ（struct + zlib）で PNG / ICO / ICNS を生成 → ファイルは正常に生成された
- `tauri.conf.json` に `bundle.icon` を追加

**新たに判明した問題**:

#### 問題 4: PNG が RGB で Tauri が RGBA を要求

- **症状**: 全プラットフォームで同一エラー
  ```
  error: proc macro panicked
   --> src/lib.rs:150:14
    |
  150 |         .run(tauri::generate_context!())
    |              ^^^^^^^^^^^^^^^^^^^^^^^^^^
    |
    = help: message: icon .../icons/32x32.png is not RGBA
  ```
- **原因**: Python で生成した PNG の color_type が `2` (RGB, 3 bytes/pixel) だった。Tauri の `generate_context!()` マクロはコンパイル時に PNG をデコードし、RGBA (color_type=6, 4 bytes/pixel) でないとパニックする
- **教訓**: **Tauri のアイコン PNG は必ず RGBA (color_type=6) で生成すること**。RGB は不可

### 6.3 v0.1.0-test.3 — RGBA 修正版（全成功）

**日時**: 2026-03-28 22:21 UTC
**結果**: 全 4 ジョブ成功

| ジョブ | 結果 | ビルド時間 |
|--------|------|-----------|
| macOS ARM (macos-latest) | success | ~3 min |
| macOS Intel (macos-15-intel) | success | ~5.5 min |
| Linux (ubuntu-latest) | success | ~4 min |
| Windows (windows-latest) | success | ~5.5 min |

**修正内容**:
- PNG 生成の `color_type` を `2` (RGB) → `6` (RGBA) に変更
- ピクセルデータを `bytes([r, g, b])` → `bytes([r, g, b, 255])` に変更
- 検証ステップに IHDR の color_type=6 チェックを追加

**全ステップ成功を確認**:
- Checkout → Install Rust → Install Tauri CLI → **Build with cargo tauri** → **Upload release artifacts** すべて success

---

## 7. トラブルシューティング

### 「icon ... is not RGBA」エラー

PNG が RGB モードになっている。RGBA (アルファチャンネル付き) で再生成すること。

```bash
# 確認方法
python3 -c "
import struct
with open('tobkiri_launcher/src-tauri/icons/32x32.png', 'rb') as f:
    f.read(8)  # signature
    f.read(4)  # IHDR length
    f.read(4)  # 'IHDR'
    data = f.read(13)
    w, h, depth, ctype = struct.unpack('>IIBB', data[:10])
    print(f'{w}x{h} depth={depth} color_type={ctype}')
    # color_type=6 なら RGBA、2 なら RGB（NG）
"
```

### ランナーがキューのまま進まない

ランナーが廃止されている可能性がある。`release.yml` の `runs-on` を確認。

```bash
grep "runs-on\|os:" .github/workflows/release.yml
```

https://github.com/actions/runner-images で現在利用可能なランナーを確認。

### AppImage バンドルでパニック

icons ディレクトリに正方形 (width == height) の PNG が存在しないか、サイズが不足している。`ls -la tobkiri_launcher/src-tauri/icons/` で確認。

### draft release が作られない

`softprops/action-gh-release@v2` は `files` パターンにマッチするファイルがない場合、release を作らない可能性がある。ビルド成果物のパスを確認:

```
tobkiri_launcher/src-tauri/target/<target>/release/bundle/
├── dmg/   (macOS)
├── nsis/  (Windows)
├── deb/   (Linux)
└── appimage/ (Linux)
```

---

## 8. 変更履歴

| 日付 | 内容 |
|------|------|
| 2026-03-29 | 初版作成。v0.1.0-test.1〜3 の障害記録、ビルド手順、アイコン管理、アップデート機構の現状を記載 |
