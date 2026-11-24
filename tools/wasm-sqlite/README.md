# Pinned private SQLite producer

This is an optional experiment. Normal DATA uses the native Rust Store backend.
To verify and embed a producer result, explicitly enable the Wasm crate's
`private-sqlite-experiment` feature and set `TOBKIRI_SQL_PRODUCER_DIR`. The
feature retains all provenance checks below; default builds do not require or
embed an unused experiment artifact.

This producer builds only an internal core Wasm module. It does not establish a
safe SQL runtime, exclusive DATA DB handling, WAL recovery or aggregate memory
coverage. See the pinned receipt and the Wasm crate capacity checkpoint.

The currently executed producer is SDK34 arm64 macOS. `build.py` rejects any
compiler, linker or wasip1 libc whose exact digest differs from its pins; it does
not download tools. Its disk monitor applies only to its own process group.

The official SDK34 Linux archive was read to EOF and hashed without saving,
extracting or executing the SDK. Its `wasi-sdk-34.0-x86_64-linux.tar.gz` SHA256 is
`b761e3a0721dbae9c09a0059e5fdb2bf917d1b4a8a7b430fb3b5aafb0984b2c4`.
Primary asset metadata: <https://github.com/WebAssembly/wasi-sdk/releases/expanded_assets/wasi-sdk-34>.
`host-toolchain-sdk34-x86_64-linux.json` records the exact archive, resolved
compiler/linker links, native SDK libraries and sysroot inventory. These hashes
establish tool provenance, not successful Linux compilation or module equality.

A Linux x86_64 CI stage first downloads and checksum-verifies that exact official
archive and extracts it in a private directory. The source-only recipe is:

```sh
python3 tools/wasm-sqlite/build-linux.py \
  --archive "$RUNNER_TEMP/sdk34.tar.gz" \
  --sdk "$RUNNER_TEMP/sdk34/wasi-sdk-34.0-x86_64-linux" \
  --out "$RUNNER_TEMP/sqlite-core" \
  --disk-floor-bytes 5905580032
export TOBKIRI_SQL_PRODUCER_DIR="$RUNNER_TEMP/sqlite-core"
```

`build-linux.py` verifies its own source pin, the complete official archive and
every extracted SDK object before executing tools. Extra files, modified files,
escaping links or different compiler/linker bytes fail. It calls the unchanged
`build.py` source/flags/ABI with only the exact Linux host-tool hashes selected,
and requires the existing 1,073,130-byte module digest
`d52e4d9ec0ee1d6bee0cb26918d907d96722442950787a5c45656d674e3ccc02`.
The original macOS producer and receipt remain valid. A Linux receipt adds a
closed host-profile identity; it cannot replace the approved module hash.

Linux compiler execution and cross-host module equality remain **unexecuted**.
The recipe must fail if CI produces different bytes; it cannot refresh the pin.
No local SDK download, extraction or Linux compiler execution occurred. The
recipe never downloads or extracts tools, and Cargo never runs the producer or
uses native SQL as a fallback. Embedding the producer does not enable an
unproved SQL runtime, WAL/VFS path or allocation adapter.
