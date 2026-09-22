# Compatibility Alias Migration

Last updated: 2026-07-10

`defaultspack.*` is the canonical function vocabulary. `defaults.*` names are
compatibility aliases and must not be used by new callers.

## Runtime authority

Protocol v4 does not use a compatibility-alias file, legacy Function Registry,
or alias projection as runtime authority. The canonical Defaultspack identity
and implementation set are the v4 Pack manifest, contract catalog,
artifact-index, executable catalog, and bundle lock. Legacy aliases described
on this page are offline migration vocabulary only; they cannot select a
Function, provider, artifact, approval, or authority record.

The strict integrity scan verifies those v4 documents and their real
implementation bytes. It deliberately does not load an alias allowlist or
compare legacy function manifests.

No request arguments, user identifiers, prompt text, file paths, URLs, tokens,
or other payload values are recorded by compatibility telemetry. Audit events
contain only the alias, canonical replacement, migration stage, caller class
(`internal` or `external`), schema version, and whether a warning was emitted.

## Stages

1. **Inventory:** retained as historical migration documentation only.
2. **Warning:** compatibility callers may be measured by their owning migration
   surface, but this is outside the v4 runtime authority boundary.
3. **Enforcement:** new v4 callers must use canonical Function identities from
   the executable catalog.
4. **Removal:** legacy aliases may disappear from offline projections without
   changing the v4 Pack catalog.

## First Removal

The `defaults.model_runtime.*` compatibility group is not a v4 Function
identity. Canonical operation identities come from the v4 executable catalog;
any remaining `defaults.*` spelling is an offline migration concern.

Run the guard locally with:

```bash
cd tobkiri_runtime
python scripts/quality/scan_defaultspack_integrity.py --strict
```
