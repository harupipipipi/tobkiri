# [QA][pack-architecture][Wave 2][soon] PR #<番号> 実環境テスト

このPRを実環境でテストしてください。

起動テストを必ず行ってください。

## Target

- PR:
- Wave: 2
- Base: `soon`
- Head: `codex/pack-architecture-program-soon`
- Head SHA:
- Related issue: #1148

## 実装内容

- 移動するownership: profile pack/resource selection
- 旧authoritative owner: startup/setup/frontend/resource loaderごとのselection
- 新authoritative owner: immutable `ResolvedProfile`
- 追加するglobal contract: resolved runtime plan projection
- 削除する旧経路: runtime loaderのall-installed implicit discovery
- compatibility／migration: setup-pack selection一方向import

## 必須実環境

- OS: macOS / Windows / Linuxの対応環境
- Profile: clean default / user-edited default / CLI-only / multi-pack
- Bundle: 最小bundleと標準bundle
- Surface: Viewer / CLI / Capability Graph
- Migration fixture: startup profile + setup_pack_selection
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
- requested/available/selected/healthy/authorizedの分離
- same inputから同一input_hash/plan_hash
- profile外installed packのroute/UI/tool/prompt/provider/serviceが非表示
- pack removalで全projectionが同時に消えること
- lockfile生成・verified read・refresh
- manifest/resource hash改変、pack削除、version不一致のstale診断
- user-edited default profileをseed更新が上書きしないこと
- migration dry-run、backup、停止、再起動、rollback、再移行
- shutdown後にlock/temp/active markerが残らないこと

## Security／integrity

- Authority境界
- approval token binding
- secret scope
- workspace binding
- stale revision
- replay
- direct cross-pack accessがないこと
- dual-writeがないこと
- installed/selectedをpermission grantとして扱わないこと
- lockfileにsecret/token値がないこと
- effective permissionsがprofile policyとのintersectionであること

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
- migration前後profile/lockfile差分
- pack removal/stale hash診断

## Reporting

結果をこのIssueと対象PRへコメントしてください。
失敗した場合はPRをマージ可能扱いにしないでください。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。
