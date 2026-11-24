# [QA][pack-architecture][Wave 10][soon] PR #<番号> 実環境テスト

このPRを実環境でテストしてください。

起動テストを必ず行ってください。

## Target

- PR: 未作成（push/PR許可後に記入）
- Wave: 10
- Base: `soon`
- Head: `codex/pack-architecture-program-soon`
- Head SHA: PR作成時に記入
- Related issue: #1158

## Required validation

- safe/core、personal desktop、coding、research、scheduler/connector、Operations
  Company の各 profile が明示的な effective pack set だけで起動すること
- critical global contract provider が一意で、無関係な installed-pack scan や
  foreign pack-ID branch がないこと
- defaultspack の旧 HTTP/function route が selected global contract を呼び、
  primary feature store/runtime/UI を直接 import しないこと
- legacy Company CRUD route が `rumi.resource.company.v1` と
  `rumi.action.company.state.v1` のみを通り、旧 `CompanyService`／
  `CompanyStore` を構築しないこと。作成・更新・削除で approval token binding、
  authority receipt、stale revision、replay、restart persistence を確認
- Company の description、metadata、conversation-group identifier が selected
  profile-scoped state owner にだけ保持されること。member/role 由来の legacy
  agent projection を確認し、未移行の Company runtime／dispatch／collection route
  は旧 state fallback を開始せず、明示的な adapter/sunset 状態であることを記録
- legacy Company settings route の merge／明示的 replace、subagent-team write guard、
  approval token binding と authority receipt の arguments binding を確認
- legacy Company agents route の role/member atomic upsert/delete、legacy projection、
  one approval token が複数 owner action に再利用されないことを確認
- legacy Company channels route の selected-state read/write、approval binding、
  Mimo 同期や runtime channel の write-on-read が発生しないことを確認
- legacy Kanban board の export、one-shot import、同一 source hash の no-write
  dedupe、changed source hash の fail-closed、stale revision、rollback を確認
- migration 前の legacy Kanban route は migration-required diagnostic と recovery
  path を返し、空 board を静かに新規作成しないこと
- legacy snapshot reader が read-only で、旧 SQLite の schema migration・DB 作成・
  write を行わないこと
- migration 後の old Kanban route は global resource/action だけを通し、旧 SQLite
  に read/write しないこと。二重 writer がないこと
- old React Kanban view の撤去後、isolated Kanban route の profile scope、pack
  削除時の route 消失、read-only RPC、approved action 境界を確認
- defaultspack bundle に Kanban component/resource/API client が残らず、legacy
  workspace tab が global `/kanban` route へ移動すること
- legacy `tool_task_board` / agent-session ID が旧 state を作成・更新せず、
  stable deprecated code と contract-native recovery を返すこと
- startup/shutdown、restart persistence、upgrade/downgrade、clean-install、
  macOS/Windows/Linux を記録すること

## Evidence

- OS、profile、effective pack set、selected provider、実行コマンド、redacted logs
- migration source hash、rollback decision、旧/new route response、screenshots
- 失敗時は recovery path と再現手順を Issue と対象 PR に記録

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。
