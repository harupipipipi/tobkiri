# Rust candidate for the existing Gateway finalizer

This pack is an additive Rust/Wasm implementation candidate for the existing
`local.ai-gateway-finalizer-python` owner. It exports the unchanged
`local:ai-gateway-finalizer-python/finalizer@1.0.0` interface and imports the
same caller, usage-cost, and stream-normalizer interfaces. Runtime info reports
the actual Rust implementation and names the existing source owner; this pack
does not claim promotion or a new canonical caller identity.

The candidate uses `ordered-python-compat` for ordered finite JSON values and
Python-compatible scalar formatting. It preserves the existing finalizer's
request and Candidate checks, caller/usage/stream call order, typed domain
rejection, imported Core errors, and terminal error mapping. The stream path
still calls the original stream normalizer first, attaches every usage cost
before calling any per-event tool intent, then handles top-level intents.
Imports are generated from the original WIT files. Native builds return an
explicit `unavailable` error at the first dependency boundary rather than
substituting local caller, usage, or stream implementations.

The unchanged Python source suite at `tests/test_gateway_finalizer.py` retains
the ten source-case comparisons against the original Python helpers and the
real Python adapters. The Rust native test also walks the same ten case IDs and
the frozen original Python source observations: it checks exact domain
rejections and confirms that cases requiring imports stop as `unavailable` in
native execution. That native test does not claim candidate output parity for
dependency-backed success/error cases, nor Core/Gate/guest execution. A current
Wasm composition and actual typed-import receipt remain necessary to establish
those results.

This bounded finalizer remains a projection helper. It does not run provider
generation or retry, grants no authority, and does not move usage cost, stream
normalization, or tool calling onto the Host.
