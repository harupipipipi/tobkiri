このPRを実環境でテストしてください。

起動テストを必ず行ってください。

## Target

- PR: 未作成（push/PR許可後に記入）
- Wave: 7
- Base: `soon`
- Head: `codex/pack-architecture-program-soon`
- Head SHA: PR作成時に記入
- Related issue: #1153

## 実装内容

- 移動するownership: conversation, message, turn, context materialization,
  memory, knowledge
- 旧authoritative owner: defaultspack chat/memory2/knowledge/steer modules
- 新authoritative owner: Wave 7 owner packs（contextは非永続派生）
- 追加するglobal contract: conversation/message/turn/context/memory/knowledge
- 削除する旧経路: conversation JSON、steer JSON、memory SQLite/Markdown/wiki、
  knowledge file store
- compatibility／migration: contract-only facade、source-hash import、
  marker-bound rollback

## 必須実環境

- OS: macOS、可能ならLinux
- Profile: default-profileと新規profile
- Bundle: local source bundle
- Surface: browser/CLI/mobile conversation APIs
- Migration fixture: conversation、分岐message、memo、knowledge、queued steer
- Packあり／なしの両構成: ありは正常、owner欠落はfail-closed

## 必須確認

- clean startup
- effective pack setにWave 7の5 packが一度だけ含まれる
- contract resolutionが各contractで一意
- conversation/message CRUD、履歴一括編集、branch、restart persistence
- turn begin/steer/consume/cancel/handoffとbounded retention
- context revision mismatch、budget、digest determinism
- memory/memo/wiki互換APIが同じownerだけへ書く
- knowledge CRUD/searchがネットワークキーなしで動く
- pack削除時のsurface消失
- migration、restart persistence、rollback、clean shutdown

## Security／integrity

- Authority境界
- approval token binding
- secret scope
- workspace binding
- stale revision
- replay
- direct cross-pack accessがないこと
- dual-writeがないこと
- first-found fallbackがないこと
- legacy SQLite/Markdown/conversation/steer filesが新規作成されないこと

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
- owner storage tree before/after

## Reporting

結果をこのIssueと対象PRへコメントしてください。
失敗した場合はPRをマージ可能扱いにしないでください。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。
