# Sealed Python environment contract

Packagers place the complete environment at
`{Tauri resource_dir}/app/python-runtime/`. The outer `app/` tree remains
covered by `runtime-resource-manifest.v1.json`; the nested environment is
independently covered by `python-runtime/sealed-environment.v1.json` using
the [environment manifest v1 schema](../../.github/schemas/sealed-python-environment.v1.schema.json).
Digest values are lowercase, unprefixed 64-character SHA-256 hex strings.

The manifest has exactly these fields: `schema`, `environment_digest`,
`platform`, `architecture`, `python_version`, `package_provenance`,
`sentinels`, and `files`. `package_provenance` has exactly `kind`, `package_id`,
and `release_digest`. `sentinels` has exactly `stdlib_sha256`,
`site_packages_sha256`, and `native_sha256`. Each sorted `files` item has
exactly `path`, `size`, `sha256`, and `executable`. `environment_digest` is the
SHA-256 of the compact JSON encoding of the sorted `files` array. The manifest
itself is excluded from that inventory; every other regular file is included.
Links, special files, duplicates, missing files, extra files, writable
executables, and non-canonical paths are rejected.

Outer resource-manifest keys are always relative to `{resource_dir}/app`.
For a sealed application entry `app/X`, the sole outer key is
`python-runtime/app/X`, and the application snapshot key is `X`. These three
domains are mapped exactly; unprefixed outer keys, alternate prefixes, and
fallback lookups are invalid. Runtime resource paths are slash-separated,
printable ASCII relative paths. Empty, dot, parent, backslash, colon, Unicode,
and ASCII-case-ambiguous paths are rejected before hashing. Every inventoried
regular file must have exactly one filesystem link.

The fixed layout includes `runtime/`, `venv/`, `app/kernel_entry.py`,
`app/defaultspack_entry.py`, `app/host_helper_entry.py`, `sentinels/`, and
`lease.v1`. The inventoried `sealed-directory-modes.v1.json` file binds the
directory tree using the [directory modes v1 schema](../../.github/schemas/sealed-python-directory-modes.v1.schema.json).
The interpreter is `venv/bin/python3` on Unix and `venv/Scripts/python.exe`
on Windows. The venv must install
`tobkiri_sealed.bootstrap` at
`venv/lib/pythonX.Y/site-packages/tobkiri_sealed/bootstrap.py` on Unix or
`venv/Lib/site-packages/tobkiri_sealed/bootstrap.py` on Windows; Launcher
invokes only that module with `-I -B` and one of the `typed`, `defaultspack`,
or `host_helper` roles. The launch v3 argument order and the protocol
identifiers are in [`sealed_python_protocol.rs`](../src-tauri/src/sealed_python_protocol.rs).
See the [launch example and platform bindings](../docs/sealed-python-environment.md#resource-contract).

Before reporting startup, the bootstrap must create the nonce-bound
[`io.tobkiri.sealed-python-attestation.v2` JSON](../../.github/schemas/sealed-python-attestation.v2.schema.json).
Its required fields are `schema`, `nonce`, `role`, `environment_digest`,
`executable`, `prefix`, `base_prefix`, `sys_path`, the three sentinel digests,
`runtime_overlay_sha256`, `outer_runtime_manifest_sha256`, and `lifetime_lease`.
Bootstrap emits `native_first_setup: true` only for `typed` when that policy
is active, and `packvm_disabled: true` only for the macOS CI probe on `typed`
or `defaultspack`. Launcher checks both values against the launch policy.
The attestation file reference is the `--attestation` path to the
`startup-<nonce>.json` file. The `io.tobkiri.sealed-python-attestation-file.v1`
contract governs publication and identity checks. The JSON payload uses the
v2 schema above.

The bootstrap first acquires a shared OS lock on `lease.v1`, retains it until
process exit, and sets `lifetime_lease` to `true`.
Launcher proves the shared lease by attempting the exclusive replacement side
before returning the child. Environment replacement must take that exclusive
lease and use a new content-addressed directory, never mutate the active tree.

`package_provenance.kind` is `pinned-python-build-standalone-v1` on macOS,
`windows-authenticode-v1` on Windows, and `linux-immutable-package-v1` on Linux.
On macOS Launcher independently validates the containing app's signature under
its build-bound policy. Missing or invalid packaged environments fail before
any external provisioner can start.

For historical artifacts, see the [older environment schema](sealed-environment.v1.schema.json),
which uses `sha256:` prefixes, and the [startup attestation v1 schema](../../.github/schemas/sealed-python-attestation.v1.schema.json).
Regenerate older environments and bootstrap code using the current packaging
tools before building with the current Launcher.
