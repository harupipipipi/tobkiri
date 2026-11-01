# Sealed Python environment contract

Packagers place the complete environment at
`{Tauri resource_dir}/app/python-runtime/`. The outer `app/` tree remains
covered by `runtime-resource-manifest.v1.json`; the nested environment is
independently covered by `python-runtime/sealed-environment.v1.json` using
schema `io.tobkiri.sealed-python-environment.v1`.

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
`lease.v1`. The interpreter is `venv/bin/python3` on Unix and
`venv/Scripts/python.exe` on Windows. The venv must install
`tobkiri_sealed.bootstrap` at
`venv/lib/pythonX.Y/site-packages/tobkiri_sealed/bootstrap.py` on Unix or
`venv/Lib/site-packages/tobkiri_sealed/bootstrap.py` on Windows; Launcher
invokes only that module with `-I -B` and
a closed role enum. It never supplies `PYTHONPATH` or an application script
path.

Before reporting startup, the bootstrap must create the nonce-bound
`io.tobkiri.sealed-python-attestation.v1` JSON requested by Launcher. Its exact
fields are `schema`, `nonce`, `role`, `environment_digest`, `executable`,
`prefix`, `base_prefix`, `sys_path`, the three sentinel digests, and
`lifetime_lease`. The bootstrap must first acquire a shared OS lock on
`lease.v1`, retain it until process exit, and set `lifetime_lease` to true.
Launcher proves the shared lease by attempting the exclusive replacement side
before returning the child. Environment replacement must take that exclusive
lease and use a new content-addressed directory, never mutate the active tree.

On macOS `package_provenance.kind` is `apple-code-signature-v1` and Launcher
independently validates the containing app's platform signature. Integrity
digests are not treated as package authenticity. Missing or invalid packaged
environments fail before any external provisioner can start.
