# Verified Python 3.13 asset packager

This standalone tool packages the 13 real assets consumed by componentize-py's
publishing build script. It never builds CPython, invokes Cargo, fetches source,
or runs the normal componentize-py build script. It does not generate runtime
stubs and is not itself a component producer.

## Architecture and source provenance

- `package.py` uses Python's standard library to capture SHA-256-verified input
  files, construct deterministic tar data, and launch one hash-pinned local
  compressor worker. The driver **does execute that worker as a subprocess**,
  using a fixed argument vector, no shell, bounded pipe reads, timeout and
  process-group cancellation.
- The Rust worker has no subprocess or network code. It reads bounded stdin,
  validates Wasm before and after stripping, and emits zstd at level 19.
- `src/upstream_strip.rs` preserves the exact reviewed strip function body from
  componentize-py commit `c0949b19d464f5d70bc1051195a3ae0e6a012df9`. Only Rust
  visibility and module imports were adapted. Upstream attribution and its
  Apache 2.0/LLVM exception license are retained in source and `LICENSE`.
- Cargo dependencies are exactly anyhow 1.0.102, zstd 0.13.3, wasmparser 0.245.1
  and wasm-encoder 0.245.1 from crates.io. The normalized 32-package registry
  closure retains exact upstream identities and checksums, with dependency edges
  restricted to an upstream subset. Original 51-record provenance evidence is
  retained separately from the normalized records. No Git dependency,
  runtime fork, top-level producer build script, or new package source is used.
  Tar creation is Python stdlib, so no Rust tar dependency is needed.

Check derivation without compiling or executing upstream code:

```
python verify_derivation.py --upstream-lock SOURCE/componentize-py/Cargo.lock \
  --upstream-build SOURCE/componentize-py/build.rs
python -m unittest discover -s tests -v
```

The upstream build.rs argument must be the reviewed Python 3.13-patched version.
The verifier checks source hashes, exact strip body, direct dependency pins and
all derived registry records, the exact manifest and local root package, and
unique/resolvable dependency edges with no unreachable lock entries. The normalized registry-only helper has passed a frozen Rust 1.94.0 build:
four Rust unit tests and one actual-worker zstd round-trip/strip-whitelist test.
The 31 Python tests mock worker transport and are not themselves evidence of
Wasm or zstd correctness. Synthetic Wasm tests do not establish correctness of
the complete 13-asset producer bundle, which requires real runtime artifacts.

The actual built CPython shared library also passed bounded worker stripping and
compression, independent zstd decompression, and Wasmtime 48 `Module.validate`
on both original and stripped bytes. All non-custom sections (including imports
and exports), `dylink.0`, and the retained custom-section whitelist matched
exactly. `validation-receipt.json` records source, lock, worker, input and output
hashes. No Wasm instantiation or guest execution was performed.

## Input contract

Use a trusted local input directory with no concurrent replacement. Capture each
input from its verified build/source receipt. The caller supplies a JSON manifest
and its SHA-256, plus the compiled worker's path and SHA-256. Hashes establish
identity, not a guarantee that an arbitrary binary or runtime is trustworthy.
All path components must be regular directories, not symlinks.

Manifest format (one example record, not a complete runnable manifest):

```
{
  "schema_version": 1,
  "files": [{
    "asset": "python-lib.tar.zst",
    "member": "os.py",
    "path": "stdlib/os.py",
    "sha256": "SHA256_OF_THE_FILE",
    "mode": 420
  }]
}
```

Every included file in each tar must have its own record. `path` is relative to
the trusted input root. `member` is the tar-relative name, or null for a direct
binary asset. Modes are decimal 420 (0644) or 493 (0755). Duplicate files,
duplicate members, directory/file collisions, unsafe paths, symlinks and
nonregular inputs are rejected. Missing any asset is an error.

Direct binary assets and their intended inputs:

- `libcomponentize_py_runtime_sync.so.zst`: actual sync runtime Wasm.
- `libcomponentize_py_runtime_async.so.zst`: actual async runtime Wasm.
- `libpython3.13.so.zst`: built CPython WASI shared library.
- `libc.so.zst` and `libwasi-emulated-{mman,process-clocks,getpid,signal}.so.zst`:
  matching SDK's wasm32-wasip2 shared libraries.
- `libc++.so.zst` and `libc++abi.so.zst`: SDK wasm32-wasip2/noeh shared libraries.
- `wasi_snapshot_preview1.reactor.wasm.zst`: the pinned upstream adapter.

The two tar assets are `python-lib.tar.zst` (installed Python 3.13 stdlib) and
`bundled.tar.zst` (componentize-py bundled Python sources).

Sync and async inputs must be nonempty and byte-distinct. Their final compressed
outputs must also be distinct after stripping; equality fails before publication. The Rust worker rejects
invalid Wasm and modules with no function bodies. Such structural checks cannot
prove a runtime is correct or rule out all semantic stubs: only use actual build
outputs with reviewed provenance, and perform runtime smoke tests afterward.

## Packaging semantics and reproducibility delta

The upstream inclusion predicate is preserved: exclude extensions `.a`, `.pyc`,
`.whl` and filenames `Makefile`, `Changelog`, `NEWS.txt`. Excluded entries in the
input manifest fail rather than being silently ignored. All other included bytes
are preserved.

Intentional difference from upstream filesystem traversal: tar members are
sorted by member name; uid/gid/mtime are zero, owner/group names empty, modes
0644/0755, and only regular files are written. Python PAX tar encoding is used.
This is a documented reproducibility choice, **not byte-identical reproduction
of upstream tar headers or traversal order**.

All `.so` inputs use upstream's recursive strip algorithm, preserving custom
sections `name`, `component-type:*` and `dylink.0`. The reactor adapter is
validated and compressed without stripping. Every output uses zstd level 19.

Limits: input file 64 MiB; all input files together 256 MiB; manifest 4 MiB;
10,000 records; tar/worker input and compressed output 128 MiB per asset; stderr
8 KiB; combined compressed assets 256 MiB; worker timeout 240 seconds per asset. Output is capped while reading, not
only after process completion. Any transport failure kills the worker process
group. No output path is overwritten.

## Running after compiling and verifying the worker

```
python package.py --input-root INPUTS --manifest inputs.json \
  --manifest-sha256 MANIFEST_SHA256 \
  --worker /absolute/path/to/tobkiri-python313-packager \
  --worker-sha256 WORKER_SHA256 --destination NEW_OUTPUT_DIRECTORY
```

The destination's existing parent must be trusted local storage. Inputs are
captured once before packaging. Output is prepared privately and atomically
renamed only after every asset succeeds. The receipt records input-manifest,
worker, worker-input and output hashes. Failed partial directories are retained
with `FAILED.txt` and an explicitly failed receipt, not reported as success.

A successful packaging receipt does not attest runtime compatibility, sandbox
policy enforcement, or successful component production.

Cargo normalizes the upstream workspace feature-unified lock to the standalone
helper closure: 19 unused registry nodes and eight optional edges are removed.
Package names, versions, sources and checksums remain exact upstream identities;
no edge is added. The verifier pins normalized records, retains original upstream
records, checks resolved edge subsets, and requires an exact reachable graph.
