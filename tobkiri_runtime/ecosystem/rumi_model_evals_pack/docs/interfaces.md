# Interfaces

## Flows

This pack does not execute provider calls or model samples. It declares eval
suite specs and recipes, and exposes verified operations that read the pinned
catalog, produce non-executing approval-required plans, and deterministically
score observations supplied by another runtime.

## Functions And Handlers

The only executable functions are the artifact-bound catalog, plan, and local
score operations in `runtime/evaluator.py`. It adds no provider adapter, remote
eval runner, shell script, notebook, or tool manifest.

## Routes

This pack does not add HTTP, WebSocket, desktop, CLI, or frontend routes.

## Events

The pack defines logical event names only:

- `eval.suite.created`
- `eval.contract.completed`
- `eval.provider_smoke.completed`
- `eval.e2e.completed`
- `eval.metrics.reviewed`
- `eval.fit_matrix.updated`
- `eval.promotion_gate.reviewed`

No event handlers are registered.

## Stores

No stores are created. Consumers may persist eval results, cost/latency snapshots, provider evidence, fit matrices, and promotion decisions through existing project, memory, audit, or artifact stores.

## Required Secrets

None.

Provider smoke recipes may require runtime-provided provider credentials, but this pack must not include API keys, bearer tokens, OAuth clients, provider secrets, account ids, private endpoints, or credential placeholders.

## Network

Network default: `none`.

Provider smoke and remote model tests are `explicit_runtime_approval_required`. Contract suites can be run against local mocks or recorded fixtures without network.

## Grants

No grants are required by this pack itself. If a runtime executes provider calls, terminal commands, file writes, or cost-bearing evals, grants must come from underlying runtime/tool packs and approval policy.

## Overlap Interfaces

- `defaultspack`: owns provider catalog and runtime route execution. This pack provides evidence overlays and promotion gates only.
- `rumi_agent_services_pack`: may consume service-routing confidence and flakiness evidence.
- `rumi_code_ide_pack`: may consume coding task pass@k, diff quality, and tool-use smoke evidence.
- `rumi_data_analysis_pack`: may consume SQL, chart, notebook, and analysis-report fit evidence.
