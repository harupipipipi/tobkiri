# Wave 3 tracking comment drafts

## Parent Epic #1145

Wave 3のローカル実装ではprofile-scoped frontend contribution schema/catalog、
declarative generic host、opaque-origin isolated UI + capability RPC、system builtin
限定same-origin module loader、region quarantine/fallbackを追加しました。

catalogはbackendでowner pack hash/build identity/descriptor/module hash/declared
export/profile revision/plan hashを検証し、clientのtrusted自己申告を使いません。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。

## Wave Issue #1149

Wave 3にroute/renderer/region/action/data-source/settings/commandのgeneric
contribution contractを追加しました。既定はdeclarative、executable UIは
opaque iframe、same-originはsystem packだけです。collision/tie、hash不一致、
missing export、render failureを隔離し、pack removalはcatalogからatomicに消えます。

このWaveの実装担当Codexはfrontend test、lint、build、Browser、起動確認を実行して
いません。PR作成後、独立したQA IssueでこのPRを実環境でテストしてください。
起動テストを必ず行ってください。

## Future Draft PR comment

このPRを実環境でテストしてください。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。

QA tracking: #<QA Issue>

QA Issueに実環境結果が投稿されるまで、このPRをReadyまたはmerge可能扱いにしないでください。
