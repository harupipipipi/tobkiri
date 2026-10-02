# Pack Architecture Wave 4 — Prompt Studio Pilot

Wave 4 moves prompt authoring to `rumi_prompt_studio_pack`. The pack is an
optional, independently versioned process provider. Chat starts without it;
when selected in the active `ResolvedProfile`, its route, API compatibility
shims, resource provider, and isolated UI appear together.

## Authoritative boundary

The only authored prompt store is:

```text
user_data/packs/rumi_prompt_studio_pack/profiles/<profile>/prompt_studio.store.json
```

Writes are atomic and require an expected body hash. Save, delete, toggle,
version, and rollback all use the same lock and store revision. A first-write
rollback removes the override. Metadata recursively drops secret-, token-,
credential-, authorization-, and API-key-like fields.

`defaultspack` retains no prompt writer. Its finite `/api/prompts/*` routes and
function aliases bind the active profile and call typed global contracts. The
legacy Prompt Manager reads authored records and forwards mutations through
the same owner contract; it no longer reads or writes the old shared JSON
store. Chat composition consumes a read-only projection and continues with its
built-in composition sources when Prompt Studio is absent.

## Contracts and isolation

- `rumi.resource.prompt.studio.v1`: list, get, editor load, active summary,
  trace projection.
- `rumi.action.prompt.author.v1`: save, delete, toggle, diff, lint, compact,
  conversion, build, and compatibility authoring operations.
- `rumi.action.prompt.version.v1`: versions and rollback.
- `rumi.action.prompt.test.v1`: provider- and tool-free rendering testbench.
- `rumi.action.prompt.migrate.v1`: inspect, import, apply, and rollback.

Discovery validates JSON only and does not import pack code. Activation verifies
the artifact index and process module hash before registering a provider handle.
Calls execute in a single-request subprocess with no injected credentials or
network configuration. The selected provider identity must exactly match the
active resolved plan.

## UI and capability broker

The route descriptor is verified against the pack artifact identity. The host
loads it in an opaque-origin sandbox and passes only a one-time RPC nonce and
profile identifier. The frame receives no bearer token. The same-origin host
broker binds every request to profile, plan hash, contribution, owner, declared
contract, authenticated local UI approval, expiry, and one-time request ID.

Removing the pack removes the verified route contribution and the conditional
legacy prompt routes. Defaultspack contains no Prompt Studio component import
or product-specific navigation callback.

## Migration and rollback

The migration adapter reads only fixed legacy roots: profile prompt Markdown/
text files and defaultspack's historical shared prompt JSON directory. It
normalizes an inventory and returns a source hash during inspect. Apply re-reads
the inventory, rejects a changed hash, and sends records to the new owner. The
new owner creates a mode-`0600` backup, verifies the new store, and atomically
writes the owner marker. Legacy files are never edited or deleted.

Rollback requires the recorded migration ID, preserves a mode-`0600` snapshot
of the new store, removes the new store and owner marker, and leaves legacy data
available. Migration refuses to overwrite an already initialized target.

## Data ownership matrix

| Resource | Authoritative pack | Schema version | Storage | Backup | Migration | Rollback | Retention | Export/import |
|---|---|---:|---|---|---|---|---|---|
| profiles | core profile owner | resolved-profile v1 | existing profile store | exact copy | Wave 2 | Wave 2 | existing | JSON |
| settings | feature owner through settings contract | contribution v1 | owner storage | owner policy | per feature Wave | owner rollback | owner policy | contract-defined |
| secrets | core authority boundary | existing | secret store only | unchanged | none | unchanged | unchanged | prohibited in catalogs |
| conversations | existing conversation owner | existing | unchanged | unchanged | Wave 7 | Wave 7 | unchanged | unchanged |
| messages | existing conversation owner | existing | unchanged | unchanged | Wave 7 | Wave 7 | unchanged | unchanged |
| prompts | `rumi_prompt_studio_pack` | prompt-studio store v1 | pack/profile namespace | owner-only migration backup | fixed-root one-way import | owner marker rollback | profile lifetime plus backups | contract-defined JSON |
| tools | defaultspack | existing | unchanged | unchanged | Wave 6 | Wave 6 | unchanged | unchanged |
| provider connections | defaultspack | existing | unchanged | unchanged | Wave 5 | Wave 5 | unchanged | unchanged |
| artifacts | existing artifact owner | existing | unchanged | unchanged | later Wave | later Wave | unchanged | unchanged |
| schedules | defaultspack | existing | unchanged | unchanged | Wave 9 | Wave 9 | unchanged | unchanged |
| memory | defaultspack | existing | unchanged | unchanged | Wave 7 | Wave 7 | unchanged | unchanged |
| knowledge | defaultspack | existing | unchanged | unchanged | Wave 7 | Wave 7 | unchanged | unchanged |
| Company data | defaultspack | existing | unchanged | unchanged | Wave 9 | Wave 9 | unchanged | unchanged |
| approvals | core authority boundary | existing | unchanged | unchanged | none | unchanged | unchanged | unchanged |
| audit records | core authority boundary | existing | unchanged | unchanged | none | unchanged | unchanged | unchanged |
| Prompt Studio UI | `rumi_prompt_studio_pack` | contribution v1 | verified pack assets | artifact index | descriptor adoption | remove pack | pack version | pack artifact |

Validation was not executed by the implementation agent.
Independent testing is required before merge.
