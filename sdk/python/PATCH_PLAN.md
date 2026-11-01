# Pinned producer source patch

Versioned source patches now exist in
[`tools/python-producer`](../../tools/python-producer/README.md), with original
source pins, exact before/after deltas, a strict patch checker and added source
overlays. They are uncompiled and are not an accepted compiler/runtime patch.
The adviser approved modifying the original componentize-py 0.25.0
producer before CPython starts. A second initializer, post-build component
editing, native Python execution fallback and host ABI exceptions are excluded.

The patch belongs in the original producer's `ComponentGenerator` preinit path
and its explicit CLI/API input. It must consume a canonical declared build-plan
digest and an explicit SOURCE_DATE_EPOCH. The public build seed is the SHA-256
of a versioned domain separator and that digest. `deterministic_plan.py`
prepares those declaration-only values; it does not enforce initialization.

The original Wasmtime-WASI 46.0.1 builder already exposes `secure_random`,
`insecure_random`, `insecure_random_seed`, `wall_clock` and `monotonic_clock`.
The patch supplies deterministic build-only providers through these APIs,
sets PYTHONHASHSEED=0 before CPython starts, and verifies the resulting Python
flags. Wall time uses the declared epoch; monotonic time starts at zero and
advances by a defined 1000 ns per observation. Guest locale/TZ/environment are
explicit, bytecode writes are disabled, and TCP/UDP/name lookup are disabled.

Only captured declared source, bindings, pinned standard-library and helper
trees are mounted at stable guest paths. No HOME, inherited environment,
venv/pipenv discovery or undeclared filesystem access is permitted. Mounts are
read-only. File order, paths, sizes, modes and timestamps are normalized, and
filesystem stat/metadata hashes use declaration-derived identities rather
than host inode/device identity. The original preinit filesystem bindings must
enforce this without changing nominal resource identity. Hash-map output
ordering and preinit scheduling must also be deterministic.

The public seed is never a runtime cryptographic source. The original CPython
WASI entropy and `_random` primitives now have source guards, retained implicit
RNG tracking, a seal audit and a private activation callback boundary. Original
runtime begin/flag-check/seal hooks surround its original initialization. Public
preinit entropy/RNG outputs, explicit seeds and restored unknown state are named
unsupported. Stateless SystemRandom has source provenance support; custom RNG
extensions/caches remain unavailable. No Python monkeypatch is a safety proof.

The private Core owner/epoch/random.bytes bridge is not installed. Public entry,
parameter lifting and callback source guards therefore reject DORMANT instances.
Typed immutable filesystem capture and nominal WASI46 filesystem adapters now
have reviewable source, but remain uncompiled and unproved. FS-01 rejects before
preinit instead of admitting host metadata. IO pollable accounting/lifetime and
the full deny/perturbation matrix remain open. The snapshot
still needs an actual retained RNG/cache audit and live Core reseed/provenance
proof; merely adding a WASI import is insufficient. See the
[open source gap ledger](../../tools/python-producer/GAPS.md).

Pin original source, patch, CPython, bundled runtime, dependency lock, toolchain,
flags, WAC, declared inputs and output hashes. Keep compiler Wasmtime 46.0.1 and
Python validation Wasmtime 48.0.0 separate from the host's pinned version. The
existing SDK34 reuse is approved under recipe revision 2; actual SDK tree,
sysroot and selected tool hashes are recorded in sdk34-pins.json. Native OS
closure, full dependency/source closure and exact compiler flags remain pending. No second Cargo target or
SDK copy is proposed.

The original build script has no supported prebuilt-runtime bundle option and
creates separate nested sync/async Cargo targets. That path is now rejected in
the source patch before SDK discovery/build. Reusing the outer shared target
recursively would deadlock. Sequential original-producer staging is approved in principle. The new verifier
and packaging sources are unconnected prototypes with a pending recipe; BUILD-02
still rejects. Actual stage receipts and complete admission are required.
The build constructs native/WASI CPython and runtime libraries plus the compiler.
A conservative, unmeasured additional disk budget is 8–12 GiB; the required
free capacity is therefore 13–17 GiB including the 5 GiB floor. This estimate
must be refined by bounded measurements after the parent releases the build
hold. No build, dependency download or binary extraction starts under that hold.

Acceptance requires two independent clean environments and repeated fresh
processes producing identical inner and outer bytes, with perturbations of
host paths, environment, wall time, entropy and directory order. Undeclared
filesystem/network requests must fail. Replay the unchanged 91 legacy cases
through Rust Core, retain the typed alias/resource proof, verify exact outer ABI
closure/private-init inaccessibility, and prove runtime randomness is not the
baked public seed. Until then the earlier differing builds remain evidence and
all migration inventory entries stay `todo`.
