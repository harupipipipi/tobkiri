# Original Gateway helper component build trial

This separate development component preserves the two original Gateway tool
normalization helper bodies. It is not the original full Gateway, a default
Profile change, or a Host/HTTP replacement. See
`packs/local.ai-gateway-tool-caller-python/README.md` for the bounded source
surface and intentional malformed-success boundary.

The reviewed ten-file stage was built using the same preserved, specifically
approved custom CPython 3.13 producer and official WAC 0.12 tool as the preceding
bridge trial. No interpreter, compiler dependency, or isolation setting changed.
`componentize-trial.py` differs from that reviewed driver only in its exact build
plan hash. The fixed empty dependency environment contains no Host package or
reference oracle. The build's pre-initialization step runs source code; this is
not an observation of the exported guest runtime-info operation.

The system-Python isolation probe passed before custom producer execution:
separate network/PID namespaces, no routes or home/config mounts, read-only
inputs, private writable output, and the unchanged resource guards. All 1,537
mounted input records remained unchanged after the build. The build finished in
30.43 seconds with sampled peak RSS 418,603,008 bytes and no known surviving
processes. Sampled RSS/process guards are not hard resource caps.

`component.build.json` and `build-trial.json` bind the actual 17,878,415-byte
component, source manifest, plan, driver, tools, and supervision evidence. The
component SHA-256 is
`1a438458ba804cf9b62c7125602b1499f159263387f56f2470c51ce4191e521c`.
Typed target validation passed. Its six imports are the five canonical Core
interfaces plus the required real bridge@2 dependency; its two exports are
canonical lifecycle and its own caller interface. No Host, HTTP, WASI, local
normalizer fallback, or private compiler export remains in the outer surface.

At this build checkpoint, exported guest calls have not been replayed. Real
composition with the preserved bridge, observed runtime identity from both
guests, native Core/Gate acceptance, and production Gateway routing are separate
pending gates. The raw component and exact receipt are preserved separately;
this directory records source and evidence, not a production binary default.


## Composed replay diagnostic checkpoint

The original caller and bridge components now compose directly, with exactly the
five canonical Core imports (all trapping), both public interfaces, and separate
provider/caller lifecycles. Composition SHA-256 is
`2530b6a090f6584cfc6ffeef131b070af429c0b120381be463c767f47bb2fe67`.
The frozen source reference contains 30 cases; its observations are not guest
execution, import counts, retry proof, or full Host parity.

The first two attempts stopped at an overly broad expected type surface. Exact
introspection and bounded input-section inspection show that both pinned input
components already omit unused `risk`, `pending`, and `verdict` declarations.
Their consumers, approvals and gate-mediator, are not imported. The matcher now
requires exactly the five referenced types and every nested function signature;
it does not accept arbitrary subsets or change canonical WIT.

Attempt 3 passed that exact ABI check but could not reserve the default roughly
4 GiB virtual address range for each Python memory within the unchanged 8 GiB
process cap. The development runner now uses Wasmtime 48's supported 128 MiB
initial reservation, matching the existing per-memory Store limit. This changes
neither guards nor growth/trap settings nor OS/Store resource caps. The separately
supervised synthetic probe verifies two instances, exact out-of-bounds traps, and
rejected growth beyond the Store cap. See the [Wasmtime 48 configuration
reference](https://docs.rs/wasmtime/48.0.0/wasmtime/struct.Config.html#method.memory_reservation).

Attempt 4 passes ABI and memory reservation but reaches the original single-
component core-instance cap at instance 65. All 30 cases fail during instantiation;
no explicit guest export was attempted or returned. Fuel consumed during failed
instantiation is retained. Actual caller/provider replay is therefore **not yet
successful** at this checkpoint. The failed receipts, input identities and cleanup
results are retained rather than replaced with successful-looking evidence.
Native Core/Gate acceptance and production/default Profile routing remain false.
