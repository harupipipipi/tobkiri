# Reusing verified Audit row images

The generic native AuditLog still scans every stored row before an append.
Within one open connection, it may reuse a previous successful semantic chain
verification when the complete ordered SQLite value image is identical. The
image includes all eight columns, storage classes, framed text/blob lengths,
row count and tail. It is a fixed-size in-memory digest state, not a second
persisted trust record or a tail-only check.

The connection mutex covers taking the previous image, `BEGIN IMMEDIATE`,
scanning, semantic fallback when needed, the existing exact event-identity
comparison, quota checks, INSERT and commit. The candidate image is published
only after commit acknowledgement. Any intervening error discards reuse.
Open/reopen and explicit verification always perform full semantic verification;
explicit verification also clears reuse. A changed image triggers the original
verifier before append admission. Existing integrity error classes are retained.

The change introduces no Profile, Pack, Gate, permission, database schema or
quota exception. Writer/minute/size participate in change detection, but their
existing quota interpretation is unchanged; this does not add independent
authentication of those metadata fields. SQLite-observed value identity is not
physical-file attestation or an anti-rollback anchor. The raw scan remains
linear in history size per append, so a growing append sequence is still
quadratic overall.

Use the opt-in `tobkiri-store` `audit_performance` test to measure disposable
fixtures with ordinary quotas. Its theoretical prior-row count is not a
measurement of semantic decoder calls after reuse. Single fixture timings do
not establish real Core, model or chat latency. The regression cases cover
same-head body tampering, foreign valid append, canonical-equivalent stored
JSON, exact event identity, quota ordering, begin/scan/insert/commit failures,
lost acknowledgement, reopen, explicit verification, typed value framing and
negative or discontinuous sequence classification.
