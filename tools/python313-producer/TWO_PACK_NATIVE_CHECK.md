# Original Python caller and bridge: additional native check

This is an additional test route for two **separate** Python Packs. It does not
replace the historical development composition, the WAT consumer fixtures, or
the full Gateway. Do not use a precomposed component in place of either Pack:
the purpose is to observe the actual caller-to-provider edge through Core/Gate.

## Immutable inputs and packaging

The checked-in transport fixtures can be authenticated and prepared without a
producer, source-oracle regeneration, or guest execution:

```sh
python3 -B scripts/wasm/stage_preserved_gateway_tool_caller.py \
  --output /absolute/path/to/fresh-staged-inputs
```

The output contains `graph/` and `materialization.json`, published together only
after all pinned compressed/decompressed identities, current source plans and
the fixed graph index pass. The graph directory used below is
`/absolute/path/to/fresh-staged-inputs/graph`. Preparation rejects existing
destinations and linked/nonregular inputs. All private scratch directories use
the validated output parent, not a potentially symlinked platform default
temporary directory. No preparation receipt claims native acceptance.

The existing guarded macOS CI job runs this preparation and then the exact
native test below, retaining both preparation and replay diagnostics. Workflow
presence and passing local routing tests do not establish an executed CI result.

`scripts/wasm/materialize_gateway_tool_caller_graph.py` verifies the reviewed
caller/bridge component and build-receipt pins, reconstructs current source
plans, and publishes only a fresh inactive graph directory. Its CLI requires
both input directories, explicit pins and their hash, and a new output path.
Each input directory contains `component.wasm` and `component.build.json`.
The script does not build, instantiate, activate, or execute either component.

The graph contains 35,798,851 bytes of component source, below the unchanged
64 MiB Core compilation catalog limit. The five separate finalization
components total 89,513,931 bytes and do **not** fit that limit. This check
neither raises the limit nor establishes five-Pack native acceptance.

The caller's WIT is one independently resolvable document: its original
731-byte source followed by a reversible nested-package encoding of the exact
canonical bridge WIT. The resulting 1,099-byte document has SHA-256
`687f47f6c592ee1bc195d481d02efd0c051f664729e965cc0271850735ffaa04`.
No operation, nominal Core type, permission, artifact or original source pin
changes in this packaging transformation.

An earlier split-document layout preserved source bytes but failed production
WIT inspection: `interface or world caller does not exist` at the world's
`export caller;`. A production-parser regression retains rejection of that
layout. Successful source reconstruction alone is not parser acceptance.

## Static validation, including on Linux

Set these variables to the fresh materialized graph and its returned index
hash, not to the development-composition directory:

```sh
export TOBKIRI_TEST_GATEWAY_TOOL_CALLER_GRAPH_DIR=/absolute/path/to/graph
export TOBKIRI_TEST_GATEWAY_TOOL_CALLER_INDEX_SHA256=0441bde565b5e1765a8fdc262e61db00a5fff0c160624d99d5d841f65c737d96
cargo test --offline --locked -p tobkiri-e2e --test ordered_python \
  evidence_gateway_tool_caller::current_caller_graph_has_exact_static_abi \
  -- --ignored --exact --nocapture
```

The fixed reviewed graph has caller Pack digest
`a8c507beb99d21e6f776121c899c9407b3aa0f7673c16376c4d1e1867ba69114`
and bridge Pack digest
`7ff540650f7677c8530ef6f5cdba8d1e2fe436a57f99133118e9d377c467cb31`.
The loader checks the complete member closure, provenance, production WIT
resolution, actual component types, and Profile compilation. It does not open
Core, create a Session, instantiate a component, or execute a guest. Its result
must not be reported as native runtime acceptance.

## Native Core/Gate replay

Run only on the existing supported owner platform: arm64 macOS, Darwin 24.6.0,
with a local APFS physical root. The existing runtime platform checks remain
authoritative. Set a fresh absolute evidence path outside the checkout:

```sh
export TOBKIRI_TEST_GATEWAY_TOOL_CALLER_EVIDENCE=/absolute/path/outside/checkout/native-caller.json
cargo test --offline --locked -p tobkiri-e2e --test ordered_python \
  gateway_tool_caller::original_python_tool_caller_replays_through_real_core \
  -- --ignored --exact --nocapture
```

This route requires both actual guest runtime identities, all 30 current source
cases, authenticated user and imported-provider terminal receipts, and ordered
dependency dispatches. It also checks that removing only the caller's bridge
permission returns denial before provider entry, and that requesting Session
shutdown rejects a subsequent call before any dispatch. Every created Session
is closed and close errors are retained alongside operation errors.

The shutdown check is **pre-entry admission**, not deterministic in-flight guest
cancellation. The latter remains unverified. Invalid/nonfinite typed transport
results are not full legacy HTTP-error parity. Neither a passing check nor its
receipt establishes production enablement, full Gateway migration, live model
generation, retry/billing parity, all typed error arms, or complete Host
retirement. No native result is supplied by this document.

A separately archived [2026-10-21 native trial](trials/2026-10-21-native-caller/)
records this route on its exact source tree, with all 30 cases matched. Its
execution patch, receipt index and explicit test-Host scope are part of that
result; later code and the full Launcher workflow require their own validation.
