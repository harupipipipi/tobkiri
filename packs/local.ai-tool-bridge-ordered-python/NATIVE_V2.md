# Versioned bridge: static inspection and native acceptance

This is an opt-in test harness, not a replacement for the original Gateway.
The v1 behavior and consumer test route remain unchanged. Its component and
source-proof pins were refreshed on 2026-10-20 after the reviewed Gateway helper
extraction changed provenance, not any of the 33 source case observations.
The v2 route uses
Pack version `2.0.0`, shape `local.ai-tool-bridge-ordered-python.bridge@2.0`,
and WIT interface `local:ai-tool-bridge-ordered-python/bridge@2.0.0`.

## Required immutable inputs

Materialize the exact refreshed v2 component with
`scripts/wasm/materialize_ordered_tool_bridge_v2.py`. The current provenance
change is recorded in
`tools/python313-producer/trials/2026-10-20-finalization/source-rebinding.json`.
Both rebuilt bridges passed fresh isolated 33-case development replays before
their native-harness identity pins were updated. Inactive materialization
authenticates bytes only and does not establish native Core acceptance.

The earlier build, replay, and materialization in
`tools/python313-producer/trials/2026-10-20/` and the passing checkpoint
`92f38e3241b802605a4af59ba35aae72ee73cdbd` are preserved historical evidence,
not current source proof. See `INTEGER_V2.md` for the protocol. Set these
independent pins for the refreshed component when invoking the Rust tests:

```sh
export TOBKIRI_TEST_ORDERED_PYTHON_V2_PACK_DIR=/absolute/path/to/inactive-v2-pack
export TOBKIRI_TEST_ORDERED_PYTHON_V2_PACK_SHA256=7ff540650f7677c8530ef6f5cdba8d1e2fe436a57f99133118e9d377c467cb31
export TOBKIRI_TEST_ORDERED_PYTHON_V2_COMPONENT_SHA256=30103a20b3694622a62e50cd57cb52af791cf0936657889811562db06ad3e092
export TOBKIRI_TEST_ORDERED_PYTHON_V2_RECEIPT_SHA256=01f8d3b20b5bcd46d16d1fae1265b16155237fef3ce2163c0cfa47e32c23a14d
```

The harness independently checks the canonical manifest, exact members,
component and receipt bytes, nineteen source bindings, twelve staged files,
original inventory/provenance, and all frozen case identities and inputs. A
matching receipt alone is insufficient. Actual compiled component types must
have exactly the reviewed imported/exported interface-name sets, exported
function signatures, and exported Core error alias type identities. Core import
function signatures are validated by Core at runtime. No guest is instantiated by static inspection.

```sh
cargo test --offline -p tobkiri-e2e --test ordered_python \
  evidence_v2::ordered_python_v2_pinned_pack_has_reviewed_static_abi \
  -- --ignored --exact --nocapture
```

This portable check cannot establish that Core can start or execute the Pack.

## Real Core test, not yet accepted

Run only on the existing supported owner platform: arm64 macOS, Darwin 24.6.0,
local APFS. Production allocation-owner checks are unchanged. Linux helper
success is not a substitute. Provide a fresh absolute evidence filename whose
existing parent is outside the checkout:

```sh
export TOBKIRI_TEST_ORDERED_PYTHON_V2_EVIDENCE=/absolute/external/path/native-v2.json
cargo test --offline -p tobkiri-e2e --test ordered_python \
  v2::ordered_python_v2_bridge_replays_through_real_core \
  -- --ignored --exact --nocapture
```

The test uses the real Core, Wasm execution factory, Gate, verified Pack pins,
and isolated Profiles. It requires:

- Actual guest runtime-info: CPython 3.13.16 and Unicode 15.1.0.
- All 33 direct cases: eighteen byte-exact successes, twelve fixed typed
  INVALID outcomes, and three fixed INTERNAL nonfinite boundaries. The latter
  remain excluded from semantic parity.
- Exact successful physical Pack Init and per-call dispatch/terminal audit
  pairs with arguments/effect digests and request/grant matching. Direct calls
  have User authority; the authenticated Init and frozen binding identify the
  physical provider. Nested pairs identify the typed consumer Pack/hash.
- A real typed consumer for runtime-info and representative ordinary, bigint,
  depth, invalid, and nonfinite cases. Nested errors retain Core's existing
  `provider-error` custody rather than inventing direct-call error identity.
- Separate denied consumer sessions with the effective port grant removed,
  actual Gate denials, and no dispatch to the provider.
- Awaited public Session close after acquired sessions, including Result error
  paths, with original and cleanup errors retained separately.

A fresh diagnostic records preflight/runtime failures after output-path
validation. Public Session cleanup is not a claim that Core invoked the guest
lifecycle shutdown export. The development replay separately executes that
export explicitly.

Even a future passing native receipt will not prove original Gateway caller
replacement, no-retry/billing behavior in that caller, full Host parity, or
production activation. Those remain separate migration gates. No legacy HTTP
error-byte parity is claimed by the intentional typed boundary.
