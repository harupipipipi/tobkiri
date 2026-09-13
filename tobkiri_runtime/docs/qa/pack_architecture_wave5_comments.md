# Wave 5 tracking comments

## Parent Epic #1145

Wave 5 AI Runtime Ownershipをローカルprogram branchへ実装しました。AI gateway、model/provider registry、catalog、credential broker、provider protocol adaptersを独立packへ分離し、defaultspackの主要AI compatibility surfaceをselected global contractsへの有限adapterへ変更しました。Provider追加、catalog entry更新、remote push、PR作成、mergeは行っていません。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。

## Wave Issue #1151

Wave 5のローカル実装では、provider-neutral routing、distinct generate/stream contracts、独立request pipeline/stream/usage/modality/tool-intent/eval contracts、capability/cost/health policy、replay-safe failover、digest-pinned declarative catalog、revision-guarded registries、encrypted scoped credential handles、source-hash migration/rollback、saved-reference alias resolution、approved defaultspack compatibility writesを追加しました。remote healthは検証前に`unknown`です。具体的Providerやcatalog entryは追加していません。

このWaveの実装担当Codexはテスト、build、起動確認を実行していません。
PR作成後、独立したQA IssueでこのPRを実環境でテストしてください。
起動テストを必ず行ってください。

QA draft: `docs/qa/pack_architecture_wave5_qa.md`
