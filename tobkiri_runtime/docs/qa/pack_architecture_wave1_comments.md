# Wave 1 tracking comment drafts

## Parent Epic #1145

Wave 1をローカルprogram branch上で実装しました。manifest-backed pack graph、
Python/TypeScript/JavaScript/Dart source edge、foreign pack branch、sibling path、
direct implementation route、unscoped discovery/secret、kernel domain branchを
exact edgeとして報告するrepository-wide gateです。

既存負債baselineはwildcardを許さず、owner/reason/introduced date/removal Wave/
sunset date/exact path/categoryを必須とし、reference baselineとの比較では削除
だけを許可します。runtime ownershipやProvider機能は変更していません。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。

## Wave Issue #1147

Wave 1のローカル実装にarchitecture scanner、strict baseline schema、fixture
tests、Justfile/CI gate、ownership/rollback文書、QA Issue本文案を追加しました。
diagnosticはpath/line/source/target/rule/guidanceをtext/JSON/SARIFで出力します。

このWaveの実装担当CodexはCI、scanner、テスト、build、起動確認を実行していません。
PR作成後、独立したQA IssueでこのPRを実環境でテストしてください。
起動テストを必ず行ってください。

## Future Draft PR comment

このPRを実環境でテストしてください。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。

QA tracking: #<QA Issue>

QA Issueに実環境結果が投稿されるまで、このPRをReadyまたはmerge可能扱いにしないでください。
