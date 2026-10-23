# Preserved custom Python component

This inactive test fixture is the actual ordered-tool-bridge component rebuilt
by the isolated Python 3.13 trial on 2026-10-20, not an expected-output stub or a
production Pack. Its exact original Python source and current caller provenance
are bound by `component.build.json`; the original source remains in
`packs/local.ai-tool-bridge-ordered-python`.

The producer build/startup provenance is recorded in
`tools/python313-producer/trials/2026-10-19/producer-receipt.json`. The custom
producer is not the official-wheel SDK compiler. Its official source pins and
the reviewed fixed `dicej/wasm-tools` dependency are recorded there. The current
fixture refresh is explained by
`tools/python313-producer/trials/2026-10-20-finalization/source-rebinding.json`.
The original executable bridge and all 33 source case observations are
unchanged; the reviewed Gateway helper extraction changed their provenance.
The previous fixture and passing checkpoint remain preserved at commit
`92f38e3241b802605a4af59ba35aae72ee73cdbd`, with the earlier trial evidence
unchanged. Those historical receipts do not authenticate the current source
plan. No binary here enables a Profile or replaces a legacy caller.

- XZ: 3,952,452 bytes, SHA-256
  `c34ba42e584ab8b6e2fdbfc94e102cef385bc4edc13083cd0d1fe54f7823ce04`.
- Component: 17,921,530 bytes, SHA-256
  `9beee26b1ad8229c04571eca1f22d47f9d23e1a8475324919f8c1fc4c7ea1e94`.
- Build receipt SHA-256:
  `496d94b8a3ed9b53e97a58c8f58a17f42ea3d5d1837bb1466b039a82ae6d867a`.

`scripts/wasm/stage_preserved_ordered_python.py` authenticates the compressed
input, enforces bounded single-stream decompression, verifies the exact output
and build receipt, rechecks the current source plan and oracle, and materializes
a fresh inactive Pack. The separate Rust test must still prove actual runtime
identity, behavior, permissions and audit routing through Core.

The fresh component passed the isolated 33-case development replay with actual
guest CPython 3.13.16 and Unicode 15.1.0. Admitted outputs and all explicit
exclusions passed. This is not native Core acceptance.

The 33 frozen cases contain 12 successful outputs, 12 original-invalid cases,
and 9 explicit domain exclusions. Even a successful bounded replay is not full
unit parity or completion of Issue #1520; production caller replacement and the
documented integer/depth/error-boundary gaps remain separate requirements.
