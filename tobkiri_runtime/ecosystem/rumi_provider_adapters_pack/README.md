# Rumi Provider Adapters Pack

This pack owns provider protocol execution only. The AI gateway selects a
catalog model, the provider registry supplies an enabled connection and
adapter protocol, and the credential broker resolves a caller-bound opaque
handle. The adapter never scans installed packs or reads environment secrets.

The initial compatibility protocols cover OpenAI-compatible and Anthropic
request envelopes without branching on provider or pack IDs. Unsupported
protocols return `incompatible`; unconfigured connections return
`not_configured`. Streaming has a distinct contract and normalized event
envelope, even when a provider requires buffered compatibility execution.

Validation was not executed by the implementation agent. Independent testing
is required before merge, including startup, credential binding, deadline,
network failure, normalized streaming, unsupported protocols, and rollback.

