# [QA][pack-architecture][Wave 9][soon] PR #<番号> 実環境テスト

このPRを実環境でテストしてください。

起動テストを必ず行ってください。

## Target

- PR: 未作成（push/PR許可後に記入）
- Wave: 9
- Base: `soon`
- Head: `codex/pack-architecture-program-soon`
- Head SHA: PR作成時に記入
- Related issue: #1157

## 実装内容

- 移動するownership: schedule/job state、connector registry/inbound/outbound/
  transport/OAuth、Company state/coordinator、Company UI、agent and product
  adapter projections
- 旧authoritative owner: defaultspack scheduler/connector/Company feature paths
  と Operations Company の直接経路
- 新authoritative owner: Wave 9 scheduler、connector、Company、agent owner packs
- 追加するglobal contract: schedule/job、connector gateway/OAuth、Company
  state/coordinator/runtime/work、Kanban state/conversation import、profile-scoped
  UI projections
- 削除する旧経路: canonical vendor-to-chat/Company imports と UI からの
  secret/credential handle exposure
- compatibility／migration: deterministic inbound IDs、receipt-bound delivery、
  PKCE one-shot state、Operations Company snapshot-hash import、profile pack
  removal projection

## 必須実環境

- OS: macOS、Windows、Linux（少なくとも一台ずつ）
- Profile: default-profile、新規 profile、scheduler/connector/Company/product
  pack を個別に除いた profile
- Bundle: local source bundle
- Surface: browser UI、CLI、tool invocation、connector callback endpoint、Viewer
- Migration fixture: schedules、connector registry/credential handles、signed
  inbound payload、legacy Operations Company state、Company tasks/channels/routes
- Packあり／なしの両構成: ありは正常、provider/owner 欠落は fail-closed

## 必須確認

- clean startup、effective pack set、global contract provider が一意
- schedule interval/cron/due dispatch、lease、retry、cancel、restart persistence
- scheduler route と tool contribution、scheduler pack 削除時の surface 消失
- connector inbound signature/replay/deduplication、outbound retry/cancel/audit
- LINE、Slack、Discord、email、P2P、generic webhook、HTTP API、mobile/QR pairing
  それぞれの pack あり／なしと adapter isolation
- Slack PKCE begin/callback/token exchange の state one-shot、credential scope、
  OAuth callback replay 拒否、UI に secret/handle が出ないこと
- connector-to-Turn と connector-to-Company の deterministic mapping、Company
  route ambiguity が unassigned になること
- Company role/member/mention/inbound/task lifecycle、agent dispatch/handoff/
  cancellation、supervisor job adapter、restart persistence
- Operations Company legacy snapshot import の source hash dedupe、changed
  source 拒否、schedule reference のみ保持、rollback
- Companies/Connections/Scheduler UI の profile scope、pack 削除時の route 消失、
  mutation が approved action/tool に限定されること
- artifact/project/Kanban/office-authoring/research/data-analysis product pack の
  workflow/UI content が host core を編集せず profile pack set で出入りすること
- Kanban legacy snapshot import の source hash dedupe、changed source 拒否、
  stale revision、event dedupe、conversation task import の idempotency
- Kanban isolated UI の profile scope、read-only RPC、pack 削除時の route 消失。
  Wave 10 cutover 前は legacy Kanban UI と同じ profile で同時に有効化しないこと
- migration、rollback、clean shutdown

## Security／integrity

- Authority境界、approval token binding、receipt caller/argument/profile/session/
  replay binding
- connector credential scope、OAuth code/secret non-persistence、redacted logs
- direct cross-pack private access、first-found fallback、dual-write がないこと
- stale revision、scheduler lease/retry/cancel、inbound replay、outbound dedupe
- connector/vendor pack が chat/Company/scheduler implementation を import
  しないこと

## 必須証拠

- 実行コマンド、OS／環境、selected profile、effective pack set、selected provider
- redacted logs、screenshots、startup/shutdown、migration/rollback、packあり／なし
  の route/tool/UI 差分
- scheduler timing/retry/lease、connector signed inbound/outbound、Company task
  dispatch の記録（秘密・credential handle・OAuth code は除外）

## Reporting

結果をこのIssueと対象PRへコメントしてください。
失敗した場合はPRをマージ可能扱いにしないでください。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。
