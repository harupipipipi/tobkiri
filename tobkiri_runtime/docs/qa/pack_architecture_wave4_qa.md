# [QA][pack-architecture][Wave 4][soon] PR #<番号> 実環境テスト

このPRを実環境でテストしてください。

起動テストを必ず行ってください。

## Target

- PR:
- Wave: 4
- Base: `soon`
- Head: `codex/pack-architecture-program-soon`
- Head SHA:
- Related issue: #1150

## 実装内容

- 移動するownership: prompt source registry、store、renderer/composition、authoring、versioning、diff、lint、compact、testbench、rollback、trace、Prompt Studio UI
- 旧authoritative owner: `defaultspack`
- 新authoritative owner: `rumi_prompt_studio_pack`
- 追加するglobal contract: `rumi.resource.prompt.studio.v1`、`rumi.action.prompt.author.v1`、`rumi.action.prompt.version.v1`、`rumi.action.prompt.test.v1`、`rumi.action.prompt.migrate.v1`
- 削除する旧経路: defaultspack writer、loader、primary UI import。旧HTTP/functionは有限contract shimのみ
- compatibility／migration: active-profile-bound shim、fixed-root inventory migration、owner marker rollback

## 必須実環境

- OS: macOSを含むサポート対象
- Profile: defaultと新規fixture profile
- Bundle: defaultspack + Prompt Studioあり／なし
- Surface: Viewer、`/prompts`、legacy API
- Migration fixture: profile Markdown/textとshared JSON
- Packあり／なしの両構成

## 必須確認

- clean startup
- Prompt StudioなしでChat起動
- Prompt Studioありでroute/UI/API出現
- Prompt Studio削除でroute/UI/API消失
- effective pack setとselected provider identity
- prompt edit、save、stale revision rejection
- restart、persistence、version、first-write rollback、normal rollback
- profile migration、legacy source preservation、changed-source rejection
- migration rollback、restart persistence、clean shutdown
- provider/tool-free testbench
- asset hash mismatch quarantineとprocess failure isolation

## Security／integrity

- Authority境界
- local UI approval、CSRF、profile/plan/contribution/owner/contract binding
- expiryとreplay rejection
- secret scopeとmetadata redaction
- stale revision
- direct cross-pack accessがないこと
- dual-write／legacy writerがないこと
- iframeへbearer credentialが渡らないこと

## 必須証拠

- 実行コマンド
- OS／環境
- selected profile
- effective pack setとselected contract providers
- redacted logs
- screenshots
- startup result
- shutdown result
- migration result
- rollback result
- pack removal前後のroute/API結果

## Reporting

結果をこのIssueと対象PRへコメントしてください。
失敗した場合はPRをマージ可能扱いにしないでください。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。
