# Rumi Company State Store Pack

Owns companies, settings, roles, members, mentions, channels, tasks, routes,
inbound records, and messages. It coordinates no work, invokes no agents,
imports no connector or scheduler, and provides no UI. Every mutation is
revision-bound and receipt-gated.

`migration.operations.import` accepts a caller-supplied legacy Operations
Company snapshot once. It persists a source hash and rejects a second snapshot
with different contents, so the old state is not treated as a live fallback or
dual writer. The imported Company keeps only non-secret identifiers and legacy
schedule references; schedule execution remains independently owned.

