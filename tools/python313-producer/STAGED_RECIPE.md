# Staged producer review contract (not an executable recipe)

The following gates must be resolved before claiming a usable Python 3.13
producer. This document does not grant approval to execute any downloaded code.

1. Recover or fetch a complete componentize-py tree from the recorded official
   upstream commit, with bounded download and an independently recorded archive
   digest. Compare the selected files with the manifest. Inspect the entire
   execution/dependency surface before proceeding, including submodules and
   workspace build scripts. The selected snapshot alone is insufficient.
2. Verify the official CPython archive and selected files with this verifier.
   Stage a new tree with safe extraction (reject traversal, unsafe links,
   special files, collisions, and over-limit content); do not mutate the
   historical review snapshot. Apply the exact patch with pre/post hashes.
3. Pin the exact WASI SDK, Rust compiler/target, native compiler, zlib, SQLite,
   crate dependencies, and all additional build inputs. Review and hash their
   actual bytes. Resolve licenses and any required execution approval before
   invoking them. No unverified automatic fetch or network-enabled build.
4. Prepare an explicit staged recipe for native build-Python, dependencies,
   CPython WASI static objects/shared library, the chosen runtime variant(s),
   linking, stdlib packaging, and final componentization. Preserve exact command
   lines, environment, artifact hashes, and dependency provenance per stage.
   Eliminate or contain nested environment scrubbing and unconditional async
   builds rather than relying on outer Cargo flags. Review each stage before
   execution; do not call the patched top-level build script indiscriminately.
5. Validate CPython 3.13 compilation and linking. Confirm HACL SHA2 plus remaining
   crypto modules, SQLite, compression, stdlib imports, and dynamic loading.
   Audit the existing SQLite randomness and WASI socketpair behavior against the
   runtime's authority model; do not introduce authentication bypasses to make
   tests pass. Do not infer runtime security from a source-only review.
6. Validate actual component behavior with the runtime's host-import boundary:
   local-first startup, approved/denied operations, filesystem jail, capability
   trust, networking restrictions, audit records, and malformed guest inputs.
   Preserve existing approval guards and require real smoke-test evidence.
7. Only after successful gates, publish a reproducible output manifest and
   test evidence. Source verification is not a claim that a component exists,
   that the runtime supports this combination, or that issue #1520 is complete.

## Reconstruction evidence

The recovered official archive was hashed and compared with all seven selected
CPython files. All 14 selected originals and seven patched hashes passed the
in-memory verifier. Fourteen unit tests cover the full verifier with bounded synthetic fixtures,
including manifest, patch, archive, selected-source tampering, and incorrect
patched-output hashes. They also cover valid patch application,
context mismatch, bad hunk counts, duplicate/unknown file paths, bad new-line
positions, unsafe file paths/symlinks/size limits, and the pinned patch digest.
No build command, upstream import, configure script, makefile, Cargo script,
downloaded binary, or test from upstream was executed.
