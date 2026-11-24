# Tobkiri AI Strategy Runtime Pack

This Host-brokered Pack discovers and dispatches optional AI strategy Packs
without adding provider-specific code to the AI Gateway or defaultspack.

An installable strategy Pack provides exactly one operation for
`tobkiri.service.ai.strategy.execute.v1`. Its signed Pack v4 manifest declares
the generic strategy runtime and any services it calls as `pack_dependencies`,
along with the matching generic `contract_dependencies`. Profile activation
derives exact caller-to-provider edges from those signed relationships. A new
strategy therefore needs no static Profile edge and no Gateway change.

Saved state uses the full captured `provider_id` as `strategy_reference`.
Short operation suffixes are internal provider-instance details and are not
stable selection identities. The catalog lists only providers present in the
captured Plan with an available backend. Dispatch requires one exact match and
fails closed if the selected Pack is missing, disabled, duplicated, or stale;
it never falls back to direct generation.

Every execute request carries Host-bounded replay, deadline, and cost fences.
Sandbox strategies reach downstream services only through their signed Plan
edges and the generic PackVM capability bridge. Client approval flags,
credentials, and ambient provider authority are not part of this contract.
