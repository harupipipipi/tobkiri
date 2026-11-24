# Pack Architecture Wave 2

Wave 2 introduces one immutable `ResolvedProfile` as the complete runtime plan
for a profile revision. Startup and Capability Graph loading consume the same
plan and no longer derive independent pack sets.

## Resolution inputs and identity

Resolution includes profile revision, manifest hashes, platform, policy
revision, lockfile revision, requested packs/contracts, observed health, and
approval-derived authorization. Canonical JSON produces stable `input_hash` and
`plan_hash` identities. A running request remains bound to its plan revision;
changes create a new plan rather than mutating the old one.

The plan keeps `requested`, `available`, `selected`, `healthy`, and `authorized`
separate. Selection is never a permission grant. The effective pack set contains
only selected, available, healthy, and authorized packs. Pack dependencies are
explicit manifest edges; filesystem order and installed-pack fallback are not
resolution inputs.

## Complete projection

The plan precomputes routes, UI modules, tools, prompts, models/providers,
services, resources, graphs, policies, and scheduler contributions. Loaders
consume the context-bound effective set. Removing a pack therefore removes all
of its projections together. A loader cannot silently consult an installed pack
outside the current plan.

Contract provider selection uses the Wave 0 cardinality semantics. Optional
absence remains diagnostic and does not become an empty success. Effective
permissions are the intersection of pack requests and profile policy, never the
union of provider capabilities.

## Lockfile

`ProfileLockfile` pins every selected pack, provider, and projected resource by
version and content hash. It stores only opaque credential handles and scopes;
secret values and bearer tokens are not accepted fields. Generation, atomic
write, verified read, stale validation, and explicit refresh are provided.
Changed manifests/resources, missing packs, profile revision changes, and plan
hash changes produce actionable stale-resolution diagnostics without fallback.

## Legacy migration and rollback

`setup_pack_selection.json` is a one-way compatibility input. Migration supports
dry-run diff, exact backup, atomic profile update, and rollback. User fields are
preserved. The legacy file is never updated, so no dual-write is introduced.

## Data ownership matrix

| Resource | Authoritative pack | Schema version | Storage | Backup | Migration | Rollback | Retention | Export/import |
|---|---|---:|---|---|---|---|---|---|
| profiles | core profile owner | resolved-profile v1 | existing profile store | exact pre-migration copy | legacy setup selection one-way import | restore exact backup | existing policy | JSON |
| settings | existing settings owner | existing | unchanged | unchanged | later Wave | later Wave | unchanged | unchanged |
| secrets | core authority boundary | existing | secret store; opaque handle in lockfile | unchanged | none | unchanged | unchanged | no secret export |
| conversations | existing conversation owner | existing | unchanged | unchanged | Wave 7 | Wave 7 plan | unchanged | unchanged |
| messages | existing conversation owner | existing | unchanged | unchanged | Wave 7 | Wave 7 plan | unchanged | unchanged |
| prompts | defaultspack | existing | unchanged; hash projected | unchanged | Wave 4 | Wave 4 plan | unchanged | unchanged |
| tools | defaultspack | existing | unchanged; hash projected | unchanged | Wave 6 | Wave 6 plan | unchanged | unchanged |
| provider connections | defaultspack | existing | unchanged; opaque handle projected | unchanged | Wave 5 | Wave 5 plan | unchanged | unchanged |
| artifacts | existing artifact owner | existing | unchanged | unchanged | later Wave | per-Wave | unchanged | unchanged |
| schedules | defaultspack | existing | unchanged; hash projected | unchanged | Wave 9 | Wave 9 plan | unchanged | unchanged |
| memory | defaultspack | existing | unchanged | unchanged | Wave 7 | Wave 7 plan | unchanged | unchanged |
| knowledge | defaultspack | existing | unchanged | unchanged | Wave 7 | Wave 7 plan | unchanged | unchanged |
| Company data | defaultspack | existing | unchanged | unchanged | Wave 9 | Wave 9 plan | unchanged | unchanged |
| approvals | core authority boundary | existing | unchanged | unchanged | none | unchanged | unchanged | unchanged |
| audit records | core authority boundary | existing | unchanged | unchanged | none | unchanged | unchanged | unchanged |
| resolved runtime plan | `core_runtime.resolved_profile` | v1 | immutable memory snapshot | lockfile | startup/profile bridge | bind prior revision | run lifetime | canonical JSON |
| profile resource lock | core profile owner | v1 | owner-only JSON | Git/user backup policy | explicit refresh only | restore prior lock | profile lifetime | canonical JSON |

Validation was not executed by the implementation agent.
Independent testing is required before merge.
