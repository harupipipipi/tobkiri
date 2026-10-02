# Wave 0 tracking comment drafts

## Parent Epic #1145

Wave 0を最新`origin/soon`から分離したローカルbranch
`codex/pack-architecture-wave0-contracts-soon`で実装しています。

対象は`rumi.pack.v3`、typed global contracts、deterministic resolution、
data-only discovery、legacy registryからの一方向read-only projection、
ownership/migration文書、独立QA指示です。runtime feature移動、Provider追加、
Tobkiri関連変更、remote pushは行っていません。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。

PR作成後、独立QA Issueで起動、互換resolution、discovery非実行、invalid
manifest、migration、rollback、clean shutdownの実測を依頼します。

## Wave Issue #1146

Wave 0のローカル実装では次を追加しました。

- fail-closedな`rumi.pack.v3` schema
- security/lifecycle/data ownership/failure/cardinality metadata
- canonical JSONとcontent identity
- explicit semantic-version compatibility
- deterministic typed contract registryとopaque consumer clients
- stale/ambiguous/missing/incompatible diagnostics
- legacy `InterfaceRegistry`からの一方向read-only projection
- Python/TypeScript/Dart binding generatorとchecked-in bindings
- AI/tool/UI/storage/event/policy contract examples
- ownership/migration/rollback文書とQA Issue本文案

このWaveでは旧registryをauthoritative ownerとして維持し、v3からlegacyへの
write-back、dual-write、runtime機能移動、Provider追加は行っていません。

このWaveの実装担当Codexはテスト、build、起動確認を実行していません。
PR作成後、独立したQA IssueでこのPRを実環境でテストしてください。
起動テストを必ず行ってください。

## Future Draft PR comment

このPRを実環境でテストしてください。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。

QA tracking: #<QA Issue>

QA Issueに実環境結果が投稿されるまで、このPRをReadyまたはmerge可能扱いにしないでください。
