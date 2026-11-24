# Harness capability projection

This App Pack candidate runs the original catalog-to-target calculation from
`defaultspack_dynamic_capability_targets`. Its ordinary typed `project` port
accepts a bounded binding/catalog DATA envelope. The result preserves every
target field and the original input-schema bytes, encoded as UTF-8 text.

Catalog `approved`/`enabled` flags, target IDs, claimed artifact digests and the
returned result confer no authority. The eventual caller must use the real
Profile/port/artifact/custody/Gate path. Legacy HTTP records are source reuse
inside this App calculation; they are not a new Rust Core or Host vocabulary
and do not preserve HTTP as the product runtime transport.

`build_components.py --capability-projection` builds this ordinary Pack and a
separate resource-free Native DATA comparison wrapper over the same adapter
and original source bytes. Both use the existing authenticated producer,
locked schema dependencies, genuine Wasm rpds extension, and existing source
and artifact bounds. No dependencies are added to older build scopes.

The Native comparison uses the existing runner and its unchanged 64 MiB,
2-billion-fuel and original absolute 30-second limits. The pre-change bf8
factory supplies expected results; invalid envelope cases are separate
boundary tests. This comparison proves calculation behavior only. It does not
prove an OS owner, actual Profile, ordinary Pack invocation, GUI approval or
full product parity. The ordinary Profile/Flow connection remains required.

The input validator's unchanged body and reserved-key constant now live in
`_public_view_input.py`. The old UI module reexports the same function object
and still reads/validates the UI schema. The projection's import no longer
reads that UI schema: this is an explicit import-side-effect change, not a
claim of arbitrary monkeypatch/reflection equivalence. In-repository reference
review found no dependency on the old validator globals; external reflection
remains unverified.

There is no invented artifact digest, signature or installable manifest here.
Actual CI outputs must be bound through the ordinary Pack admission pipeline.
The existing Launcher Setup, GUI approval, settings and conversation flow are
unchanged by this candidate.
