# Operations

## Install

Select `rumi_prompt_studio_pack` and explicitly grant its read/write/migration
capabilities. It has no pack dependency, secret, or network requirement.

## Test

Run `python -m pytest -q tobkiri_runtime/tests/test_rumi_prompt_studio_pack_contract.py`.

Independent QA must run the focused runtime, contract, frontend, migration,
rollback, startup, shutdown, and pack-removal checks in the Wave 4 QA draft.

## Failure Modes

Missing permission, stale plan, stale body hash, replay, artifact mismatch,
process failure, changed migration source, or initialized target fails closed.

## Rollback And Revocation

Use `migration.rollback` with the recorded migration ID before removing the pack
when legacy restoration is required. Removing the pack from the effective set
removes its providers, route, API shims, and UI contribution. Backups remain
until explicit retention cleanup.

## Manual Review Points

Review profile/plan binding, permissions, artifact identity, migration source
hash, owner marker, redacted diagnostics, and rollback evidence.

## Version Ledger

Every prompt artifact release must update `ledgers/prompt_version_ledger.yaml` with prompt id, version, fixture ids, change summary, status, and non-overlap compatibility notes.
