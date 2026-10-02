# Tobkiri Docs Route Map

「何をしたいか」から、最短で読む場所に辿るための入口です。

## 目的別ルート

| やりたいこと | まず読む | 次に読む |
|---|---|---|
| 最短で起動確認したい | [tutorials/runtime-quickstart.md](./tutorials/runtime-quickstart.md) | [operations.md](./operations.md) の「起動」 |
| 用語の意味を揃えたい | [terminology.md](./terminology.md) | [prompt_authoring.md](./prompt_authoring.md), [subagents.md](./subagents.md) |
| runtime の仕組みをコードなしで理解したい | [concepts/system-mechanism.md](./concepts/system-mechanism.md) | [architecture.md](./architecture.md) |
| `tobkiri_launcher` の起動と詰まり方を知りたい | [rumi_viewer_start.md](./rumi_viewer_start.md) | [../README.md](../README.md) の「目的別ガイド」 |
| Pack を作りたい | [pack-development-guide.md](./pack-development-guide.md) | [pack-development.md](./pack-development.md) |
| Pack docs の置き方を知りたい | [pack-documentation-contract.md](./pack-documentation-contract.md) | [pack-development.md](./pack-development.md) |
| Base/Shell application を作りたい | [ADR-016](./ADR-016_BASE_SHELL_APPLICATION_MODEL.txt) | [v4 protocol contract](../tobkiri_protocol/README.md) |
| viewer 表示 Pack を作りたい | [examples/viewer_hello_pack/README.md](./examples/viewer_hello_pack/README.md) | [examples/viewer_pack/README.md](./examples/viewer_pack/README.md) |
| Capability Graph / node / profile 仕様を確認したい | [capability_graph.md](./capability_graph.md) | [node_spec.md](./node_spec.md), [profile_spec.md](./profile_spec.md), [capability_graph_pr_plan.md](./capability_graph_pr_plan.md) |
| API キー / secrets / 運用を知りたい | [operations.md](./operations.md) | [quality_pack/philosophy_memo.md](./quality_pack/philosophy_memo.md) |
| v4 authority 実装境界を統合したい | [runtime-authority-v4.md](./runtime-authority-v4.md) | [ADR-014](./ADR-014_BOUNDARY_CAPABILITY_GRANTS.txt), [ADR-015](./ADR-015_RUNTIME_SECURITY_LIFECYCLE.txt) |
| defaultspack の実装側を追いたい | [../ecosystem/defaultspack/README.md](../ecosystem/defaultspack/README.md) | [../ecosystem/defaultspack/docs/getting-started.md](../ecosystem/defaultspack/docs/getting-started.md) |
| Codex OSS から取り込んだ coding-tool 観点を見たい | [codex_oss_reference.md](./codex_oss_reference.md) | ルートの [../../AGENTS.md](../../AGENTS.md), [../../justfile](../../justfile) |
| Pack architecture再設計の正本を確認したい | [TOBKIRI_PACK_ARCHITECTURE_IMPLEMENTATION_PLAN.txt](./TOBKIRI_PACK_ARCHITECTURE_IMPLEMENTATION_PLAN.txt) | [ADR-014](./ADR-014_BOUNDARY_CAPABILITY_GRANTS.txt), [ADR-015](./ADR-015_RUNTIME_SECURITY_LIFECYCLE.txt), [ADR-016](./ADR-016_BASE_SHELL_APPLICATION_MODEL.txt), [design inputs](./PACK_ARCHITECTURE_DESIGN_INPUTS.json) |

## Pack architecture v4 canonical precedence

Pack architecture v4について文書が矛盾する場合は、次の順で優先します。

1. Accepted ADR-014 / ADR-015 / ADR-016
2. `TOBKIRI_PACK_ARCHITECTURE_IMPLEMENTATION_PLAN.txt`
3. versioned Protocol/Contract schemas and canonical vectors
4. migration guides and current implementation notes
5. legacy runtime, namespace, fallback, Pack authoring documents

production v4 cutoverは完了済みです。既存文書は履歴またはoffline migrationの
説明として残りますが、authority、isolation、activation、distribution、timeout
設計の根拠には使用しません。現在の実装状態は
[status/current-status.md](./status/current-status.md) とtracked complete-v4 evidenceを
参照してください。

## まずここを見れば全体像が分かる3本

1. [tutorials/runtime-quickstart.md](./tutorials/runtime-quickstart.md)
2. [concepts/system-mechanism.md](./concepts/system-mechanism.md)
3. [operations.md](./operations.md)

## Legacy / 互換ドキュメント

以下は互換のため残している旧系統です。新規参照は避け、canonical 側を参照してください。

| Legacy | 代わりに読む canonical |
|---|---|
| [pack_development_guide.md](./pack_development_guide.md) | [pack-development.md](./pack-development.md), [pack-development-guide.md](./pack-development-guide.md) |
| [defaultspack_v2.md](./defaultspack_v2.md) | [defaultspack-v2.md](./defaultspack-v2.md) |
| [setuptodo.md](./setuptodo.md) | [roadmap.md](./roadmap.md) |
