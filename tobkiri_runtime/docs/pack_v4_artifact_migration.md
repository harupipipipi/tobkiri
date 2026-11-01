# Pack v4 artifact migration

Tobkiri's production Pack artifact source is
`schemas/pack_v4_catalog.v1.json`. The catalog contains 139 ecosystem Packs.
`defaults` and `defaultspack` are intentionally excluded because their
migration is owned by the defaultspack migration stream.

The catalog was imported once from 95 v3-authoritative and 44 legacy-only
Packs. The import retains Pack and Contract dependencies, capabilities,
network and secret requirements, host-execution and workspace boundaries,
approval policy, provider semantics, schemas, legacy source evidence, runtime
artifact digests, and source evidence. A normal generation never
reads `rumi.pack.v3.json` or `ecosystem.json`.

## Canonical and compatibility files

Each migrated Pack has three production artifacts:

- `pack.v4.json`: canonical Pack identity, requirements, functions,
  operation/provider catalogs, migration metadata, and provenance.
- `contracts.v4.json`: exact operation schemas and provider semantics for the
  contracts implemented by the Pack.
- `artifact-index.v4.json`: hashes for the generated documents and runtime
  artifacts plus a deterministic canonical integrity seal.

The old `rumi.pack.v3.json` and `ecosystem.json` documents are offline,
read-only compatibility projections. They are not fallback authority sources.
Legacy Contract IDs are retained in `migration.legacy_ids`; production
Contract IDs use the `tobkiri.*` namespace. Provider and Operation IDs are
Pack-qualified so a registry never needs an ambiguous global fallback. Legacy
component and connectivity operations remain only in the canonical source
catalog's offline migration evidence; they are never emitted into a v4
`operation_catalog`. Declarative content Packs therefore have empty Function,
Provider, Contract, and Operation catalogs rather than synthetic executable
principals.

## Generation and verification

From `tobkiri_runtime/`, regenerate or verify the artifacts with:

```bash
python scripts/migrate_pack_artifacts_v4.py
python scripts/migrate_pack_artifacts_v4.py --check
```

`--check` is read-only and rejects missing, hand-edited, stale, malformed, or
internally inconsistent output. It also validates schemas, source identities,
artifact hashes, Contract revisions, Function-to-Contract-to-Operation
principal closure, schema hashes, integrity seals, and global
Contract-owner/provider/Operation uniqueness. The Defaultspack bundle and
executable catalog have separate official `--check` generators; CI runs all
three checks together.

`--import-legacy` is reserved for replaying the one-way migration from the
pinned migration commit. It must not be used by runtime discovery, profile
resolution, dispatch, or ordinary artifact builds.

## Classification

v3 Contracts become accepted v4 Contracts with one deterministic Contract
owner and one or more Pack-qualified providers. Entry points become exact
qualified Operations, each covered by one finite Function principal path. A
legacy component/connectivity name is retained only as migration evidence; it
does not become a v4 Operation. Packs that only ship declarative content have
empty Function/Provider/Contract/Operation catalogs; the generator does not
invent executable Operations to fill those catalogs.

## Runtime receipt execution evidence

Compiling or counting an operation does not demonstrate that it ran. The v1
runtime ledger remains readable for historical inspection, but a nonempty
Pack is release-eligible only when its isolated-conformance receipt contains
`operation_executions`: exactly one successful measured Host invocation for
each reviewed `(function_id, v4_contract_id, v4_operation_id)` identity. Counts
alone, duplicate/reused invocations, missing operations, substituted identities,
and failed outcomes fail release validation. New invocation observations must
be timezone-aware timestamps within the install-to-conformance observation
window; empty reviews cannot carry phantom execution records.

Each execution record uses `method: host-operation-invocation.v1`, identifies
its Pack, target artifact digest and isolated Host instance, and carries its
`function_id`, `contract_id`, `operation_id`, distinct `invocation_id`,
`observed_at`, `outcome`, and measured `input_digest`/`output_digest`. The
surrounding conformance and release receipt digests bind these records. For a
review that references generated semantics by digest, the operation identities
must come from that exact digest-bound semantic comparison. Hashes establish
integrity of recorded measurements; they do not replace invocation or review.

The current `tests/conformance_support/collect_pack_runtime_receipts.py`
collector validates catalog, compilation, admission, install and approval. It
has no supported actual per-operation execution probe. It therefore fails
closed for every nonempty executable Pack instead of treating inventory counts
as successful executions. Empty inventories retain the existing independently
reviewed zero-operation and admission-only paths.

The checked-in 143 historical runtime receipts are unchanged: the 95 nonempty
inventory-only records cannot establish release eligibility. The 39
admission-only and 9 independently reviewed zero-operation records retain their
existing eligibility under this check. The aggregate migration release gate
must remain red until genuine exact-artifact execution evidence exists; do not
rehash historical attestations or relax gate expectations to restore green.
