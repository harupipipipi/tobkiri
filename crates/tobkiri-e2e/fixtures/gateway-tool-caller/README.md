# Preserved caller and bridge components

These are the exact two separately built, reviewed custom Python components
from the 2026-10-20 finalization trial. They are inactive native-test inputs,
not a composed component, expected-output stub, producer, or production Profile.
The raw originals remain unchanged outside this fixture.

Each `component.wasm.xz` is one deterministic XZ stream produced with Python
`lzma.compress`, `FORMAT_XZ`, `CHECK_CRC64`, and preset 6. The format contains no
timestamp or filename. Its decompression uses a 128 MiB dictionary-memory cap
and the exact pinned output length plus one byte, rejecting truncation,
concatenated streams, trailing bytes, wrong lengths and wrong hashes.
No component is executed during compression or staging.

## Exact transport identities

Bridge (`local.ai-tool-bridge-ordered-python`, version 2.0.0):

- XZ: 3,957,460 bytes, SHA-256
  `f2ae05cd09161fb8ad65c5fcf4f168852b7ea33895ec32578244aa47c6db236f`.
- Component: 17,910,172 bytes, SHA-256
  `30103a20b3694622a62e50cd57cb52af791cf0936657889811562db06ad3e092`.
- Build receipt: 8,535 bytes, SHA-256
  `01f8d3b20b5bcd46d16d1fae1265b16155237fef3ce2163c0cfa47e32c23a14d`.

Caller (`local.ai-gateway-tool-caller-python`, version 1.0.0):

- XZ: 3,937,708 bytes, SHA-256
  `8f6b6769d7fe0e05a17288dc38b1e7e648df11b3c889e4441192cd82d79dc3ea`.
- Component: 17,888,679 bytes, SHA-256
  `cb561cb9b9b85aaf1de9cb74a57ffece061bd4b8100d6030b5aabc79b2cb7fc1`.
- Build receipt: 8,451 bytes, SHA-256
  `18d900c56e25e2a5ab9d746a00f06066ebbae32cba8f0a681e6009826224f477`.

The exact receipts and actual build/replay provenance are preserved under
`tools/python313-producer/trials/2026-10-20-finalization/`. The custom producer's
original source and tool provenance remains under the earlier trial directories.
The current 30-case frozen caller observations have SHA-256
`854fa03de651c12cb7126d8f81e4f0c62e340a5a85f71eb50784e539e207bced`.
Those observations are validated, not regenerated, during CI preparation.

## Fail-closed preparation

Run `python3 -B scripts/wasm/stage_preserved_gateway_tool_caller.py --output`
with a fresh destination under an existing trusted parent. No download,
producer build, exact-Python source-oracle run, guest execution or activation
is performed. Python's standard library is sufficient.

The script rejects symlinks, hardlinks, FIFOs and other nonregular evidence;
authenticates both compressed inputs and receipts; decompresses bounded exact
bytes; and invokes the existing two-Pack materializer. That materializer
reconstructs every current source plan and rechecks the frozen source bindings.
Both temporary stages are private, and only a fully validated output is
published atomically without replacement. Failures leave no success record or
partial output. Unsupported atomic publication fails closed.

The output contains only `graph/` and `materialization.json`. The graph holds
two separately hash-bound inactive Pack directories and the original frozen
observations. Its index SHA-256 is
`0441bde565b5e1765a8fdc262e61db00a5fff0c160624d99d5d841f65c737d96`.
The caller's complete original WIT and reversibly nested canonical bridge WIT
form one reviewed 1,099-byte document, avoiding the invalid historical
split-interface/world packaging. Original component, receipt and source hashes
are unchanged. The old failed graph remains separate diagnostic evidence.

Combined raw components total 35,798,851 bytes, below the unchanged 64 MiB
catalog bound. Caller permissions allow only the reviewed bridge port.
The five-component finalizer graph is not staged here.

`materialization.json` explicitly records `build_performed`,
`wasm_execution_performed`, `native_core_acceptance`, and `production_enabled`
as false. A later independent native Core/Gate test must authenticate the same
graph and establish its own actual-runtime result. Preparation does not prove
native acceptance, production readiness, live generation, retries or billing.
