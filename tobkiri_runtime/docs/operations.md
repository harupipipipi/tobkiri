
# Tobkiri Runtime — Operations Guide

運用者向けのガイドです。設計の全体像は [architecture.md](architecture.md)、Pack 開発は [pack-development.md](pack-development.md) を参照してください。

> 正規 runtime ディレクトリは `tobkiri_runtime/` です。互換性のため、
> CLI は `rumi_ai` のままです。これは内部互換名であり、公開製品名は Tobkiri です。

---

## 目次

1. [セットアップ](#セットアップ)
2. [起動](#起動)
3. [セキュリティモード](#セキュリティモード)
4. [HTTP API 概要](#http-api-概要)
5. [Pack 承認管理](#pack-承認管理)
6. [ネットワーク権限管理](#ネットワーク権限管理)
7. [Capability Handler 承認](#capability-handler-承認)
8. [Capability Grant 管理](#capability-grant-管理)
9. [pip 依存ライブラリ管理](#pip-依存ライブラリ管理)
10. [Secrets 管理](#secrets-管理)
11. [Pack Import / Apply](#pack-import--apply)
12. [共有ストア管理](#共有ストア管理)
13. [Docker / コンテナ管理](#docker--コンテナ管理)
14. [Flow 実行](#flow-実行)
15. [特権管理（Privileges）](#特権管理privileges)
16. [UDS ソケット設定](#uds-ソケット設定)
17. [監査ログの読み方](#監査ログの読み方)
18. [Pending Export](#pending-export)
19. [認証トークン](#認証トークン)
20. [構造化ログ設定](#構造化ログ設定)
21. [非推奨警告レベル制御](#非推奨警告レベル制御)
22. [ヘルスチェック運用](#ヘルスチェック運用)
23. [メトリクス確認](#メトリクス確認)
24. [Pack テンプレート生成 (scaffold)](#pack-テンプレート生成-scaffold)
25. [エラーコードリファレンス](#エラーコードリファレンス)
26. [環境変数リファレンス](#環境変数リファレンス)
27. [トラブルシューティング](#トラブルシューティング)

---

## セットアップ

### 必要条件

- Python 3.10+
- Docker（本番環境で必須）
- Git

### インストール

```bash
git clone https://github.com/harupipipipi/tobkiri.git
cd tobkiri/tobkiri_runtime

# セットアップ（CLI）
python bootstrap.py --cli init

# または手動
pip install -r requirements.txt
```

### セットアップツール

セットアップツールは CLI と Web の 2 つのインターフェースを提供します。

```bash
# CLI モード
python bootstrap.py --cli              # 対話メニュー
python bootstrap.py --cli check        # 環境チェック
python bootstrap.py --cli init         # 初期セットアップ
python bootstrap.py --cli doctor       # 診断
python bootstrap.py --cli recover      # リカバリー
python bootstrap.py --cli run          # アプリ起動

# Web モード
python bootstrap.py --web              # ブラウザ操作（デフォルトポート 8080）
python bootstrap.py --web --port 9000  # ポート指定
```

セットアップツールは以下を自動化します: Python / Git / Docker のチェック、仮想環境（.venv）の作成、依存関係のインストール、user_data ディレクトリの初期化、default pack のインストール（オプション）。

---

## 起動

```bash
# 本番環境（Docker 必須）
python app.py

# 開発環境（Docker 不要、明示的 opt-in と lockfile が必要）
export RUMI_ENVIRONMENT=development
export RUMI_USER_DATA="${RUMI_USER_DATA:-$PWD/user_data}"
mkdir -p "$RUMI_USER_DATA"
touch "$RUMI_USER_DATA/permissive.lock"
python app.py --permissive

# ヘッドレスモード
python app.py --headless

# ヘルスチェック実行
python app.py --health

# Pack バリデーション実行
python app.py --validate
```

`--health` はヘルスチェックを実行し、結果を JSON で stdout に出力して終了します。status が `"UP"` なら exit code 0、それ以外は exit code 1 です。組み込みプローブとして disk（ディスク空き容量）と writable_tmp（`/tmp` 書き込み可能性）が含まれます。CI/CD やコンテナオーケストレーションのヘルスチェックに利用できます。

`--validate` は Pack のバリデーションを実行し、結果を出力して終了します。

---

## セキュリティモード

通常起動は strict です。permissive は開発時だけの例外で、CLI と環境変数のどちらで
要求しても、明示的 opt-in と `permissive.lock` が必要です。

| モード | Docker | 動作 |
|--------|--------|------|
| `strict`（デフォルト） | 必須 | Docker 不可なら実行拒否 |
| `permissive` | 不要 | 警告付きでホスト実行を許可 |

```bash
# 本番
export RUMI_SECURITY_MODE=strict

# 開発（推奨: CLI を使う）
export RUMI_ENVIRONMENT=development
export RUMI_USER_DATA="${RUMI_USER_DATA:-$PWD/user_data}"
mkdir -p "$RUMI_USER_DATA"
touch "$RUMI_USER_DATA/permissive.lock"
python app.py --permissive
```

既存の自動化で `RUMI_SECURITY_MODE=permissive` を使う場合も、同じ環境変数と lockfile が
必要です。production で permissive を有効化しないでください。

### Windows PowerShell

PowerShell では次を実行します。

```powershell
$env:RUMI_ENVIRONMENT = "development"
if (-not $env:RUMI_USER_DATA) { $env:RUMI_USER_DATA = Join-Path $PWD "user_data" }
New-Item -ItemType Directory -Force -Path $env:RUMI_USER_DATA | Out-Null
New-Item -ItemType File -Force -Path (Join-Path $env:RUMI_USER_DATA "permissive.lock") | Out-Null
python app.py --permissive
```

---

## HTTP API 概要

全エンドポイントは `Authorization: Bearer YOUR_TOKEN` が必須です。

### Pack 管理

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/api/packs` | 全 Pack 一覧 |
| GET | `/api/packs/pending` | 承認待ち Pack 一覧 |
| GET | `/api/packs/{pack_id}/status` | Pack 状態取得 |
| POST | `/api/packs/scan` | Pack スキャン |
| POST | `/api/packs/{pack_id}/approve` | Pack 承認 |
| POST | `/api/packs/{pack_id}/reject` | Pack 拒否 |
| POST | `/api/packs/import` | Pack import |
| POST | `/api/packs/apply` | Pack apply |
| DELETE | `/api/packs/{pack_id}` | Pack アンインストール |

### ネットワーク権限

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/api/network/list` | 全 Grant 一覧 |
| POST | `/api/network/grant` | ネットワーク権限を付与 |
| POST | `/api/network/revoke` | ネットワーク権限を取り消し |
| POST | `/api/network/check` | アクセス可否をチェック |

### Capability Handler 候補

| メソッド | パス | 説明 |
|----------|------|------|
| POST | `/api/capability/candidates/scan` | 候補スキャン |
| GET | `/api/capability/requests?status=pending` | 申請一覧 |
| POST | `/api/capability/requests/{key}/approve` | 承認（Trust + copy） |
| POST | `/api/capability/requests/{key}/reject` | 却下 |
| GET | `/api/capability/blocked` | ブロック一覧 |
| POST | `/api/capability/blocked/{key}/unblock` | ブロック解除 |

### Capability Grant

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/api/capability/grants?principal_id=xxx` | Grant 一覧 |
| POST | `/api/capability/grants/grant` | Grant を付与 |
| POST | `/api/capability/grants/revoke` | Grant を取り消し |
| POST | `/api/capability/grants/batch` | Grant 一括付与（最大 50 件） |

### pip 依存ライブラリ

| メソッド | パス | 説明 |
|----------|------|------|
| POST | `/api/pip/candidates/scan` | 候補スキャン |
| GET | `/api/pip/requests?status=pending` | 申請一覧 |
| POST | `/api/pip/requests/{key}/approve` | 承認 + インストール |
| POST | `/api/pip/requests/{key}/reject` | 却下 |
| GET | `/api/pip/blocked` | ブロック一覧 |
| POST | `/api/pip/blocked/{key}/unblock` | ブロック解除 |

### Secrets

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/api/secrets` | キー一覧（値はマスク） |
| POST | `/api/secrets/set` | 秘密値を設定 |
| POST | `/api/secrets/delete` | 秘密値を削除 |

### Flow 実行

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/api/flows` | 登録済み Flow 一覧 |
| POST | `/api/flows/{flow_id}/run` | Flow を実行 |

### Store

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/api/stores` | Store 一覧 |
| POST | `/api/stores/create` | Store を作成 |
| GET | `/api/stores/shared` | 共有ストア一覧 |
| POST | `/api/stores/shared/approve` | 共有ストア承認 |
| POST | `/api/stores/shared/revoke` | 共有ストア取消 |

### Unit

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/api/units?store_id=xxx` | Unit 一覧 |
| POST | `/api/units/publish` | Unit を公開 |
| POST | `/api/units/execute` | Unit を実行 |

### Privileges

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/api/privileges` | 特権一覧 |
| POST | `/api/privileges/{pack_id}/grant/{privilege_id}` | 特権付与 |
| POST | `/api/privileges/{pack_id}/execute/{privilege_id}` | 特権実行 |

### Pack 独自ルート

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/api/routes` | 登録済みルート一覧 |
| POST | `/api/routes/reload` | ルートテーブルを再読み込み |

### Docker / コンテナ

| メソッド | パス | 説明 |
|----------|------|------|
| GET | `/api/docker/status` | Docker 利用可否 |
| GET | `/api/containers` | コンテナ一覧 |
| POST | `/api/containers/{pack_id}/start` | コンテナ起動 |
| POST | `/api/containers/{pack_id}/stop` | コンテナ停止 |
| DELETE | `/api/containers/{pack_id}` | コンテナ削除 |

---

## Pack 承認管理

### 承認待ちの確認

```bash
curl http://localhost:8765/api/packs/pending \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Pack の承認

```bash
curl -X POST http://localhost:8765/api/packs/{pack_id}/approve \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Pack の拒否

```bash
curl -X POST http://localhost:8765/api/packs/{pack_id}/reject \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "セキュリティ上の懸念"}'
```

### 再承認（Modified 状態の Pack）

ファイル変更でハッシュ不一致になると `modified` 状態になり、自動無効化されます。

```bash
curl -X POST http://localhost:8765/api/packs/{pack_id}/approve \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## ネットワーク権限管理

### Grant の付与

```bash
curl -X POST http://localhost:8765/api/network/grant \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "pack_id": "my_pack",
    "allowed_domains": ["api.openai.com", "*.anthropic.com"],
    "allowed_ports": [443]
  }'
```

### Grant の一覧

```bash
curl http://localhost:8765/api/network/list \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### アクセスチェック

```bash
curl -X POST http://localhost:8765/api/network/check \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pack_id": "my_pack", "domain": "api.openai.com", "port": 443}'
```

### Grant の取り消し

```bash
curl -X POST http://localhost:8765/api/network/revoke \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pack_id": "my_pack", "reason": "不要になった"}'
```

---

## Capability Handler 承認

> **注意**: core_pack（store / secrets / flow / communication / docker）が提供する関数は、この候補導入ワークフローを経由せず、kernel 起動時に FunctionRegistry へ自動登録されます。以下の候補導入ワークフロー（scan → approve → Grant）は、ユーザー Pack が同梱するカスタム capability handler に対して適用されるものです。

Capability handler は 2 段階の操作で使用可能になります。

1. **Trust 登録**（handler 承認）: scan で検出された候補を approve し、handler のコード（sha256）を信頼済みとして登録
2. **Grant 付与**（権限付与）: 承認済み handler の permission を Pack に付与

```
候補スキャン (scan)
    ↓
pending（承認待ち）
    ↓
approve → Trust 登録 + コピー + Registry reload
    ↓
Grant 付与（principal × permission）
    ↓
Pack が capability を使用可能
```

候補は scan → pending → approve/reject → blocked の状態遷移を辿ります。

### 候補のスキャン

```bash
curl -X POST http://localhost:8765/api/capability/candidates/scan \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json"
```

### 承認待ち一覧

```bash
curl "http://localhost:8765/api/capability/requests?status=pending" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### scan レスポンス

候補スキャン後のレスポンス例:

```json
{
  "success": true,
  "data": {
    "scanned": 3,
    "new_candidates": 2,
    "candidates": [
      {
        "candidate_key": "my_pack:fs_read_v1:fs_read_handler:a1b2c3d4e5f6...",
        "pack_id": "my_pack",
        "slug": "fs_read_v1",
        "handler_id": "fs_read_handler",
        "permission_id": "fs.read",
        "sha256": "a1b2c3d4e5f6...",
        "status": "pending",
        "description": "ファイルシステム読み取り handler",
        "risk": "ファイルシステムへの読み取りアクセスを提供"
      }
    ]
  }
}
```

`candidate_key` の形式は `{pack_id}:{slug}:{handler_id}:{sha256}` です。sha256 を含めることで handler.py の内容が変わると別の候補として扱われます。

### 候補の承認

`candidate_key` に含まれる `:` は URL エンコードが必要です。

```bash
ENCODED_KEY="my_pack%3Afs_read_v1%3Afs_read_handler%3Aabc123..."

curl -X POST "http://localhost:8765/api/capability/requests/${ENCODED_KEY}/approve" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"notes": "Reviewed and approved"}'
```

approve は Trust（sha256 allowlist）の登録 + `user_data/capabilities/handlers/` へのコピー + Registry reload を行います。実際に使用するには別途 Grant の付与が必要です。

### 候補の却下

```bash
curl -X POST "http://localhost:8765/api/capability/requests/${ENCODED_KEY}/reject" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "不要なファイルシステムアクセス"}'
```

1 回目・2 回目は `rejected`（1 時間クールダウン）、3 回目で `blocked` になります。

### ブロック解除

```bash
curl -X POST "http://localhost:8765/api/capability/blocked/${ENCODED_KEY}/unblock" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "再評価の結果許可"}'
```

---

## Capability Grant 管理

capability handler の approve 後、実際に Pack が capability を使用するには Grant（principal × permission）の付与が必要です。

### Grant の付与

```bash
curl -X POST http://localhost:8765/api/capability/grants/grant \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"principal_id": "my_pack", "permission_id": "fs.read"}'
```

### Grant の一覧

```bash
curl "http://localhost:8765/api/capability/grants?principal_id=my_pack" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Grant の取り消し

```bash
curl -X POST http://localhost:8765/api/capability/grants/revoke \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"principal_id": "my_pack", "permission_id": "fs.read"}'
```

### Grant の一括付与（バッチ）

最大 50 件の Grant を一括で付与します。処理は best-effort（個別の失敗が他の付与を妨げない）です。

```bash
curl -X POST http://localhost:8765/api/capability/grants/batch \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "grants": [
      {"principal_id": "pack_a", "permission_id": "store.get"},
      {"principal_id": "pack_a", "permission_id": "store.set"},
      {"principal_id": "pack_b", "permission_id": "secrets.get", "config": {"allowed_keys": ["API_KEY"]}}
    ]
  }'
```

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `grants` | ✅ | Grant オブジェクトの配列（最大 50 件） |
| `grants[].principal_id` | ✅ | 対象 Pack ID |
| `grants[].permission_id` | ✅ | 権限 ID |
| `grants[].config` | 任意 | Grant 設定（`allowed_keys` 等） |

レスポンス例:

```json
{
  "success": true,
  "data": {
    "total": 3,
    "succeeded": 3,
    "failed": 0,
    "results": [
      {"principal_id": "pack_a", "permission_id": "store.get", "success": true},
      {"principal_id": "pack_a", "permission_id": "store.set", "success": true},
      {"principal_id": "pack_b", "permission_id": "secrets.get", "success": true}
    ]
  }
}
```

### 全体フロー

```
1. capability handler 候補をスキャン
   POST /api/capability/candidates/scan

2. 候補を承認（Trust 登録 + コピー）
   POST /api/capability/requests/{key}/approve

3. Grant を付与（principal × permission）
   POST /api/capability/grants/grant

4. Pack が capability を使用可能に
```

---

## pip 依存ライブラリ管理

Pack の pip 依存を scan → approve → インストールするワークフローです。

### 候補のスキャン

```bash
curl -X POST http://localhost:8765/api/pip/candidates/scan \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json"
```

### 承認待ち一覧

```bash
curl "http://localhost:8765/api/pip/requests?status=pending" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### 承認（インストール実行）

`candidate_key` は URL エンコードが必要です。

```bash
KEY=$(python3 -c "from urllib.parse import quote; print(quote('my_pack:requirements.lock:abc123...', safe=''))")

curl -X POST "http://localhost:8765/api/pip/requests/${KEY}/approve" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"allow_sdist": false}'
```

デフォルトは wheel のみ（`--only-binary=:all:`）。wheel が存在しないパッケージを含む場合は `"allow_sdist": true` を指定してください。

### 却下

```bash
curl -X POST "http://localhost:8765/api/pip/requests/${KEY}/reject" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "不要なパッケージを含んでいる"}'
```

1 回目・2 回目は `rejected`（1 時間クールダウン）、3 回目で `blocked` になります。

### ブロック解除

```bash
curl -X POST "http://localhost:8765/api/pip/blocked/${KEY}/unblock" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "再評価の結果許可"}'
```

### 前提条件

Pack が承認済み（approved 状態）であることが前提です。未承認 Pack の依存導入は strict モードで拒否されます。

---

## Secrets 管理

### キー一覧（値はマスク）

```bash
curl http://localhost:8765/api/secrets \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### 秘密値の設定

```bash
curl -X POST http://localhost:8765/api/secrets/set \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key": "OPENAI_API_KEY", "value": "sk-..."}'
```

### 秘密値の削除

```bash
curl -X POST http://localhost:8765/api/secrets/delete \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key": "OPENAI_API_KEY"}'
```

秘密値は `user_data/secrets/` に 1 key = 1 file で格納されます。API で再表示はできません（set と delete のみ）。ログに秘密値は一切出力されません。

### 暗号化

秘密値は Fernet（AES-128-CBC + HMAC-SHA256）で暗号化されて保存されます。暗号化鍵は以下の優先順で取得されます。

1. 環境変数 `RUMI_SECRETS_KEY`（Base64 エンコードされた Fernet 鍵）
2. `user_data/settings/.secrets_key` ファイル
3. 上記いずれも存在しない場合、鍵を自動生成して `.secrets_key` に保存

### 鍵のバックアップ

暗号化鍵を紛失すると既存の秘密値は復号できなくなります。`user_data/settings/.secrets_key` を安全な場所にバックアップしてください。環境変数 `RUMI_SECRETS_KEY` で鍵を外部管理する場合も同様にバックアップが必要です。

### 平文モード

`RUMI_SECRETS_ALLOW_PLAINTEXT` で暗号化なしの保存を制御できます。

| 値 | 動作 |
|-----|------|
| `auto`（デフォルト） | 暗号化鍵が利用可能なら暗号化、なければ平文で保存 |
| `true` | 常に平文での保存を許可 |
| `false` | 暗号化鍵が必須。鍵がない場合は秘密値の保存を拒否 |

本番環境では `RUMI_SECRETS_ALLOW_PLAINTEXT=false` を推奨します。

---

## Pack Import / Apply

### Import（staging への取り込み）

```bash
curl -X POST http://localhost:8765/api/packs/import \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path": "/path/to/my_pack.zip"}'
```

フォルダ / `.zip` / `.rumipack`（zip 互換）に対応しています。

### Apply（staging から ecosystem へ適用）

```bash
curl -X POST http://localhost:8765/api/packs/apply \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"staging_id": "abc123"}'
```

apply 時にバックアップが自動作成されます。`pack_id` と `pack_identity` が既存 Pack と不一致の場合は拒否されます。

---

## 共有ストア管理

Pack 間で Store を共有するための管理 API です。共有リクエストは手動承認が必要です（SharedStoreManager）。

### 共有ストア一覧

```bash
curl http://localhost:8765/api/stores/shared \
  -H "Authorization: Bearer YOUR_TOKEN"
```

レスポンス例:

```json
{
  "success": true,
  "data": {
    "shared_stores": [
      {
        "store_id": "shared_data",
        "owner_pack": "pack_a",
        "shared_with": ["pack_b", "pack_c"],
        "status": "approved",
        "approved_at": "2026-01-15T10:00:00Z"
      }
    ]
  }
}
```

### 共有ストア承認

```bash
curl -X POST http://localhost:8765/api/stores/shared/approve \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "store_id": "shared_data",
    "owner_pack": "pack_a",
    "target_pack": "pack_b"
  }'
```

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `store_id` | ✅ | 共有対象の Store ID |
| `owner_pack` | ✅ | Store の所有 Pack ID |
| `target_pack` | ✅ | 共有先の Pack ID |

レスポンス例:

```json
{
  "success": true,
  "data": {
    "store_id": "shared_data",
    "owner_pack": "pack_a",
    "target_pack": "pack_b",
    "status": "approved",
    "approved_at": "2026-01-15T10:00:00Z"
  }
}
```

### 共有ストア取消

```bash
curl -X POST http://localhost:8765/api/stores/shared/revoke \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "store_id": "shared_data",
    "owner_pack": "pack_a",
    "target_pack": "pack_b"
  }'
```

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `store_id` | ✅ | 対象の Store ID |
| `owner_pack` | ✅ | Store の所有 Pack ID |
| `target_pack` | ✅ | 共有を取り消す Pack ID |

レスポンス例:

```json
{
  "success": true,
  "data": {
    "store_id": "shared_data",
    "target_pack": "pack_b",
    "status": "revoked"
  }
}
```

---

## Docker / コンテナ管理

### Docker 状態確認

```bash
curl http://localhost:8765/api/docker/status \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### コンテナ一覧

```bash
curl http://localhost:8765/api/containers \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### コンテナ起動 / 停止

```bash
# 起動
curl -X POST http://localhost:8765/api/containers/{pack_id}/start \
  -H "Authorization: Bearer YOUR_TOKEN"

# 停止
curl -X POST http://localhost:8765/api/containers/{pack_id}/stop \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## Flow 実行

### Flow 一覧の取得

```bash
curl http://localhost:8765/api/flows \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Flow の実行

```bash
curl -X POST http://localhost:8765/api/flows/hello/run \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"name": "World"}, "timeout": 300}'
```

`inputs` は Flow の入力データ（dict）、`timeout` は最大実行時間（秒、デフォルト 300、最大 600）です。

同時実行数は `RUMI_MAX_CONCURRENT_FLOWS` 環境変数で制限されます（デフォルト 10）。上限に達した場合はステータスコード `429` が返却されます。

### 成功レスポンス

```json
{
  "success": true,
  "flow_id": "hello",
  "result": {
    "greeting": {"message": "Hello, World!"}
  },
  "execution_time": 1.234
}
```

`result` には Flow の outputs が格納されます。ただし `_` プレフィックスで始まるキー（`_kernel_step_status` 等の内部キー）は自動的に除外されます。

### エラーレスポンス

```json
{
  "success": false,
  "error": "Flow not found: nonexistent_flow",
  "flow_id": "nonexistent_flow",
  "status_code": 404
}
```

| status_code | 説明 |
|-------------|------|
| `404` | 指定された `flow_id` が存在しない |
| `408` | Flow 実行がタイムアウトした |
| `429` | 同時実行数上限（`RUMI_MAX_CONCURRENT_FLOWS`）に到達 |
| `500` | Flow 実行中に予期しないエラーが発生 |
| `503` | システムが一時的に利用不可（起動中等） |

### レスポンスサイズ制限

Flow の実行結果は `RUMI_MAX_RESPONSE_BYTES`（デフォルト 4MB）を超える場合、切り詰められます。切り詰めが発生した場合、レスポンスに `"truncated": true` が付与されます。

---

## 特権管理（Privileges）

Pack に対して特権的操作（例: `pack.update`、`system.restart` 等）を許可・実行するための API です。Capability Grant とは独立した仕組みで、ホスト側の危険な操作を明示的に許可するために使用します。

### 特権一覧

```bash
curl http://localhost:8765/api/privileges \
  -H "Authorization: Bearer YOUR_TOKEN"
```

レスポンス例:

```json
{
  "success": true,
  "data": {
    "privileges": [
      {
        "privilege_id": "pack.update",
        "description": "Pack の更新適用を許可",
        "granted_packs": ["updater_pack"]
      },
      {
        "privilege_id": "system.diagnostics",
        "description": "システム診断情報の取得を許可",
        "granted_packs": []
      }
    ]
  }
}
```

### 特権付与

```bash
curl -X POST http://localhost:8765/api/privileges/{pack_id}/grant/{privilege_id} \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json"
```

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `pack_id`（パスパラメータ） | ✅ | 対象 Pack ID |
| `privilege_id`（パスパラメータ） | ✅ | 付与する特権 ID |

レスポンス例:

```json
{
  "success": true,
  "data": {
    "pack_id": "updater_pack",
    "privilege_id": "pack.update",
    "granted_at": "2026-02-15T10:00:00Z"
  }
}
```

### 特権実行

```bash
curl -X POST http://localhost:8765/api/privileges/{pack_id}/execute/{privilege_id} \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"args": {"target_pack": "my_pack", "staging_id": "abc123"}}'
```

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `pack_id`（パスパラメータ） | ✅ | 実行元 Pack ID |
| `privilege_id`（パスパラメータ） | ✅ | 実行する特権 ID |
| `args`（ボディ） | 任意 | 特権操作に渡す引数 |

レスポンス例:

```json
{
  "success": true,
  "data": {
    "pack_id": "updater_pack",
    "privilege_id": "pack.update",
    "result": {"status": "applied", "target_pack": "my_pack"},
    "executed_at": "2026-02-15T10:05:00Z"
  }
}
```

特権が付与されていない Pack からの実行リクエストは `403 Forbidden` で拒否されます。

---

## UDS ソケット設定

strict モードで Pack 実行コンテナから UDS ソケットにアクセスするための設定です。

### 環境変数

| 環境変数 | 説明 | デフォルト |
|----------|------|-----------|
| `RUMI_EGRESS_SOCKET_GID` | Egress ソケットの GID | なし |
| `RUMI_CAPABILITY_SOCKET_GID` | Capability ソケットの GID | なし |
| `RUMI_EGRESS_SOCKET_MODE` | Egress ソケットのパーミッション | `0660` |
| `RUMI_CAPABILITY_SOCKET_MODE` | Capability ソケットのパーミッション | `0660` |
| `RUMI_EGRESS_SOCK_DIR` | Egress ソケットのベースディレクトリ | `/run/rumi/egress/packs` |
| `RUMI_CAPABILITY_SOCK_DIR` | Capability ソケットのベースディレクトリ | `/run/rumi/capability/principals` |

### 設定手順

1. 専用 GID を決定（例: 1099）
2. 環境変数を設定:
   ```bash
   export RUMI_EGRESS_SOCKET_GID=1099
   export RUMI_CAPABILITY_SOCKET_GID=1099
   ```
3. ソケット作成時に指定 GID の group が自動設定されます
4. `docker run` 時に `--group-add=1099` が自動付与されます

GID が未設定の場合、コンテナ（nobody:65534）からソケットにアクセスできません。

---

## 監査ログの読み方

監査ログは `user_data/audit/` に `{category}_{YYYY-MM-DD}.jsonl` の形式で保存されます。

### 基本的な読み方

```bash
# 今日のネットワークログ
cat user_data/audit/network_$(date +%Y-%m-%d).jsonl | jq .

# 拒否されたリクエスト
cat user_data/audit/security_$(date +%Y-%m-%d).jsonl | jq 'select(.success == false)'

# 権限操作のログ
cat user_data/audit/permission_$(date +%Y-%m-%d).jsonl | jq .

# lib 実行ログ
cat user_data/audit/system_$(date +%Y-%m-%d).jsonl | jq 'select(.action | contains("lib"))'

# capability grant 操作
cat user_data/audit/permission_$(date +%Y-%m-%d).jsonl | jq 'select(.details.permission_type == "capability_grant")'

# principal_id 上書き警告
cat user_data/audit/security_$(date +%Y-%m-%d).jsonl | jq 'select(.action == "principal_id_overridden")'

# 共有辞書の操作履歴
cat user_data/settings/shared_dict/journal.jsonl | jq .

# 循環検出された共有辞書操作
cat user_data/settings/shared_dict/journal.jsonl | jq 'select(.result == "cycle_detected")'
```

### カテゴリ一覧

| カテゴリ | 内容 |
|----------|------|
| `flow_execution` | Flow 実行 |
| `modifier_application` | Modifier 適用 |
| `python_file_call` | ブロック実行 |
| `approval` | Pack 承認操作 |
| `permission` | 権限操作 |
| `network` | ネットワーク通信 |
| `security` | セキュリティイベント |
| `system` | システムイベント |

---

## Pending Export

起動時に `user_data/pending/summary.json` が自動生成されます。外部ツールはこのファイルを読むだけで承認待ち状況を把握できます。

```bash
cat user_data/pending/summary.json | jq .
```

---

## 認証トークン

全ての HTTP API エンドポイントは `Authorization: Bearer YOUR_TOKEN` ヘッダーによる認証が必須です。トークンは HMAC 鍵から導出されます。

### トークンの確認

起動時にトークンがコンソールに表示されます。また、HMAC 鍵ファイル（`user_data/settings/.hmac_key`）から導出されるため、同じ鍵ファイルが存在する限りトークンは不変です。

鍵ファイルが存在しない場合は初回起動時に自動生成されます。

### トークンのローテーション

HMAC 鍵をローテーション（再生成）することでトークンが変更されます。

```bash
# HMAC 鍵ローテーションを有効にして起動
export RUMI_HMAC_ROTATE=true
python app.py
```

`RUMI_HMAC_ROTATE=true` を設定すると、次回起動時に既存の HMAC 鍵が新しい鍵で置き換えられます。ローテーション後は以前のトークンは無効になるため、全ての API クライアントの設定を更新してください。

ローテーションは一度だけ実行されます。ローテーション完了後は `RUMI_HMAC_ROTATE` を `false` に戻すか、環境変数を削除してください。

---

## 構造化ログ設定

### 環境変数

| 環境変数 | 説明 | デフォルト |
|----------|------|-----------|
| `RUMI_LOG_LEVEL` | ログレベル。DEBUG / INFO / WARNING / ERROR / CRITICAL | `INFO` |
| `RUMI_LOG_FORMAT` | 出力形式。json / text | `json` |

### 設定方法

```bash
export RUMI_LOG_LEVEL=DEBUG
export RUMI_LOG_FORMAT=text
python app.py --headless
```

app.py 起動時に `configure_logging()` が自動的に呼ばれ、`rumi.*` 名前空間のロガーに適用されます。

### JSON 形式の出力例

```json
{"timestamp": "2026-02-24T12:00:00.000000Z", "level": "INFO", "module": "rumi.kernel.core", "message": "Flow loaded", "correlation_id": "req-123"}
```

### テキスト形式の出力例

```
2026-02-24T12:00:00.000000Z [INFO] rumi.kernel.core - Flow loaded (correlation_id=req-123)
```

---

## 非推奨警告レベル制御

### 環境変数

| 環境変数 | 説明 | デフォルト |
|----------|------|-----------|
| `RUMI_DEPRECATION_LEVEL` | 非推奨 API 呼び出し時の動作 | `warn` |

| 値 | 動作 |
|-----|------|
| `warn` | `DeprecationWarning` を `warnings.warn` で発行 |
| `error` | `DeprecationWarning` 例外を送出 |
| `silent` | 何もしない |
| `log` | `logging` で WARNING レベル出力 |

### 設定例

```bash
export RUMI_DEPRECATION_LEVEL=error
python app.py --headless
```

---

## ヘルスチェック運用

### CLI でのチェック

```bash
python app.py --health
```

status が `"UP"` なら exit code 0、それ以外は exit code 1 を返します。

### プログラムからの利用

```python
from core_runtime.health import get_health_checker, probe_disk_space
checker = get_health_checker()
checker.register_probe("disk", lambda: probe_disk_space("/"))
result = checker.aggregate_health()
# result["status"]: "UP" / "DOWN" / "DEGRADED" / "UNKNOWN"
```

### カスタムプローブの追加

```python
from core_runtime.health import HealthStatus
def my_probe() -> HealthStatus:
    # カスタムチェックロジック
    return HealthStatus.UP
checker.register_probe("my_service", my_probe)
```

---

## メトリクス確認

### スナップショットの取得

```python
from core_runtime.metrics import get_metrics_collector
collector = get_metrics_collector()
snapshot = collector.snapshot()
# snapshot["counters"], snapshot["gauges"], snapshot["histograms"]
```

### 自動収集メトリクス

Wave 15 で以下のメトリクスが自動的に収集されます。

| メトリクス名 | 種別 | 説明 | labels |
|-------------|------|------|--------|
| `flow.step.success` | counter | ステップ実行成功カウント | handler |
| `flow.step.error` | counter | ステップ実行失敗カウント | handler |
| `flow.execution.complete` | counter | Flow 実行完了カウント | flow_id |
| `docker.available` | gauge | Docker 利用可否 | — |
| `container.start.success` | counter | コンテナ起動成功カウント | — |
| `container.start.failed` | counter | コンテナ起動失敗カウント | — |
| `flows.registered` | gauge | 登録済み Flow 数 | — |
| `python_file_call.duration_ms` | histogram | Python ファイル実行時間（ミリ秒） | — |

---

## Pack テンプレート生成 (scaffold)

新規 Pack のひな形を生成するコマンドラインツールです。

### 使い方

```bash
python -m core_runtime.pack_scaffold <pack_id> [--template TEMPLATE] [--output-dir DIR]
```

### テンプレート一覧

| テンプレート | 説明 |
|-------------|------|
| `minimal`（デフォルト） | 最小構成（ecosystem.json + run.py） |
| `capability` | Capability Handler 付き |
| `flow` | Flow 定義付き |
| `full` | 全部入り |

### 実行例

```bash
python -m core_runtime.pack_scaffold my-pack --template full --output-dir ecosystem/
```

---

## エラーコードリファレンス

エラーコードは `RUMI-{カテゴリ}-{3桁番号}` の形式で体系化されています。各エラーには suggestion（解決策提案）が付属します。

### カテゴリ一覧

| カテゴリ | 説明 | 例 |
|---------|------|-----|
| `AUTH` | 認証・認可 | `RUMI-AUTH-001`（トークン無効） |
| `NET` | ネットワーク | `RUMI-NET-001`（接続失敗） |
| `FLOW` | フロー実行 | `RUMI-FLOW-001`（Flow 未発見） |
| `PACK` | Pack 管理 | `RUMI-PACK-001`（pack_id 無効） |
| `CAP` | Capability | `RUMI-CAP-001`（Capability 未発見） |
| `VAL` | バリデーション | `RUMI-VAL-001`（空値） |
| `SYS` | システム全般 | `RUMI-SYS-001`（内部エラー） |

---

## 環境変数リファレンス

Tobkiri runtime の動作を制御する環境変数の一覧です。

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `RUMI_SECURITY_MODE` | `strict` | セキュリティモード。`strict`（Docker 必須）または `permissive`（Docker 不要、開発用） |
| `RUMI_LOG_LEVEL` | `INFO` | ログレベル。`DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `RUMI_LOG_FORMAT` | `json` | ログ出力形式。`json`（構造化 JSON）または `text`（人間向けテキスト） |
| `RUMI_DEPRECATION_LEVEL` | `warn` | 非推奨 API 呼び出し時の動作。`warn` / `error` / `silent` / `log` |
| `RUMI_SECRETS_KEY` | なし | Secrets の Fernet 暗号化に使用する鍵（Base64 エンコード）。設定されていない場合は `.secrets_key` ファイルまたは自動生成にフォールバック |
| `RUMI_SECRETS_ALLOW_PLAINTEXT` | `auto` | 平文シークレットの許可。`auto`（暗号化鍵がなければ平文で保存）、`true`（常に平文を許可）、`false`（暗号化鍵が必須、鍵がなければ保存拒否） |
| `RUMI_MAX_RESPONSE_BYTES` | `4194304`（4MB） | Flow 実行結果および Egress Proxy レスポンスの最大サイズ（バイト） |
| `RUMI_MAX_CONCURRENT_FLOWS` | `10` | 同時 Flow 実行数の上限 |
| `RUMI_MAX_REQUEST_BODY_BYTES` | `1048576`（1MB） | HTTP API が受け付けるリクエストボディの最大サイズ（バイト） |
| `RUMI_API_BIND_ADDRESS` | `127.0.0.1` | API サーバーのバインドアドレス。外部公開する場合は `0.0.0.0` に変更（非推奨） |
| `RUMI_CORS_ORIGINS` | なし | CORS 許可オリジンのカンマ区切りリスト（例: `http://localhost:3000,http://localhost:8080`） |
| `RUMI_HMAC_ROTATE` | `false` | `true` に設定すると次回起動時に HMAC 鍵をローテーション |
| `RUMI_DIAGNOSTICS_VERBOSE` | `false` | `true` に設定すると診断ログに詳細情報を含める |
| `RUMI_EGRESS_SOCKET_GID` | なし | Egress UDS ソケットの GID。strict モードでコンテナからソケットにアクセスするために必要 |
| `RUMI_CAPABILITY_SOCKET_GID` | なし | Capability UDS ソケットの GID。strict モードでコンテナからソケットにアクセスするために必要 |
| `RUMI_EGRESS_SOCKET_MODE` | `0660` | Egress UDS ソケットのパーミッション |
| `RUMI_CAPABILITY_SOCKET_MODE` | `0660` | Capability UDS ソケットのパーミッション |
| `RUMI_EGRESS_SOCK_DIR` | `/run/rumi/egress/packs` | Egress UDS ソケットのベースディレクトリ |
| `RUMI_CAPABILITY_SOCK_DIR` | `/run/rumi/capability/principals` | Capability UDS ソケットのベースディレクトリ |
| `RUMI_SECRET_GET_RATE_LIMIT` | `60` | `secrets.get` の rate limit（回/分/Pack、sliding window） |
| `RUMI_LOCAL_PACK_MODE` | `off` | local_pack 互換モード。`off`（無効）または `require_approval`（承認必須で有効、非推奨） |

---

## トラブルシューティング

### Docker が利用できない

```
Error: Docker is required but not available
```

開発時だけ、セキュリティモード節の手順で明示的 opt-in と `permissive.lock` を用意してから
`python app.py --permissive` を実行してください。環境変数だけで sandbox を無効化することはできません。

### Pack が承認されない

```bash
# 承認待ちを確認
curl http://localhost:8765/api/packs/pending \
  -H "Authorization: Bearer YOUR_TOKEN"

# 承認
curl -X POST http://localhost:8765/api/packs/{pack_id}/approve \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Pack が無効化された（Modified）

ファイル変更でハッシュ不一致になると自動無効化されます。再承認してください。

```bash
curl -X POST http://localhost:8765/api/packs/{pack_id}/approve \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### ネットワークアクセスが拒否される

```bash
# Grant 状態を確認
curl http://localhost:8765/api/network/list \
  -H "Authorization: Bearer YOUR_TOKEN"

# 権限を付与
curl -X POST http://localhost:8765/api/network/grant \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pack_id": "my_pack", "allowed_domains": ["api.example.com"], "allowed_ports": [443]}'
```

### Capability が使えない

approve（Trust + copy）だけでは使えません。Grant の付与が必要です。

```bash
curl -X POST http://localhost:8765/api/capability/grants/grant \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"principal_id": "my_pack", "permission_id": "fs.read"}'
```

### Capability Handler の approve が SHA-256 mismatch で失敗する

scan 後に handler.py の内容が変更されています。再度 scan を実行して新しい candidate_key で pending を作り直し、改めて approve してください。

### pip 依存のインストールが拒否される

1. Pack が承認済みか確認してください（strict モードでは必須）
2. `requirements.lock` の構文が正しいか確認してください（`NAME==VERSION` のみ許可）
3. `index_url` が https で外部ホストか確認してください

### UDS ソケットにアクセスできない

1. `RUMI_EGRESS_SOCKET_GID` / `RUMI_CAPABILITY_SOCKET_GID` が設定されているか確認
2. ソケットファイルのパーミッションを確認: `ls -la /run/rumi/egress/packs/`
3. 最終手段: `RUMI_EGRESS_SOCKET_MODE=0666`（非推奨）

### Pack 更新時に identity エラー

```
Error: pack_identity mismatch
```

既存 Pack と異なる `pack_identity` を持つ Pack で上書きしようとしています。意図的な置換の場合は、先に既存 Pack を削除してから再度 apply してください。

### lib が実行されない

```bash
# 監査ログで確認
cat user_data/audit/system_$(date +%Y-%m-%d).jsonl | jq 'select(.action | contains("lib"))'

# 記録を確認（Kernel ハンドラ kernel:lib.list_records）
# 記録をクリアして再実行を強制（Kernel ハンドラ kernel:lib.clear_record）
```

### Modifier が適用されない

1. `target_flow_id` が正しいか確認
2. `phase` が対象 Flow に存在するか確認
3. `requires` の条件が満たされているか確認
4. 監査ログで確認:
   ```bash
   cat user_data/audit/modifier_application_$(date +%Y-%m-%d).jsonl | jq .
   ```

### 旧ディレクトリの警告

```
WARNING: Using legacy flow path. This is DEPRECATED and will be removed.
```

`flow/` や `ecosystem/flows/` から `flows/`、`user_data/shared/flows/`、または Pack 内 `flows/` へ移行してください。

