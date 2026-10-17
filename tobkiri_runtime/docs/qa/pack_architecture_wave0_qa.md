# [QA][pack-architecture][Wave 0][soon] PR #<番号> 実環境テスト

このPRを実環境でテストしてください。

起動テストを必ず行ってください。

## Target

- PR:
- Wave: 0
- Base: `soon`
- Head: `codex/pack-architecture-wave0-contracts-soon`
- Head SHA:
- Related issue: #1146

## 実装内容

- 移動するownership: なし（contract foundationのみ）
- 旧authoritative owner: `InterfaceRegistry`
- 新authoritative owner: 旧registryを維持、v3はread-only projection
- 追加するglobal contract: `rumi.*` typed contract foundation
- 削除する旧経路: なし（Wave 10でprojection削除）
- compatibility／migration: legacy-to-v3 one-way read-only projection

## 必須実環境

- OS: macOS / Windows / Linuxの対応環境
- Profile: 最小既存profile
- Bundle: 最小既存bundle
- Surface: 既存の最小control surface
- Migration fixture: legacy registry fixture
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
- manifest validation / semantic-version compatibility
- discovery中にentrypointがimport・実行されないこと
- invalid manifest / duplicate provider / stale resolution diagnostics
- canonical identity fixed vector
- Python / TypeScript / Dart generated binding parity
- one / many / keyed / chain / fanout / optional semantics
- legacy public ID compatibility

## Security／integrity

- Authority境界
- approval token binding
- secret scope
- workspace binding
- stale revision
- replay
- direct cross-pack accessがないこと
- dual-writeがないこと
- self-declared trustがgrantにならないこと

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

## Reporting

結果をこのIssueと対象PRへコメントしてください。
失敗した場合はPRをマージ可能扱いにしないでください。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。
