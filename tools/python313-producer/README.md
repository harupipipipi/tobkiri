# Python 3.13 producer source review

The source reconstruction described below was **source-only**. No upstream
program was executed during that reconstruction. The previous unpublished
implementation was not recovered; these outputs were regenerated from surviving
source bytes.

## Subsequent bounded trials

Later isolated builds and development replays are preserved separately:

- [Current-source finalization trial](trials/2026-10-20-finalization/) records
  six component builds, two standalone bridge replays, and the ten-case
  five-component replay.
- [Current-source caller trial](trials/2026-10-20-current-caller/) records the
  thirty-case original-Python caller/bridge composition.

These records establish only their explicitly bounded build and development
execution results. They do not establish native Core/Gate acceptance, production
activation, complete Host retirement, or completion of issue #1520. They do not
turn the upstream top-level build into a safe or reproducible shortcut. The
source-verifier and execution-boundary guidance below still applies.

## Reconstructing missing trial artifacts on CI

The compact handoff preserves Git history and the three committed compressed
Wasm fixtures; it does not preserve the remaining finalization components or
the custom producer executable. Rebuilding those components begins with the
`Issue 1520 original Python producer inputs` workflow. It fetches only the four
archive URLs and SHA-256 identities already recorded in `stage_sources.py`,
streams at most 100 MiB per archive, and then runs the existing independent
archive/selected-source/patch checks. CI retains the download and full staged
source inventories, without downloading those source trees to the desktop.

The workflow then replays the separately recorded native build-Python recipe
with one make job. It checks every staged source byte/mode against the original
full manifest before execution and afterwards. Bubblewrap provides private
user/network/PID namespaces, read-only source/tools, a cleared environment and
only a fresh writable build directory. The existing trial supervisor bounds
time, files and sampled resources; `prlimit` additionally retains the original
2 GiB per-process address-space limit. Unavailable isolation fails the stage.
Only metadata and the build log are uploaded, never the source tree or binary.

This stage uses the current Ubuntu CI compiler, records its actual binary
hashes and installed package versions before execution, and records the new
build-Python hash on success. It does not claim the historical Debian binary
identity. The exact recipe checks CPython 3.13.16, Unicode 15.1.0 and native
hashlib/json imports. WASI libraries, producer CLI, componentization and guest
execution remain separate stages. These receipts neither replace native Core
replay nor permit changing historical component pins.

The next workflow step restores the exact WASI SDK 33.0 Linux archive from the
historical trial (193,563,606 bytes, SHA-256
`0ba8b5bfaeb2adf3f29bab5841d76cf5318ab8e1642ea195f88baba1abd47bce`).
SDK acquisition has separate bounds; the original 100 MiB source-archive limit
is unchanged. The full download is verified before bounded private extraction.
Only regular files/directories and internal links are accepted, and all links
must resolve within the new tree before any tool is invoked.

`build_wasi.py` replays the three hash-bound historical zlib, SQLite and CPython
WASI scripts. The SDK, source and preceding native build are read-only. The
previous zlib outputs become read-only for SQLite, and all dependency outputs
become read-only for CPython. Each stage retains the original supervisor and
resource limits, records actual output hashes, and checks that earlier inputs
remain unchanged. The existing SQLite feature flags are preserved; this is
not a claim of ordinary SQLite or cryptographic-random parity. Core-Wasm magic
is only an encoding sanity check. Wasmtime validation, producer runtimes/CLI,
componentization and actual guest behavior still require their later stages.

`fetch_registry.py` separately restores all 410 registry dependencies in the
original hash-bound Cargo.lock. Each archive must match its locked SHA-256
before the complete tar inventory is checked and extracted into private source
storage. The source extractor retains its traversal, link, collision, special
mode and file-size checks; additional aggregate download/member/expanded-byte
limits cover the whole closure. Cargo directory-source checksums and the actual
file identities are recorded. Neither Cargo nor any dependency program runs.
Only the registry path accepts old tar mode fields containing a redundant file
type that agrees with the member header. Privileged bits and conflicting types
still reject; the original four source-archive mode checks remain unchanged.
This is only the registry portion: the sole pinned Git dependency, Rust 1.94.0
toolchain, target, offline build configuration and runtime builds remain separate
steps. Registry acquisition alone is not a complete offline environment.

## Pinned inputs and reproducibility

- Official CPython 3.13.16 archive:
  https://www.python.org/ftp/python/3.13.16/Python-3.13.16.tar.xz
- Archive SHA-256:
  `f4b1bfb3c79b5bb11b8d228a12504163b4c0dab4d679828d8f5f26b6cb6ab35d`
- Selected componentize-py 0.25 source snapshot, attributed to commit
  `c0949b19d464f5d70bc1051195a3ae0e6a012df9`. The manifest records exact file
  hashes and upstream URLs. A complete upstream tree has **not** been
  independently authenticated by this reconstruction.
- Reconstructed patch SHA-256:
  `160cbaa6ceaf8b3707cb0d8d8eec630ea585ee52e08af5360553397638bcdeaa`

`source-manifest.json` pins 14 selected originals and seven patched outputs.
The self-authored verifier pins the manifest and patch themselves, compares all
selected CPython files with regular members of the hashed official archive,
and applies exact-context unified hunks in memory. It neither writes source
outputs nor imports, builds, or runs upstream code. Source layout:

```
SOURCE/componentize-py/{build.rs,alt-build.rs,Cargo.toml,Cargo.lock,...}
SOURCE/cpython/{configure,configure.ac,Makefile.pre.in,...}
```

Run only the native verifier and its tests:

```
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover \
  -s tools/python313-producer/tests -v
PYTHONDONTWRITEBYTECODE=1 python tools/python313-producer/verify_source.py \
  --source SOURCE --cpython-archive PATH/TO/Python-3.13.16.tar.xz
```

The archive is limited to 100 MiB and each selected regular file to 16 MiB.
No network or external command is used. This verifies selected source bytes,
not arbitrary files in an entire source checkout. Do not use the receipt as a
whole-tree attestation or as permission to execute a build.

## Exact delta

The patch moves library/stdlib names, link arguments, and PyO3 configuration
from Python 3.14 to 3.13; fixes the WASI config-site path; replaces the custom
CPython auto-fetch fallback with a failure requiring a verified staged tree;
and enables the WASI dynamic-linking configure option. The CPython delta is
exactly one corresponding line in each of `configure` and `configure.ac`.

Five HACL archive link arguments absent from the 3.13 build are removed (HMAC,
BLAKE2, MD5, SHA1, SHA3), while the existing SHA2 archive stays linked. This is
not evidence of removal of those algorithms from CPython.

No socket, random, authentication, capability, or policy patch is introduced.
The existing WASI socketpair authentication exception in official CPython and
`SQLITE_OMIT_RANDOMNESS` in the upstream build script are source-review findings,
not changes made by this patch. Neither is silently expanded or treated as
proven safe here.

## Execution is gated

Do not run top-level Cargo as a shortcut. The existing upstream build script:

- strips nested `CARGO*` and `RUST*` environment variables, so an outer offline
  flag or build override does not establish nested-build isolation;
- builds both synchronous and asynchronous variants unconditionally;
- fetches zlib and SQLite without verified content hashes;
- can fetch a WASI SDK and execute downloaded build tools;
- trusts pre-existing output paths rather than a complete provenance receipt.

The removed CPython fallback does not resolve these other download or execution
paths. `tools/python-producer` is an older Python 3.14/custom-source experiment;
it is not an approved substitute and is not applied by this work.

See `STAGED_RECIPE.md` for the required next-stage review boundaries. The patch
remains experimental. The original source review did not establish compilation,
linking, dynamic-loader compatibility, module availability, runtime authority
enforcement, guest tests, or component output. Consult the separately bound trial
records above for subsequent evidence rather than treating the source review as
execution proof or general runtime acceptance.
