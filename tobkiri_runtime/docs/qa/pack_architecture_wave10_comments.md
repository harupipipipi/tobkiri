# Wave 10 QA comment drafts

## 実環境QA依頼

起動テストを必ず行ってください。

Wave 10 の defaultspack facade 化について、対象 profile の effective pack set、
critical contract provider、legacy Kanban snapshot migration、rollback、旧 route
shim、isolated UI を実環境で確認してください。defaultspack の旧 primary owner に
read/write が残らず、migration-required が明示的に失敗し、二重 writer がないことを
証跡つきで報告してください。

Company CRUD aliases についても、selected Company state contract だけを通り、
approval token binding・authority receipt・stale revision・restart persistence が
成立することを確認してください。未移行の Company runtime/dispatch/collection
routes は別 writer を再開していないことを記録してください。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。
