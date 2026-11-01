# [QA][pack-architecture][Wave 3][soon] PR #<番号> 実環境テスト

このPRを実環境でテストしてください。

起動テストを必ず行ってください。

## Target

- PR:
- Wave: 3
- Base: `soon`
- Head: `codex/pack-architecture-program-soon`
- Head SHA:
- Related issue: #1149

## 実装内容

- 移動するownership: frontend route/module discoveryとgeneric rendering
- 旧authoritative owner: static product importsとdefaultspack UI registry
- 新authoritative owner: core frontend catalog + contributing UI pack
- 追加するglobal contract: route/renderer/region/action/data-source/settings/command
- 削除する旧経路: feature UIからのdirect implementation URL
- compatibility／migration: builtin module contribution shim

## 必須実環境

- OS: macOS / Windows / Linuxの対応環境
- Profile: default / UI packなし / dynamic UI packあり
- Bundle: browserとdesktop/webview
- Surface: host / chat / dynamic feature
- Migration fixture: builtin static screen contribution
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
- host bundleにfeature screenのstatic importがないこと
- host rebuildなしでroute/navigation/commandが消えること
- descriptor/module hash改変、未承認module、missing export
- throwing renderer、infinite suspend、oversized UI、navigation attempt
- failure対象regionだけfallbackし、他routeとshellが継続すること
- action/data-source traceにbackend pack ID/implementation URLがないこと
- feature crash中のcomposer draft preservationと復帰
- keyboard/focus/screen reader/reduced motion/narrow viewport
- shutdown後のwebview/port/module cache/worker cleanup

## Security／integrity

- Authority境界
- approval token binding
- secret scope
- workspace binding
- stale revision
- replay
- direct cross-pack accessがないこと
- dual-writeがないこと
- client自己申告trustedを使わないこと
- opaque iframeへcredential/local authを渡さないこと
- RPCがconversation/profile/surface/principal/expiryへbindされること
- equal-priority collisionがfail closedであること

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
- bundle inspection
- module resolution/quarantine/action trace
- draft recovery evidence

## Reporting

結果をこのIssueと対象PRへコメントしてください。
失敗した場合はPRをマージ可能扱いにしないでください。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。
