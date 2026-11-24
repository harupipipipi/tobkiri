# Rust candidate for the existing Gateway tool caller helper

This additive candidate implements the helper interface owned by
`local.ai-gateway-tool-caller-python`. Its WIT identity remains
`local:ai-gateway-tool-caller-python/pack@1.0.0`, and its only dependency is the
existing typed `local:ai-tool-bridge-ordered-python/bridge@2.0.0` import. Runtime
info identifies this Rust candidate and its Python source owner; it does not
claim promotion or introduce a new logical caller identity.

The three helper operations retain their source request shapes, ordered finite
JSON transport, operation IDs, per-event streaming order, and error boundaries.
Imported Core errors pass through as the original typed errors. Native execution
reports `unavailable` when an imported bridge call is required; event-helper
requests with no matching tool-intent events remain local and need no import.

This pack is source only. It does not replace the original four full Gateway
callers, provide a local normalization fallback, or claim output parity or
Core/Gate/guest execution. A composed Wasm run with the real typed import is
required to establish dependency-backed behavior.
