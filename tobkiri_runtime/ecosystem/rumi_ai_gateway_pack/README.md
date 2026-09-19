# Tobkiri AI Gateway Pack

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

The captured `tobkiri.resource.ai.readiness.v1` operation
`rumi_ai_gateway_pack.ai-gateway.preflight` accepts exactly `model_profile_id`
and text `messages`. It resolves the owned model profile, catalog and routing
health information, and requires one exact selected Provider generation
operation. It returns the selected model/provider/catalog identities, without
credential handles, pricing internals, generation or usage charging. The
read-only dispatch adapter rejects effectful dependencies even if a future
resolver accidentally tries to call them. Routing diagnostics remain in memory.

`ready: true` describes current route selection, not a network probe, valid
credentials, execution approval, or a guarantee that a later request succeeds.
Unknown health follows the existing router policy; unavailable routes fail.
Actual invocation still requires current Authority/Broker checks. This source
operation does not activate a Profile edge or complete saved-conversation UI
integration by itself.
