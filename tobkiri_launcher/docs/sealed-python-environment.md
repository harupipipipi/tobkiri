# Sealed Python environment packaging

Release builds create one native, relocatable Python environment per supported
Tauri target before packaging:

* `aarch64-apple-darwin` (macOS)

Intel macOS publication is currently fail-closed: the first cryptography
release fixing all three 2026 advisories is 50.0.0, and that release has no
CPython 3.13 macOS x86_64 wheel. Source builds and the vulnerable 48/49
releases are not packaging fallbacks.
The generator retains Linux/Windows fixture coverage, but current production
installer and release workflows intentionally build and publish macOS only.
Non-macOS production requests are rejected by the local release helper.

The universal locked runtime export is
`tobkiri_runtime/requirements.txt`. Formal macOS ARM packaging instead uses
`requirements-packaging-aarch64-apple-darwin.txt`, generated from `uv.lock`
with only compatible CPython 3.13 wheel hashes. CI verifies that generated
lock, performs wheel-only/hash-required dry runs for both ARM success and Intel
rejection, and audits both production requirements exports. CI installs the
universal runtime export and
`requirements-dev.txt` through
`.github/scripts/install_locked_python_test_dependencies.py`.
The pinned `uv` archive is staged by the existing resource preparer. Python
3.13.13 is installed and dependency wheels are synchronized at build time;
the packaged application never downloads on first launch.
Formal CI creates the sealed environment in a private rootless producer step,
using the target-bound export above, and publishes its absolute snapshot plus
raw manifest digest through `TOBKIRI_PACKAGING_PYTHON_SNAPSHOT` and
`TOBKIRI_PACKAGING_PYTHON_INVENTORY_SHA256`. Tauri resource preparation only
validates and copies that producer-owned snapshot; it never accepts a caller
requirements path or rebuilds a formal environment from the mutable checkout.
Before generation, the pinned uv executable must report the structured official
0.11.14 identity with a valid revision/date and the exact requested target
triple; arbitrary prefixes, suffixes, versions, and architectures are rejected.
The packaged Defaults projection generator receives only the verified absolute
Python executable and one core-bound `--source-provenance-file` input. That
file is `packaging-source-provenance.v1.json` and has exactly these fields:
`schema`, `source_commit`, `source_tree`, `source_clean`, and
`source_manifest_sha256`. Its schema is
`io.tobkiri.packaging-source-provenance.v1`; identities are lowercase raw
hex, and `source_manifest_sha256` must match the exact bytes of the checked-in
source manifest in the sealed snapshot. Rust creates and binds this file only
after verified Git-tree materialization. Python never creates provenance,
re-reads Git, or treats a mutable checkout or regenerated manifest as
authority. Direct Python presentation packaging requires the core-provided
private snapshot and provenance file (and is refused on Windows); no implicit
checkout-to-snapshot fallback exists.
The macOS workflow passes the provenance path through
`TOBKIRI_PACKAGING_SOURCE_PROVENANCE_FILE` and fails closed until the core
sealed-source step exports that exact file.

## Resource contract

Tauri maps `tobkiri_launcher/src-tauri/gen/app` to the stable packaged
`{resource_dir}/app`. The sealed subtree is exactly:

```text
{resource_dir}/app/python-runtime/
├── sealed-environment.v1.json
├── sealed-directory-modes.v1.json
├── lease.v1
├── runtime/                 # native CPython runtime
├── venv/                    # copied, relocatable environment
├── app/
│   ├── app.py               # packaged application closure
│   ├── kernel_entry.py
│   ├── defaultspack_entry.py
│   ├── host_helper_entry.py
│   ├── core_runtime/        # lazy-import closure
│   └── ecosystem/           # lazy-import closure
├── sentinels/
│   ├── stdlib.sha256
│   ├── site-packages.sha256
│   └── native.sha256
└── venv/*/site-packages/tobkiri_sealed/bootstrap.py
```

`python-runtime` is a producer-owned validation domain, not a generic
directory-name exemption. The resource preparer first validates this exact
subtree with the sealed-environment schema, raw manifest binding, complete
file and directory inventory, provenance, digest, and link-free identity
checks. Only that validation evidence permits the generic generated-directory
scan to traverse the subtree, including the required top-level `venv/` and
the CPython stdlib's `runtime/lib/python3.13/venv/` directory. A lookalike
such as `python-runtime-evil`, a nested `python-runtime`, or any `.venv`,
`venv`, or `virtualenv` outside this exact boundary remains forbidden. The
sealed validator separately rejects missing, extra, tampered, symlinked, or
hardlinked entries; the Rust build script then rechecks the same manifest and
exact inventory before binding the resource.

The outer runtime manifest uses one path domain: every key is relative to
`{resource_dir}/app`. A sealed `app/X` entry therefore binds only to outer
`python-runtime/app/X`; after snapshotting, its application-manifest key is
`X`. Packaging and runtime verification use this typed three-domain mapping
without prefix stripping, alternate candidates, or compatibility fallback.
Portable resource keys are printable ASCII with `/` separators and are unique
after ASCII case folding, preventing filesystem case and Unicode ambiguity.

The manifest has only the fixed top-level fields and fixed nested field sets
defined in `.github/schemas/sealed-python-environment.v1.schema.json`.
Its provenance `kind` is `pinned-python-build-standalone-v1` on macOS,
`windows-authenticode-v1` on Windows, and `linux-immutable-package-v1` on
Linux; `package_id` is always `dev.rumiai.app`.
`files` is a sorted, link-free inventory of regular files and excludes only
the manifest itself. All sealed digest fields, including `environment_digest`,
the three sentinels, the raw manifest binding, and attestation digests, are
lowercase 64-hex raw SHA-256 values; the `sha256:` prefix is not part of this
domain. `environment_digest` is SHA-256 over the compact serde-compatible JSON
bytes of that exact array. `lease.v1` is included in the inventory and is
opened under a shared OS lock by bootstrap for the lifetime of the process.

The manifest-v1 wire shape is intentionally unchanged for compatibility.
Exact directory identity is added as the required inventoried regular file
`sealed-directory-modes.v1.json`, whose schema is
`io.tobkiri.sealed-python-directory-modes.v1`. Its first entry binds the root
as `.` and its remaining sorted entries bind the exact transitive directory
closure; every mode is `0555`. Because this evidence file is in `files`, its
bytes are covered by `environment_digest` and by the raw manifest digest that
the launcher embeds. Existing v1 readers can deserialize the manifest, while
the producer, bootstrap, Rust build verifier, and launcher now require and
enforce the evidence. On POSIX, regular files must be exactly `0444` and
executable files exactly `0555`; merely non-writable but different modes are
rejected.

Python preparation and Rust staging preserve these modes. Tauri recreates
resource directories as `0755`, so the macOS packaging lane performs one
build-time Host seal on the actual `.app`: only the exact `0755` to `0555`
directory delta is accepted, with file bytes/modes, link counts, directory
closure, and manifest identity already verified. It then runs native imports
and all fixed roles from that packaged resource. No launch-time chmod or
permission exception exists. The signed app is checked afterward, and the
read-only DMG is mounted and subjected to the same exact-mode/native smoke
before upload, catching copy, bundle, image, or extraction drift.

The launcher invokes the fixed boundary:

```text
python -I -B -m tobkiri_sealed.bootstrap \
  --role typed --nonce <parent-nonce> \
  --attestation <new-attestation-path> \
  --manifest <sealed-environment.v1.json> \
  --environment-root <python-runtime> -- <role-argv...>
```

The launch wire schema is `io.tobkiri.sealed-python-launch.v1`; the startup
attestation schema is `io.tobkiri.sealed-python-attestation.v1`.
The wire role values are `typed`, `defaultspack`, and `host_helper`; `typed`
dispatches the packaged kernel wrapper. Bootstrap
rejects unknown boundary arguments, binds every supplied path to the sealed
snapshot, recomputes the stdlib/site-packages/native sentinel groups, and
publishes an attestation through a new temporary file, `fsync`, and atomic
no-replace publication. The role receives only the arguments after `--`.
`typed` directly runs `app.py`, `defaultspack` directly runs the long-lived
`ecosystem/defaultspack/defaultspack/desktop_app.py`, and `host_helper`
directly runs the stdin/stdout JSON
`core_runtime/host_broker/computer_host_helper.py`; wrappers preserve the
process environment, standard streams, and exit status. These targets and
their tracked lazy-import closure are copied under the sealed `app/` root and
are covered by the same manifest inventory. Bootstrap preloads the wrapper and
target, normalizes and validates prefixes, executable, native import roots,
and `sys.path` before publishing attestation. After that point dispatch uses
the preloaded target; the path guard rejects additions outside the snapshot.
In the packaged Defaultspack path, import roots come only from the sealed
`__file__`/`app/` layout. `REPO`, `RUMI_CORE_DIR`, `PYTHONPATH`, `PYTHONHOME`,
and `DYLD_`/`LD_` loader injection are rejected; the legacy environment
fallback remains a separate unpackaged-development behavior.

The sealed wrapper receives a process-private, bootstrap-issued scope bound to
the verified manifest and role target. Defaultspack accepts the sealed import
root only when that scope proves an exact target-file match; a snapshot
basename or client environment variable cannot select the packaged path.
After attestation, the import path object is frozen and dispatch must preserve
its exact contents.

Bootstrap emits only the fields in
`.github/schemas/sealed-python-attestation.v1.schema.json`. The native build
script binds the raw manifest SHA-256 as
`TOBKIRI_SEALED_PYTHON_MANIFEST_SHA256`; this binding is separate from the
outer Tauri resource provenance manifest.

## Threat boundary

The sealed snapshot and lease protocol are designed to fail closed against a
corrupt or non-cooperating updater, cross-UID replacement, symlink/reparse
substitution, hardlinks, special files, and path escapes. The generator emits a
non-writable snapshot and bootstrap restricts its attested `sys.path` to
canonical paths inside that snapshot before loading the sealed application
closure. This is an integrity boundary, not a claim of OS-enforced
immutability against an already-running malicious process with the same UID:
ordinary user-owned snapshots cannot provide that guarantee.

Windows/Linux installer and release publication is intentionally disabled until
their platform signing and native runtime validation are explicitly re-enabled.

The packaging lane owns the generator, resource assembly, and Python boundary;
the core Rust `sealed_python.rs` implementation remains the owner of launcher
binding/launch validation. Integration must keep the two implementations
aligned through the protocol drift test and must not add a competing Rust
schema validator in this lane.

Use the local, network-free validator with a prepared tree:

```bash
python .github/scripts/build_sealed_python_environment.py \
  --check --target x86_64-unknown-linux-gnu
```

Local contract tests use tiny synthetic trees and cover link materialization,
tamper, missing/extra inventory, path escape, permissions, role-wire, and
snapshot-`sys.path` behavior. Full CPython/native-extension construction is
deferred to the integration lane while the shared Cargo target is being
cleaned; it must cover native macOS relocation and all three role smokes before
the macOS release workflow is considered green.
