# Rumi Model Registry Pack

This pack is the single authoritative owner for saved model profiles and finite
compatibility aliases. It does not own provider catalog data or provider
execution adapters. Records are provider-neutral routing requirements and may
contain only opaque credential handles, never credential values.

Mutations require an expected store revision and use an atomic owner-local
write. Migration verifies a deterministic source hash, preserves an owner-only
backup, and exposes an explicit rollback operation. Compatibility consumers
must use the global contracts rather than reading this pack's storage.

Validation was not executed by the implementation agent. Independent testing
is required before merge, including startup, migration, rollback, stale
revision, alias resolution, secret rejection, and pack-removal isolation.

