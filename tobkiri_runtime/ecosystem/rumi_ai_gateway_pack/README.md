# Rumi AI Gateway Pack

This pack owns provider-neutral AI routing, generation, stream normalization,
usage normalization, replay-safe failover, and routing diagnostics. It contains
no concrete provider implementation or provider-specific model catalog.

Requests express capabilities, modalities, context, request surface, residency,
cost, deadline, and optional preferences. Routing joins independently selected
catalog and execution handles. Opaque credential handles pass through to the
selected adapter; credential values never enter the gateway.

Provider failure never becomes an empty response. Failover requires an explicit
policy flag, an idempotency key, no tool payload, a retryable error, and another
eligible selected provider. Human handoff is not treated as an automatic
provider fallback.

