# Issue #1520 migration evidence

`inventory.json` records every legacy ecosystem directory, each Defaults domain
and other Defaults surface, and every Python module in `core_runtime`,
`tobkiri_host` and `tobkiri_protocol`. The aggregate Defaults row intentionally
overlaps its detailed rows. New Rust Harness components do not establish parity
for the old domain handlers.

Refresh after the baseline merge with `python -B migration/inventory.py --refresh`.
Check coverage with `python -B migration/inventory.py`. The final deletion gate is
`python -B migration/inventory.py --require-deletion-ready`; it fails until every
unit carries checked parity, vectors, or approved retirement evidence. Removing a
source does not remove its row or authorize retirement.

Fixture files are frozen input/output evidence, not snapshots of credentials,
approval records or user data. The development recorder must be given an explicit
legacy handler and reviewed fixtures; it never discovers or executes a provider.
