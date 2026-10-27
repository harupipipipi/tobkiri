# Pack Architecture Wave 0

Wave 0 introduces the vocabulary-neutral `rumi.pack.v3` and typed global
contract foundation. It does not move feature runtime ownership or add a
provider.

## Architecture invariants

- Pack IDs may appear only in manifests, bundles, lockfiles, migration maps,
  compatibility shims, and repository tests.
- Discovery reads JSON metadata and schemas only. Loading a manifest never
  imports its entrypoint.
- A descriptor is not an authority grant. `trust_class`, capabilities, and
  signatures are evidence consumed by later policy and authority decisions.
- Consumers receive an opaque provider instance handle. They do not receive a
  source path, module, or sibling private-storage location.
- The legacy registry remains authoritative during Wave 0. Its adapter is a
  read-only `legacy -> v3` projection, scheduled for removal in Wave 10.
- No dual-write or first-found fallback is permitted.

## Contract semantics

The canonical namespaces are `rumi.service.*`, `rumi.action.*`,
`rumi.event.*`, `rumi.resource.*`, `rumi.policy.*`, `rumi.ui.*`,
`rumi.storage.*`, and `rumi.transport.*`.

`one` rejects an equal-priority tie. `many`, `chain`, and `fanout` return a
stable ordered set. `keyed` requires exactly one matching stable instance key.
`optional` distinguishes absence as `not_configured`; it does not turn absence
into an empty collection. All calls return one of `ok`, `unknown`,
`unavailable`, `not_configured`, `denied`, `incompatible`, `missing_provider`,
`stale_resolution`, or `invalid_manifest`.

Canonical identity is SHA-256 over UTF-8 JSON with sorted keys, no insignificant
whitespace, preserved Unicode, and non-finite numbers rejected. It is rendered
as `sha256:<lowercase hex>`.

## Contract examples

| Example | Contract namespace | Cardinality |
|---|---|---|
| AI provider | `rumi.service.ai.generate.v1` | `keyed` |
| Tool executor | `rumi.action.tool.execute.v1` | `one` |
| UI route | `rumi.ui.route.module.v1` | `many` |
| Storage | `rumi.storage.record.v1` | `one` |
| Event sink | `rumi.event.audit.recorded.v1` | `fanout` |
| Policy guard | `rumi.policy.operation.guard.v1` | `chain` |

The complete metadata-only service example is in
`examples/pack_v3/minimal_service.json`.

## Data ownership matrix

| Resource | Authoritative pack | Schema version | Storage | Backup | Migration | Rollback | Retention | Export/import |
|---|---|---:|---|---|---|---|---|---|
| profiles | existing profile owner | existing | unchanged | unchanged | Wave 2 | Wave 2 plan | unchanged | unchanged |
| settings | existing settings owner | existing | unchanged | unchanged | later Wave | later Wave | unchanged | unchanged |
| secrets | core authority boundary | existing | unchanged | unchanged | none | unchanged | unchanged | unchanged |
| conversations | existing conversation owner | existing | unchanged | unchanged | Wave 7 | Wave 7 plan | unchanged | unchanged |
| messages | existing conversation owner | existing | unchanged | unchanged | Wave 7 | Wave 7 plan | unchanged | unchanged |
| prompts | defaultspack | existing | unchanged | unchanged | Wave 4 | Wave 4 plan | unchanged | unchanged |
| tools | defaultspack | existing | unchanged | unchanged | Wave 6 | Wave 6 plan | unchanged | unchanged |
| provider connections | defaultspack | existing | unchanged | unchanged | Wave 5 | Wave 5 plan | unchanged | unchanged |
| artifacts | existing artifact owner | existing | unchanged | unchanged | later Wave | per-Wave | unchanged | unchanged |
| schedules | defaultspack | existing | unchanged | unchanged | Wave 9 | Wave 9 plan | unchanged | unchanged |
| memory | defaultspack | existing | unchanged | unchanged | Wave 7 | Wave 7 plan | unchanged | unchanged |
| knowledge | defaultspack | existing | unchanged | unchanged | Wave 7 | Wave 7 plan | unchanged | unchanged |
| Company data | defaultspack | existing | unchanged | unchanged | Wave 9 | Wave 9 plan | unchanged | unchanged |
| approvals | core authority boundary | existing | unchanged | unchanged | none | unchanged | unchanged | unchanged |
| audit records | core authority boundary | existing | unchanged | unchanged | none | unchanged | unchanged | unchanged |
| v3 contract projection | `core_runtime.global_contracts` | 3.0.0 | in-memory snapshot | regenerated | legacy-to-v3 read-only | remove projection | process lifetime | canonical JSON |

## Migration and rollback

Wave 0 adds no new writer. Rollback removes the v3 registry and projection while
leaving the authoritative legacy registry unchanged. Compatibility aliases must
declare their owner, target, removal Wave, and sunset date. Wave 10 removes the
legacy projection after downstream consumers use the resolved profile contract
set.

Validation was not executed by the implementation agent.
Independent testing is required before merge.
