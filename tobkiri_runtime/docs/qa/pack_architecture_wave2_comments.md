# Wave 2 tracking comment drafts

## Parent Epic #1145

Wave 2のローカル実装では、profile revisionごとに一度だけ生成するimmutable
`ResolvedProfile`へpack/resource selectionを統合しました。startupとCapability
Graph bridgeは同じplanを使用し、pack-aware loaderはeffective pack setだけを
読みます。lockfile、stale diagnostics、legacy setup selectionのdry-run/backup/
rollbackも追加しています。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。

## Wave Issue #1148

Wave 2にrequested/available/selected/healthy/authorizedを分離したdeterministic
resolver、complete resource projection、secret-free lockfile generation/read/
validation/refresh、legacy selection one-way migrationを追加しました。selectionは
authority grantにせず、effective permissionはprofile policyとのintersectionです。

Capability Profile/Graph/Node/binding/Flow/modifierおよびDefaultspack extension/
component/catalog/tool/functionの探索をactive effective pack setへscopeしました。

このWaveの実装担当Codexはprofile migration、scanner、テスト、build、起動確認を
実行していません。PR作成後、独立したQA IssueでこのPRを実環境でテストしてください。
起動テストを必ず行ってください。

## Future Draft PR comment

このPRを実環境でテストしてください。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。

QA tracking: #<QA Issue>

QA Issueに実環境結果が投稿されるまで、このPRをReadyまたはmerge可能扱いにしないでください。
