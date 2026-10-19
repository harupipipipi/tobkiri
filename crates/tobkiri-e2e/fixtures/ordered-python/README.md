# Preserved custom Python component

This inactive test fixture is the actual ordered-tool-bridge component produced
by the isolated Python 3.13 trial, not an expected-output stub or a production
Pack. Its exact original Python source and caller provenance are bound by
`component.build.json`; the original source remains in
`packs/local.ai-tool-bridge-ordered-python`.

The producer build/startup provenance is recorded in
`tools/python313-producer/trials/2026-10-19/producer-receipt.json`. The custom
producer is not the official-wheel SDK compiler. Its official source pins and
the reviewed fixed `dicej/wasm-tools` dependency are recorded there. No binary
here enables a Profile or replaces a legacy caller.

- XZ: 3,938,612 bytes, SHA-256
  `85b4d52f8ec9f2f6f530dafa4e1dda64b5fe8c63730509e3839560ce9c4f7165`.
- Component: 17,899,248 bytes, SHA-256
  `3c2aa68e256fa125e2e2a4cb3098704220fdf4832d83c699559ead60617aceac`.
- Build receipt SHA-256:
  `480803d6ac72f53c9bc19d56eab187f9185c30ff2a9457e32c1fd9796c11d87d`.

`scripts/wasm/stage_preserved_ordered_python.py` authenticates the compressed
input, enforces bounded single-stream decompression, verifies the exact output
and build receipt, rechecks the current source plan and oracle, and materializes
a fresh inactive Pack. The separate Rust test must still prove actual runtime
identity, behavior, permissions and audit routing through Core.

The 33 frozen cases contain 12 successful outputs, 12 original-invalid cases,
and 9 explicit domain exclusions. Even a successful bounded replay is not full
unit parity or completion of Issue #1520; production caller replacement and the
documented integer/depth/error-boundary gaps remain separate requirements.
