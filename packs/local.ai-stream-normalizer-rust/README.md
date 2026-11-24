# Rust stream normalizer candidate

This is an additive Rust translation of the pure stream projection exposed by
`local:ai-stream-normalizer-python/stream-normalizer@1.0.0`. It reuses that WIT
contract unchanged, including its Gate boundary and ordered-finite-json-v2 string
payloads. The candidate identifies itself as
`local.ai-stream-normalizer-rust`, implementation `rust`; it does not claim to
contain CPython or to be the complete original Pack migration.

The source mirrors the original three operation names, ordered event projection,
`request_id` trimming, provider-attempt truthiness and integer coercion,
terminal-event rule, and fixed public error categories/messages. The shared
`ordered-python-compat` crate provides the finite ordered JSON codec and
Python-compatible value helpers. The request/result are DATA only; nested values
are never interpreted as authority.

The source-level candidate has not been built or executed. It adds no workspace
registration, component receipt, production identity, or frozen-vector changes.
Production readiness, complete Pack migration, GUI approval parity, and full
source-level differential acceptance are not claimed here.

The host-side source differential test reads a `receipt.json` plus its hashed
input/output sidecars from the original-CPython oracle tool. Set
`TOBKIRI_PYTHON_COMPAT_RECEIPT` to that receipt path to run the exact WIT
observation comparison. When set, also set `TOBKIRI_STREAM_NORMALIZER_ACTUAL_DIR`
to a new absolute directory path. The native test creates that directory with
private permissions, writes each exact Rust result or closed WIT code/message,
and saves `summary.json` before failing on any mismatch. It compares every
source case, including native parser resource-edge cases. These actuals are
native source differential evidence only; they do not establish Wasm execution,
GUI approval parity, Setup readiness, or production activation.
