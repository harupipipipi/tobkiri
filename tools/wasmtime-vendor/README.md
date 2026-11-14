# Wasmtime vendor provenance

This inventory covers the vendor tree from draft #1521 at commit
`8be3110a333a80b8f250c80c05c3ebe1aa34dbb8`. It can be imported into `soon`
before the Rust workspace: this tree does not include Cargo's
`[patch.crates-io]` wiring and does not change the active runtime.

Both packages are published crates.io version 47.0.4, with upstream VCS
revision `254cb7a02d5804d4e466198212cda9d3575ad381`. The two upstream licenses
are preserved byte for byte. Local source changes span the original vendor
import (`ec8a0688f`), component delivery changes (`7f79668d8`), and growth
accounting changes (`f2de7d1bb`). Use the refreshed inventory below to review
the final tree; the earlier allocation review records a historical snapshot.

| Package | Unchanged | Changed | Added | Removed |
| --- | ---: | ---: | ---: | ---: |
| `wasmtime` | 230 | 20 | 3 | 0 |
| `wasmtime-internal-fiber` | 20 | 2 | 1 | 0 |

`vendor/.gitignore` is listed separately. The [inventory](source-inventory.json)
records each delta path with size, SHA-256 and Git mode, plus digests of the
complete inventories, including unchanged files. The inventory digest is the
SHA-256 of the path map as UTF-8 JSON with sorted keys and compact separators.

Download the
[`wasmtime` archive](https://static.crates.io/crates/wasmtime/wasmtime-47.0.4.crate)
and [`wasmtime-internal-fiber` archive](https://static.crates.io/crates/wasmtime-internal-fiber/wasmtime-internal-fiber-47.0.4.crate)
into one directory with their original filenames. From the repo root, run:

```sh
python3 -B tools/wasmtime-vendor/verify.py /path/to/archives
```

The checker reads Git blobs from `HEAD` by default; use `--ref <commit>` to
verify another commit. It checks archive digests before reading any members,
does not extract the archives, and does not modify the repository. This
verifies source provenance only. The follow-up that wires the patched crates
into Cargo needs its own locked build and runtime tests.
