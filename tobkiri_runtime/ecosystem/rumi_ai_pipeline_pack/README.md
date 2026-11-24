# Rumi AI Pipeline Pack

This pure service normalizes AI request envelopes and owns retry/failover
policy. It accepts only opaque credential handles. Failover requires explicit
opt-in, an idempotency key, no tools, a retryable error, another candidate, and
remaining deadline. It never executes a provider.

Validation was not executed by the implementation agent. Independent testing
is required before merge, including elapsed deadlines, raw credentials, every
failover denial reason, deterministic replay, and provider removal.

