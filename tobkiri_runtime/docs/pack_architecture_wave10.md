# Pack Architecture Wave 10

Wave 10 reduces `defaultspack` to finite compatibility facades. A retained
legacy path may expose an old HTTP/function identifier, export an old snapshot,
or diagnose a missing migration; it may not own primary state, dispatch work,
or render the primary feature UI.

## Kanban cutover boundary

`rumi_kanban_state_store_pack` is the new authoritative owner. Its resource
contract now has board, card, and column lookup operations so a legacy route
shim never opens a second state store. The defaultspack Kanban block remains
temporarily only because it is the source of the current SQLite snapshot and
legacy HTTP route IDs.

The legacy `/api/kanban/*` block now dispatches through
`domain.kanban.contract_facade` rather than constructing `KanbanService`.
Mutations are translated to exact revision-bound owner actions and require a
locally validated approval token unless the call originates from the internal
tool-server approval context. A new explicit
`POST /api/kanban/boards/{board_id}/migrate` alias performs the caller-selected
snapshot export and one-shot owner import. The remaining agent/sync aliases
return `KANBAN_LEGACY_ACTION_DEPRECATED` until their contract-native adapters
are selected; they do not resume the old service.

The legacy `tool_task_board` and `tool_task_board_agent_session` handlers now
return stable `*_LEGACY_TOOL_DEPRECATED` recovery diagnostics. Their former
SQLite writers, JSON import, and direct agent/session coupling were removed.
Those public IDs remain only until a selected task-board adapter exposes their
contract-native replacement; they must not silently recreate state in the
defaultspack namespace.

The defaultspack React workspace no longer imports Kanban components, resources,
or `/api/kanban` client methods. A legacy Kanban tab is now a finite navigation
shim to `/kanban`; the selected `rumi_kanban_surface_pack` owns the removable
isolated UI projection. The old component tree and its direct API test were
removed rather than retained as a hidden fallback.

`domain/kanban/service.py` and `domain/kanban/store.py` have been removed.
The only legacy SQLite access is `legacy_snapshot_reader.py`, which opens the
selected database in SQLite `mode=ro`, performs no schema migration, and reads
only the board, column, card, and bounded event rows needed for a caller-
selected one-shot export.

Unused legacy Kanban models, projections, task-board import helpers, and agent
links have also been removed. The remaining defaultspack Kanban modules are
the HTTP facade, migration export/reader, prompt-note compatibility helper, and
explicit no-write diagnostics.

The old UI/client removal is now complete, so `rumi_kanban_surface_pack` is in
the Wave 9 default profile set. Its route remains isolated and read-only; pack
removal removes the route without changing Kanban state ownership.

The shim cutover must occur in this order:

1. Export a caller-selected old board through a migration-only entrypoint.
2. Redeem one exact `kanban.state.manage` receipt to invoke
   `migration.import_snapshot`.
3. Route all old reads and permitted mutations through
   `rumi.resource.kanban.v1` and `rumi.action.kanban.v1`.
4. Keep an old route only as a finite alias; it must return a supported
   migration diagnostic when the selected board has not been imported.
5. Enable `rumi_kanban_surface_pack` in the default profile after the old
   primary React workspace view and direct resource client are removed.

This sequence intentionally has no live fallback to the SQLite owner and no
dual write. A failed or changed source snapshot is fail-closed and provides a
recovery path: restore the selected old owner snapshot, or retry the identical
source through the one-shot import before routing any writes.

## Company CRUD cutover boundary

`rumi_company_state_store_pack` remains the sole authoritative Company data
owner. Its normalized Company record now includes the base organization
description, metadata, and conversation-group identifier in addition to its
roles, members, channels, tasks, routes, inbound records, and messages. These
fields are persisted only in the selected profile-scoped state-store pack.

The legacy Company create, list, get, update, and delete route blocks now use
`domain.company.contract_facade`. The facade reads through
`rumi.resource.company.v1`, then translates mutations to an exact
revision-bound `rumi.action.company.state.v1` invocation. It obtains the
`company.state.manage` authority receipt from the host bridge and validates a
locally bound approval token for an external route call. An internal
tool-server approval context still goes through the host authority bridge, but
does not ask a second time for the already approved route operation.

The legacy Company settings alias now follows that same facade. Its explicit
`replace` flag is included in the owner action and receipt-bound arguments, so
replace never becomes an unreviewed merge. The existing subagent-team write
guard is evaluated from the selected Company record before the owner action;
the defaultspack settings store is no longer constructed by this route.

The legacy agents alias now maps an agent to the selected owner's role/member
records. `agent.upsert` and `agent.delete` are atomic state-owner actions, so
one user approval binds one revision-based mutation instead of allowing a
facade to consume the same token across independent role and member writes.
Legacy model, status, and agent-specific fields are carried in member metadata
for the compatibility projection; `CompanyAgentStore` is not constructed by
the route.

The legacy channels alias now reads and mutates only the selected Company's
channel records through the facade. It no longer synchronizes Mimo state,
opens the legacy Company/runtime stores, or synthesizes runtime channels in a
state route. Runtime message counts and observability are deliberately omitted
from this alias while the legacy runtime collections return their explicit
sunset diagnostic, so the route has one data owner and no hidden write-on-read
behavior.

The Company compatibility projection keeps the historic organization shape for
these finite CRUD aliases. It derives the legacy `agents` projection from the
authoritative member/role records; it does not read or write the legacy Company
store. Updates for collection-specific old fields fail closed with
`COMPANY_LEGACY_FIELD_DEPRECATED` until their corresponding member, role,
channel, or task facade is cut over. This prevents the base CRUD aliases from
silently starting a second Company writer.

Company status, bootstrap, mention resolution, inbound, messages, and task
dispatch use selected Company state/coordinator contracts. The former
runtime-store collection routes (runs, inbox, threads, and summaries) are
explicit Wave 10 sunset shims: they return
`COMPANY_RUNTIME_ROUTE_SUNSET` until a selected Company runtime contract is
introduced. They never reopen the old SQLite owner.

## Remaining defaultspack inventory

| Legacy surface | Allowed remaining role | Forbidden role after cutover |
|---|---|---|
| `/api/kanban/*` | finite route aliases and migration diagnostics | direct `KanbanService` construction or SQLite access outside migration export |
| `legacy_snapshot_reader.py` | caller-selected read-only snapshot export | schema migration, DB creation, state read/write fallback |
| React Kanban workspace | temporary deprecated route shim | primary UI, direct API client, direct implementation URL |
| `tool_task_board*` | explicit deprecated tool diagnostics | SQLite/JSON state ownership or agent/session dispatch |
| `rumi_kanban_surface_pack` | selected isolated read-only content | state/action ownership or receipt handling |
| `/api/company` CRUD aliases | finite contract facade and legacy projection | `CompanyService` or `CompanyStore` construction, legacy state fallback |
| Company settings alias | finite contract facade and selected-record policy guard | `CompanySettingsStore` construction or unbound replace semantics |
| Company agents alias | atomic role/member compatibility action and projection | `CompanyAgentStore` construction or split writes under one approval |
| Company channels alias | finite selected-state record facade | Mimo sync, `CompanyStore`/runtime-store reads, or synthesized state |
| Company runtime collection routes | explicit `COMPANY_RUNTIME_ROUTE_SUNSET` shim | direct Company runtime-store access or a second writer |

## Release boundary

The compatibility shim must emit a stable machine-readable code for each
unsupported old action, missing selected provider, migration-required board,
and stale revision. It must never silently recreate an old board in the new
store. Rollback selects one prior owner before reopening the legacy route; it
does not replay writes into both stores.

Focused runtime, migration, and cross-platform validation are specified in
`docs/qa/pack_architecture_wave10_qa.md`. This document is a plan and static
ownership record, not execution evidence.

Focused pytest and terminal integrity checks have been run locally. Lint,
build, startup, and real-environment verification still require independent QA.
マージ前に独立した実環境QAが必要です。
