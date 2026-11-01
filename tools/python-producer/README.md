> Optional producer hardening/reproducibility prototype. The normal Python Pack migration uses the pinned official wheel path in `sdk/python/build_component.py`, subject to actual Core execution and compatibility verification. Incomplete custom-producer staging is not a product execution gate. See [active resource boundaries](../../docs/migration/1520/resource-boundaries.md).

# Original Python producer source patch

This directory contains a reviewable, **uncompiled** source patch for the original
componentize-py 0.25.0 initializer and its original CPython WASI source. It does
not replace an installed wheel, initialize a second Python instance, edit a
component after compilation, relax the Core ABI, or change the frozen oracle.

`patches/componentize-py-0001.patch` changes the original `ComponentGenerator`,
CLI/PyO3 entry points, original runtime and build-script entry. The declaration
is explicit and canonical; its public domain-separated seed, clocks and guest
flags are set before CPython starts. Readonly mount permissions, source ordering,
interface naming order and one-thread scheduling are selected in that original
path. The old runtime's ambient environment/argv refresh and single cached
`random.seed()` workaround are removed from the patched Pack runtime.

`patches/cpython-0001.patch` guards original `pyurandom` (both `_PyOS_URandom`
variants and `os.urandom`) and `_random` primitives under `__wasi__`. It removes
WASI time/PID fallback, tracks implicit RNG objects, audits their provenance at
seal, and reseeds them through a private live callback during activation. The
original stdlib `SystemRandom` marks its C state stateless; base C state methods
cannot use that tag. Native CPython entropy behavior is unchanged. This is a
source modification, not a Python runtime monkeypatch.

The guard starts in `NEW`, admits build-only implicit stdlib seeds, then seals
`DORMANT`. Public RNG outputs/entropy caches and explicit seeds during preinit
are unavailable. Runtime activation is private, owner/epoch bound and requires
Gate-backed Core callbacks. There is **no installed Core bridge**, so public
parameter lifting, export calls and async callbacks fail before invoking Python.
Partial activation invalidates the instance; it cannot return to build mode.

There are also deliberate closed gates before initialization and before compiler
packaging. Readonly files alone do not normalize host metadata or prove immutable
input admission. Original nested Cargo runtime builds allocate two more targets;
using the outer target recursively would deadlock. Until these gaps are resolved,
this source patch cannot emit a reviewed Pack component. See [GAPS.md](GAPS.md).

## Review and bounded checks

`pins.json` names immutable upstream commits and selected file hashes. Every one
of the 12 original files was verified against its immutable primary-source URL.
`source-delta.json` records actual original and generated-source hashes and
sizes; `overlay/` contains added source files. `make_patch.py` regenerates exact
diffs from those original bytes. It rejects mismatched or nonunique anchors.
`apply_patch.py` independently applies the versioned unified diffs with exact
context/count checks and validates the resulting hashes. It only writes to a
fresh partial review directory; it never patches installed sources in place.

```sh
python3 -B tools/python-producer/make_patch.py --source <source-root>
python3 -B tools/python-producer/apply_patch.py --source <source-root>
python3 -B -m unittest discover -s tools/python-producer/tests -v
python3 -B -m unittest discover -s sdk/python/tests -v
```

The source root has `componentize-py/` at commit
`c0949b19d464f5d70bc1051195a3ae0e6a012df9` and `cpython/` at commit
`0e13686da8bb881b059d35e23c32bcd2e6440099`. No download, Cargo build or extraction
occurs in these commands. A supplied full checkout is not automatically admitted:
the full dependency lock, CPython tree and toolchain pins remain incomplete.

Observed checks: 8 patch-checker tests, exact application of 16 files, Rust source
syntax parsing for 9 files, one stdlib Python source parse, and the existing 27
native SDK checks. No C source was compiled; no guard, Python wasm guest, Core
activation, SQL backend, reproducibility matrix or 91-case replay was executed.
The earlier unequal component hashes and historical runtime receipts remain
unchanged. These observations describe this optional patched-source prototype.
The separate pinned official-wheel route has since passed all original 91 cases
through Rust Core; see the [current runtime receipt](../../crates/tobkiri-e2e/evidence/python-current-parity-20261015.json).
The migration inventory has 836 unresolved units and one separately evidenced
global canonical mechanism; this prototype remains unbuilt.

## Capacity

Source-only tooling occupies about 120 KiB; selected original and partial review
captures total less than 4 MiB. The latest disk observation was 6.10 GiB free,
leaving about 1.10 GiB above the 5 GiB floor. No compiler target was allocated.

The original build script constructs native CPython, WASI CPython plus zlib and
SQLite, two Rust runtime feature sets, the Rust compiler and compressed bundles.
Its nested targets are now rejected before SDK discovery or CPython build.
Existing SDK34 sharing is **not yet approved**. SDK33 is the upstream default;
no additional SDK download/copy is authorized or proposed under the hold.

A conservative unmeasured additional budget remains **8–12 GiB**, hence **13–17
GiB free including the floor**, after a sequential shared-target staging design
is approved. Planning bands are 1.5–3 GiB for source/native/WASI CPython and C
libraries, 4–6 GiB for compiler/runtime Rust intermediates (46.0.1 is distinct
from host 47), and 2.5–3 GiB for linker/temp/proof-environment headroom. These are
capacity reservations, not measured build costs. Existing SDK/tool bytes are
excluded. The bands may be reduced only by bounded measurements after the parent
releases the hold; no second target or native fallback is a proposed shortcut.

Checkpoint 002 adds source-only captured filesystem/guard/staging preparation and actual read/hash SDK34 pins. The original packaging path still rejects BUILD-02, input admission rejects FS-01, and private activation is unconnected (ACT-01). See GAPS.md and crates/tobkiri-e2e/evidence/python-source-preparation-002.json; no compiled/reproducible/legacy-parity claim follows from these files.
