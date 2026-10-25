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
This is only the registry portion: the sole pinned Git dependency, Rust toolchain,
offline build configuration and runtime builds are separate steps. Registry
acquisition alone is not a complete offline environment.

`fetch_toolchain.py` restores the historical Rust 1.94.0 version for the Linux
host and `wasm32-wasip2` target. It checks the official March 5, 2026 release
manifest against SHA-256
`aaa177def36e01d539bee6bde95295230b9ce378f81057845db8d0ebe97898ee`,
then independently checks the four selected compiler/Cargo/standard-library
archive hashes against that manifest. Bounded private extraction checks all
paths, file kinds, modes and internal links. Components are merged with exact
collision checks and no symlink-parent traversal; immutable hardlinks avoid a
second large copy. Neither upstream install scripts nor the restored compiler
run during this stage. The assembled file inventory is retained for later
read-only build mounts. The Git dependency, offline builds and actual tool/runtime
execution remain later stages; input restoration alone is not execution proof.

`fetch_git.py` restores the recorded `dicej/wasm-tools` commit and its sole
`tests/testsuite` Git input. The main commit/root tree must match the historical
receipt; its actual `.gitmodules` and gitlink must bind the one testsuite URL
and commit. The testsuite commit/root tree are independently checked, and any
additional nested Git input rejects. The CI image's Git client fetches only
these two exact commits into fresh bare repositories, with an empty private
home/template, disabled credential helpers/hooks, HTTPS-only transport and full
Git object verification. The existing process/resource supervisor covers this
network-enabled acquisition stage. Commit and tree object hashes are checked
again independently before recording the resulting Git files. No upstream
checkout, Cargo command or dependency source executes. A usable offline Cargo
cache and the actual runtime builds are checked separately.

`build_runtimes.py` rechecks the preceding source, registry, Rust, Git and WASI
receipts against the actual files, then primes Cargo inside the same networkless
namespace used for builds. Registry lookups use only verified directory sources;
the two exact Git URLs rewrite only to read-only local bare repositories, and
Git permits only the file protocol. These repositories retain their ancestors:
ordinary Cargo fetch rejects refs from a shallow input even when Git exits zero.
After local-only acquisition, `cargo fetch --frozen` must succeed. All checkout
source files, modes and blob identities (including the one exact submodule)
are compared with the pinned input graphs before the cache becomes read-only.

The runtime stage replays the unchanged historical `wasi-runtimes.sh`, including
both sync and async variants, frozen dependencies and one build job. It retains
the recorded 4 GiB per-process runtime limit; native C builds still default to
their original 2 GiB limit. Only the recorded `lib_dir=/work/wasi` line is added
to the staged PyO3 configuration. All inputs and build configurations are checked
again afterwards, and new artifacts keep their own hashes. Core-Wasm magic alone
does not establish Wasmtime validation, producer CLI, componentization or guest
acceptance; those remain subsequent stages.

`build_assets.py` then verifies the successful runtime receipt and actual output
bytes, checks the existing standalone packager's exact derivation from the
original lock and strip function, and builds/tests that worker with frozen
dependencies in the networkless namespace. It uses only the already verified
registry closure and Rust toolchain. The helper has its own manifest and does
not invoke the producer's top-level build script.

`asset_inputs.py` selects the two real runtimes, built Python library, seven SDK
libraries, original pinned reactor adapter, installed standard library and
original bundled Python sources. Every included regular file gets a bounded
hash record; unsafe links and incomplete selections reject. The existing tar
inclusion rules and documented deterministic tar metadata are preserved.
Read-only mounts expose these inputs directly, without copying the build trees.
The original packaging invocation uses the current manifest and worker hashes,
never substituted historical output identities. Its worker validates Wasm before
and after stripping; all thirteen output files must match its successful receipt.
Input identities are checked again after execution. Receipts record this as
packaging and wasmparser validation only: Wasmtime/runtime compatibility,
component generation, the producer CLI and guest execution remain separate.

`build_cli.py` verifies those actual assets and prepares a separate publishing
source tree. Every original file and mode is retained; the only source change
adds `build = "alt-build.rs"` to the root package, then the thirteen verified
assets are added. The original top-level build script remains present but is
not selected. The hash-bound historical CLI recipe builds with frozen inputs,
one job and a read-only Git cache, preserving its original namespace for Git
alternates. It retains the 4 GiB address-space limit and the stricter existing
1,200-second stage supervisor, within the historical 3,600-second maximum.
Separate isolated `--version` and `--help` checks each have a 15-second timeout.
All inputs and source bytes are checked again. A successful CLI and its receipts
are retained as a separate CI artifact for subsequent remote component builds;
no producer binary is downloaded to the desktop. Startup success does not
establish componentization, guest behavior, Core acceptance or GUI parity.

The final stages restore the existing pinned Wasmtime 48 validation wheel and
WAC 0.12 binary as data, verifying complete hashes before bounded extraction.
`component_inputs.py` captures the original usage-cost, stream-normalizer and
finalizer plans and all 31 source references. The finalizer's tool-bridge WIT
reference is explicitly the original v2 definition. Current source bytes must
still match those historical plan pins; no historical evidence is rewritten.

`build_components.py` runs the recorded bindings/componentization/composition
steps with the rebuilt custom CLI, preserving exact inner/outer typed boundaries
and complete captured source checks. Each Pack has its own fresh scratch and
networkless namespace. Producer, validation tools, SDK, inputs and controls are
read-only. This producer stage uses the historical component-trial 8 GiB process
limit and existing supervisor; native2/Rust4 limits retain their defaults and
Core/guest budgets are unchanged. Each produced component must fit the original
32 MiB file bound. New hashes and custom-producer provenance are retained with
the components only on CI. Production activation, guest runtime-info, actual
five-Pack dispatch and native Core/Gate acceptance still require their own tests.

The separate `Issue 1520 original five-Pack source sharing` workflow consumes
the recorded producer run `39956025119` at exact head
`ae7f83080a36e823ac330342584210243dc38050`. It waits for that run to succeed and
downloads its component artifact only on CI, avoiding another producer rebuild
for this measurement. `verify_original_components.py` rechecks every component,
receipt, original plan, current captured source and producer binding. The two
existing bridge/caller fixtures retain their independent authentication path.
The existing bounded unbundler measures all five actual binaries and reports
whether their extracted code closure fits the unchanged 64 MiB catalog limit.
A completed measurement does not imply that the size fits or that Core ran the
graph. Only metadata is uploaded by this consumer workflow; new source changes
require deliberate producer pin review rather than silently reusing old bytes.

That mixed checkpoint measured 69,357,323 bytes after extraction against the
67,108,864-byte catalog limit. The old two artifacts and newly rebuilt three
use distinct runtime/library code, so that measurement is an explicit failed
capacity gate, despite successful component production and inspection.

`Issue 1520 coherent original Python components` reuses the authenticated CLI
artifact from that same completed producer run and rebuilds all five originals
together. The original bridge/caller source-only stagers recover the exact
hash-bound sources and reviewed-input records; all twelve and ten plan inputs
must match. The existing three-component checkpoint remains separately usable.
The new consumer restores executable permissions lost by artifact transport,
checks the actual binary and publishing-source receipt again, and requires no
CPython source/toolchain restoration. Its namespace omits that unused source
mount; receipts distinguish this source provenance from a fresh full-tree check.
Current Pack sources, tools, controls and producer are still read-only during
each isolated build. Component order is usage, stream, bridge-v2, caller,
finalizer, matching the existing five-Pack native fixture roles.

The complete graph is independently verified, measured and materialized only
if it fits the original 64 MiB input limit. CI retains the five components and
exact shared-code closure for subsequent native Core execution. This does not
yet prove Core admission or runtime replay and does not alter production limits.

The coherent checkpoint `40131893888` passed all five builds and materialized
62,842,959 bytes of component/code source (13 distinct modules), fitting the
unchanged 67,108,864-byte limit. The separate native workflow authenticates this
run and source head, rechecks all original/source/producer bindings, and verifies
every extracted root and code module before exporting native fixture inputs.

The existing native provider fixture has an explicit original-Python shared
graph test. It retains all 94 provider, 81 bridge, 30 caller and ten finalizer
cases, original runtime probes, nested Gate/Audit checks and Session close.
It requires all five implementations to be Python and all four source oracles.
The exact transformation helper is shared with the already-tested two-Pack
graph; original manifests/assets are retained and new hashes/Profile pins are
derived. Complete source accounting adds noncomponent files per logical Pack
before checking the same 64 MiB bound. Core independently re-verifies and admits
the graph. This native test is pending until its actual supported-platform job
completes; successful artifact transport or compilation alone is insufficient.

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

## UI-local rpds dependency candidate

`issue-1520-ui-rpds.yml` builds a separate, inactive dependency experiment for
the existing Setup UI calculation. It restores the unchanged saved producer
from run `39956025119` / source `ae7f83080a36e823ac330342584210243dc38050`.
`fetch_rpds.py` pins rpds-py 2026.6.3 at
`7277eb681f6efd67eca1bbaa32f78d78bdc044a5`, its exact Cargo manifest/lock,
and all eighteen registry dependencies. `build_rpds.py` derives expanded input
inventories from the pinned archives and invokes the original supervised,
network-isolated CI namespace with a read-only configuration directory.
Cargo's bounded machine-readable root-cdylib record selects the generated
artifact path; the probe does not infer a filename from the target triple.

The candidate targets `wasm32-wasip2` with PyO3's `abi3-py313` feature. A build
must provide real limited-API evidence, core-Wasm dynamic-link metadata,
`PyInit_rpds`, and imported memory/table types before the library is staged as
`rpds.abi3.so`. A version-specific native binary is never renamed to satisfy
discovery. The Python probe imports the extension during preinitialization,
then exercises immutable map/set operations through its actual exported
operation after snapshotting. The unchanged SDK WAC helper hides the producer's
private initializer; the executed outer component must have no imports and
exactly the declared probe interface. Original limits and `post_return` cleanup
remain required.

Run the small offline regressions without compiling or executing any guest:

```sh
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover \
  -s tools/python313-producer/tests -p 'test_rpds_*.py'
```

The full build/import/run is CI-only. Source tests and build flags do not prove
extension compatibility; inspect the actual CI source, ABI, supervision and
post-snapshot observation receipts. The saved-producer artifact retains its
separate authenticated-run receipt contract. This component has no Profile,
Session, Host I/O, port or approval authority. It does not establish browser
worker hosting, native capture custody/resource enforcement, complete jsonschema
dependencies, full Setup/GUI parity or product activation. Those remain required
before switching the original Launcher flow.

## Complete JSON Schema dependency candidate

`issue-1520-ui-schema.yml` continues from the actual rpds run `40182231971`
on source `53163ed815c62c4853bbed3d8a082c0764e3084f`. It authenticates that
successful run and the unchanged producer run before restoring their artifacts.
`fetch_schema.py` selects the complete CPython 3.13 dependency closure from the
existing runtime `uv.lock`: attrs, jsonschema, jsonschema-specifications,
referencing and packaging, plus a separate locked native rpds wheel for the
reference interpreter. Each complete wheel, including resource and metadata
members, is authenticated before and after execution. No pip install, dependency
upgrade, generated expected result or substitute validator is involved.

The reference interpreter runs original wheels through ordinary imports in an
isolated `-I -S` namespace. The Wasm candidate executes their unchanged Python
source through a bounded closed-tree loader. Standard package resource and
distribution APIs receive the original captured bytes; no missing path is
resolved through the host or network. Parent-prefix indexes and joined queries
are bounded before expansion. The complete source, resource, metadata and
generated literal capture must fit the unchanged 64 MiB input bound; actual
component/Store/fuel/deadline/output limits remain independently enforced.

The late `packaging.tags` import also requires the original stdlib `logging`
package. `fetch_schema_stdlib.py` obtains its three unchanged modules from the
existing pinned official CPython 3.13.16 archive on CI, rederives their identity
from that archive, and includes them in the same source/copy/index limits.
This supplies source without warming the late import or adding host operations.
The native reference uses its own ordinary CPython 3.13.16 stdlib, independently
of the Wasm capture loader. Further runtime requirements remain observable
failures until their actual behavior and permitted boundary are established.

The Python rpds wrapper comes verbatim from the locked native wheel, whose
member identity is recorded separately. It is absent from the pinned Git
archive; this experiment does not attribute it to Git or infer how the wheel
was built. Only the already-proved genuine Wasm extension is placed below that
wrapper. Linux binary and distribution ABI metadata do not establish Wasm ABI.

The same observable calls compare valid/invalid documents, invalid schemas,
local/external/unresolved references, nested error context/paths/causes, each
registered format checker, normal rpds wrapper delegation and Map/Set behavior.
They also exercise first post-snapshot imports, original package schema-resource
loading, and late metadata/version/RECORD/member reads. Source/oracle success is
insufficient: the actual native and Wasm observations must match in CI.

The adapter supplies virtual read-only resources, not an OS pathname or a
general writable filesystem. APIs requiring a real path, such as `as_file`, are
not established by this experiment. Native capture custody, browser-worker
hosting, original resolver extraction, complete Setup/GUI parity and production
activation remain separate obligations. Run its small offline checks with:

```sh
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover \
  -s tools/python313-producer/tests -p 'test_schema_*.py'
```
