# Defaultspack v4 cutover inventory

Inventory baseline: repository commit
`64b2240e2e3d019c97920b6fb0e278cca83d6691` (2026-08-05).

This document records the pre-cutover difference between the normative Protocol
v4/Authority Kernel model and the bundled defaults implementation, together with
the current Phase 0 result. It is not a compatibility promise.

## Phase 0 result

The artifact and Authority-path foundation is implemented, but semantic Pack
migration is not complete. The tracked complete-v4 scanner currently inventories
140 bundled production Packs and requires four canonical v4 artifacts per Pack.
It classifies 41 Packs as `semantically-reviewed` and 99 as `generated-draft`;
the semantic migration evidence is therefore `RED`. During Phase 0, CI blocks
evidence freshness drift while reporting this semantic status without treating
it as a completed cutover. The scanner reports zero findings for artifact
contracts, Authority/ResolvedPlan scope, reachable legacy lookup/fallback,
double authority, Launcher safety, and offline projection identity. The exact
counts and source commit are generated in:

- `generated/architecture/architecture_inventory.json`
- `scripts/quality/evidence/complete_v4_migration_red_64b2240e.json`
- `tests/test_complete_v4_migration_gate.py`

The baseline tables below are retained as historical removal evidence. They do
not describe a live production fallback.

## Normative authority

The cutover is governed by:

- `tobkiri_protocol/schemas/pack_manifest_v4.schema.json`
- `tobkiri_protocol/schemas/profile_v4.schema.json`
- `tobkiri_protocol/schemas/base_definition_v1.schema.json`
- `tobkiri_protocol/schemas/shell_definition_v1.schema.json`
- `tobkiri_protocol/schemas/profile_lock_v1.schema.json`
- `tobkiri_protocol/schemas/resolved_plan_v1.schema.json`
- `tobkiri_protocol/schemas/activation_record_v1.schema.json`
- `docs/runtime-authority-v4.md`
- `docs/ADR-016_BASE_SHELL_APPLICATION_MODEL.txt`
- `ecosystem/defaultspack/pack.v4.json`
- `ecosystem/defaultspack/contracts.v4.json`
- `ecosystem/defaultspack/artifact-index.v4.json`
- `ecosystem/defaultspack/executables.v4.json`
- `ecosystem/defaultspack/v4/bundle.lock.json` and the documents named by it

Only schema-valid, digest-pinned documents selected by an explicit Profile are
runtime inputs. A legacy document can be read only by an offline migration tool.
It is never an activation candidate and never supplies identity, approval,
workspace, provider, or permission authority.

## Baseline conflicts

| Area | Baseline state | Required cutover |
| --- | --- | --- |
| Pack manifest | The retired legacy Defaultspack manifest was runtime-authoritative and used ecosystem-specific keys. | A schema-valid `io.tobkiri.pack.v4` manifest is the only bundled defaultspack manifest. |
| Duplicate pack | `ecosystem/defaults/` contains 408 tracked files duplicating the default implementation. | Remove the duplicate Pack; retain no runtime import or clone path. |
| Profiles | `profiles/startup.profile.yaml` is legacy; the three `defaults-modern` files claim v4 while using a private, incompatible shape. | Use canonical Profile v4 plus explicit Base and Shell definitions. |
| Base/Shell | Private assets under `domain/pack_architecture/assets` define a second `io.tobkiri.pack.v4` dialect. | Validate against the Protocol schemas and pin exact Base/Shell artifacts. |
| Provider selection | Private resolver derives backend identity from `backend.provider_ids`; other runtime paths retain registry-first selection. | Resolve each requested Contract operation to exactly one selected Function principal. Missing or duplicate providers deny. |
| Authority | Legacy manifests and function metadata contain `approved`, `host_execution`, approval policy, or permission-like fields. | Runtime authority is only an opaque Authority Kernel snapshot/reference captured in the resolved Profile/Plan. |
| Workspace | Several defaultspack domains accept ambient paths and environment-derived roots. | Activation binds one resolved workspace root; traversal and cross-workspace paths deny before dispatch. |
| Routes | `routes.json`, `compat_aliases.yaml`, and `docs/legacy_http_routes.yaml` coexist with Function manifests and block fallbacks. | Calls use exact Contract/Operation/Function principal identities. Unknown legacy calls fail closed; finite offline projection is migration-only. |
| Setup | `setup_pack/defaultspack/pack.json` advertises “v2 cloned from defaults”; `basepack` targets defaultspack implicitly. | Setup selects explicit Base then compatible Shell and persists the v4 Profile/Lock/activation state. |
| Discovery | setup/profile paths can scan every installed `ecosystem.json` and use fallback identifiers. | The default activation service consumes an explicit, digest-pinned inventory only. |
| Persistence | active selection and restart state use legacy startup/profile stores. | Atomic Profile, Lock, and Activation records are the sole restart inputs; stale or tampered records deny. |

## Baseline removal sets

The following are runtime compatibility surfaces scheduled for deletion, not
extension:

1. `ecosystem/defaults/` (408 tracked files at baseline).
2. The retired legacy Defaultspack manifest as runtime authority.
3. `ecosystem/defaultspack/routes.json`.
4. The retired Defaultspack compatibility-alias allowlist.
5. `ecosystem/defaultspack/docs/legacy_http_routes.yaml`.
6. `ecosystem/defaultspack/profiles/startup.profile.yaml`.
7. Private pseudo-v4 schemas and manifests below
   `ecosystem/defaultspack/domain/pack_architecture/assets`.
8. Function/block dispatch fallback metadata (`vocab_aliases`,
   `extensions.defaultspack.block_module`, and fallback block modules) on the live
   v4 path.
9. Implicit defaultspack promotion and first-installed/global-registry fallback.
10. Ambient environment values used as identity, approval, workspace, credential,
    or provider authority.

## Required negative regression

The cutover remains complete only while tests deny all of the following:

- missing or duplicate foundational conversation Provider;
- unapproved, hash-drifted, or unlisted artifacts;
- path traversal and a path resolved outside the bound workspace;
- unresolved, stale, or digest-mismatched ProfileLock/ResolvedPlan;
- a Shell that is absent, ambiguous, incompatible, or not exactly pinned;
- a requested edge without dependency closure;
- a client-supplied approval/identity field;
- an unknown legacy route, function alias, or Registry lookup;
- restart from any record other than the last atomically committed v4 activation.

Generated inventories and projections are now checked in and freshness-gated.
They are regenerated offline and are never read as runtime authority inputs.
