# [QA][pack-architecture][Wave 1][soon] PR #<番号> 実環境テスト

このPRを実環境でテストしてください。

起動テストを必ず行ってください。

## Target

- PR:
- Wave: 1
- Base: `soon`
- Head: `codex/pack-architecture-program-soon`
- Head SHA:
- Related issue: #1147

## 実装内容

- 移動するownership: なし（architecture gateのみ）
- 旧authoritative owner: 変更なし
- 新authoritative owner: 変更なし
- 追加するglobal contract: なし
- 削除する旧経路: baselineから除去されたexact edge
- compatibility／migration: exact edgeのshrink-only baseline

## 必須実環境

- OS: macOS / Windows / Linuxの対応環境
- Profile: 最小profileと複数pack profile
- Bundle: 最小bundleと既存標準bundle
- Surface: Viewer / control panel
- Migration fixture: approved baselineと縮小candidate
- Packあり／なしの両構成: 必須

## 必須確認

- clean startup
- effective pack set
- contract resolution
- route availability
- UI availability
- tool／prompt／service availability
- pack削除時のsurface消失
- migration
- restart persistence
- rollback
- clean shutdown
- `just pack-architecture`
- Python / TypeScript / Dart cross-pack fixture検出
- foreign pack ID / sibling path / direct implementation route検出
- active profile外 discovery / global secret / kernel domain branch検出
- JSON / SARIF diagnosticsのpath・line・source・target・rule・guidance
- wildcard baseline拒否
- baseline追加・metadata変更拒否、exact edge削除のみ許可

## Security／integrity

- Authority境界
- approval token binding
- secret scope
- workspace binding
- stale revision
- replay
- direct cross-pack accessがないこと
- dual-writeがないこと

## 必須証拠

- 実行コマンド
- OS／環境
- selected profile
- effective pack set
- redacted logs
- screenshots
- startup result
- shutdown result
- migration result
- rollback result
- scanner text / JSON / SARIF output

## Reporting

結果をこのIssueと対象PRへコメントしてください。
失敗した場合はPRをマージ可能扱いにしないでください。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。
