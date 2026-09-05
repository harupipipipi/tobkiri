# [QA][pack-architecture][Wave 6][soon] PR #<番号> 実環境テスト

このPRを実環境でテストしてください。

起動テストを必ず行ってください。

## Target

- PR:
- Wave: 6
- Base: `soon`
- Head: `codex/pack-architecture-program-soon`
- Head SHA:
- Related issue: #1152

## 実装内容

- 旧owner: defaultspack ToolRegistry/ToolExecutor/broker/MCP path
- 新owner: registry、broker、validator、guard、policy、approval bridge、selector、
  result、audit、local/capability/sandbox/remote/MCP executors、MCP client/server、
  authoring、compatibility projection
- pipeline: resolve -> validate -> guard -> policy -> authorize -> select ->
  execute -> normalize -> audit
- migration: canonical source hash、atomic owner write、owner-only backup、
  marker-bound rollback
- compatibility: IDs、schemas、aliases、widgets、results、approval、audit projection

## 必須実環境

- OS: macOSを含むサポート対象
- Profile: default、新規fixture、migrated fixture
- Bundle: Wave 6全pack、各stage/各executor/各adapterを個別に外した構成
- Surface: Viewer Chat、agent profile、legacy tool API/function、MCP client/server
- Tools: read-only、approval-gated write、rejected、sandbox、MCP
- Packあり／なしの両構成

## 必須確認

- clean startup、clean shutdown
- effective pack set、selected contract provider identity/content hash
- legacy definition snapshotとsource hashの再現性
- ID/schema/alias/widget/resultのmigration前後一致
- source drift、alias missing/collision、partial writeの拒否
- exact marker rollbackと再起動後の状態
- schema validationが型変換せず、extra fieldを拒否すること
- guard order、profile permission、cancellation、deadlineのfail closed
- file read/write、shell inspect/execute、Git read/write/publishの分離
- browser observe/control、desktop observe/control、clipboard read/writeの分離
- unknown authorityとmissing permissionの拒否
- one-shot tokenのoperation/args hash/caller/profile/expiry/replay binding
- executorのnon-broker consumer拒否とlegacy authorization receipt再照合
- token missing、changed args、wrong caller/profile、expired、replayedの拒否
- exact execution kind/provider selectionとdeterministic tie break
- local adapter omissionでservice本体が残りtool exposureだけ消えること
- capability trust/grant rejectionが維持されること
- sandbox handlerが`python_docker`以外へdowngradeしないこと
- remote executorがcredential/network policyを所有しないこと
- MCP namespace missing/mismatch、wrong consumer、unknown operationの拒否
- MCP connection approvalとtool approvalが独立して維持されること
- MCP server callもglobal brokerへ戻ること
- result/error/widget/executor provenance normalization
- raw secret field redaction
- audit stage orderingとrejected/cancelled/failed event
- audit unavailable時にexecutionがfail closedすること
- global broker rejectionがlegacy fallbackへ迂回しないこと
- authoringがcode/command/handler/module/entrypointを拒否すること
- authoring publishのapprovalとstale revision拒否
- concrete tool追加時にbroker source変更が不要なこと

## 実行シナリオ

1. agent/chat profileを起動する。
2. read-only toolを1件実行し、guard/policy/execute/auditを確認する。
3. write toolを1件要求し、approvalなしで停止することを確認する。
4. one-shot approval後に同一argsで1回だけ成功させる。
5. 同token replayとargs変更を拒否することを確認する。
6. profile deny toolを要求し、executor未到達を確認する。
7. sandbox toolとMCP toolを各1件実行する。
8. adapter removal matrixを確認する。
9. migration、再起動、rollback、再起動を確認する。
10. clean shutdownする。

## 必須証拠

- 実行コマンド
- OS／環境
- selected profileとeffective permissions
- effective pack set
- selected contract providersとartifact hashes
- migration source/result/rollback result
- redacted lifecycle audit
- approval request/token decision（token値は記録しない）
- screenshots
- startup/shutdown result
- adapter/stage removal matrix
- read/write/rejected/sandbox/MCP tool results

## Reporting

結果をこのIssueと対象PRへコメントしてください。
失敗した場合はPRをマージ可能扱いにしないでください。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。
