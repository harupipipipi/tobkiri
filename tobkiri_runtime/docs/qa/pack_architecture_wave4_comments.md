# Wave 4 tracking comments

## Parent Epic #1145

Wave 4 Prompt Studio Pilotをローカルprogram branchへ実装しました。Prompt Studioのauthoritative store、authoring/version/diff/lint/compact/test/rollback/migration、process-isolated global contracts、verified isolated UIを`rumi_prompt_studio_pack`へ移し、defaultspackのprimary writer／loader／UI importを削除しました。旧HTTP/functionはactive profileへbindする有限contract shimです。remote push、PR作成、mergeは行っていません。

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。

## Wave Issue #1150

Wave 4のローカル実装では、一意のprompt owner、atomic optimistic-concurrency store、version/rollback、fixed-root migrationとrollback、manifest-only provider resolution、artifact verification、opaque iframe RPC、pack削除時のroute/API projection cleanupを追加しました。defaultspackの旧authoring blocks/domain UIは削除し、互換経路は新ownerへの一方向adapterだけにしています。

このWaveの実装担当Codexはテスト、build、起動確認を実行していません。
PR作成後、独立したQA IssueでこのPRを実環境でテストしてください。
起動テストを必ず行ってください。

QA draft: `docs/qa/pack_architecture_wave4_qa.md`
