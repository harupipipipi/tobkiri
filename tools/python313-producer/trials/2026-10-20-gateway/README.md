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
