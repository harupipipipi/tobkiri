# Company compatibility and rollback

Team state is canonical. The first Team-state open for a profile checks for the
legacy `rumi_team_state_store_pack/.../companies.json` file while holding the
profile migration lock. A valid legacy document is converted in memory and the
complete `teams.json` document is atomically published. The evidence record
captures the legacy Pack ID and version, SHA-256 source digest, canonical Team
IDs, actor, timestamp, conflicts, rollback source, and committed activation.

The source Company file is never changed or dual-written. Before activation,
any parse, validation, size, or identity conflict leaves it as the only active
recoverable state. After activation, restart is idempotent because the canonical
Team document wins and retains its source-bound evidence. Downgrade consists of
stopping the newer runtime and using the preserved source file with the older
runtime; changes made after Team activation are intentionally not copied back.

Legacy Python symbols and Company contract/Pack identifiers are sunset adapters
until 2027-12-31. They translate payloads into Team arguments before authority
redemption, call the same Team store, project the authoritative result back, and
record bounded payload-free usage telemetry. Removing the aliases therefore
exposes remaining old consumers without revealing a second runtime or write
path.
