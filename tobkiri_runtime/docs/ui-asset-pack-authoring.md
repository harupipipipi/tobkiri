# Independent signed UI asset Packs

`core_runtime.ui_asset_pack_authoring.build_ui_asset_pack` produces an unsigned,
inert Normal Pack from captured public UI bytes. The ordinary v4 schemas and
artifact compiler validate the output. This producer does not install, select,
activate, sign, or authorize a renderer.

The trusted Default UI Application declares the exact renderer slot
`defaultspack_ui_assets_pack` and expected publisher `tobkiri.repository`.
Its capture resolves that selected artifact separately from executable Function
bindings. A Pack with no Functions can therefore provide the renderer without
replacing the protected Defaults backend.

## Build and sign

Run these commands from `tobkiri_runtime`, including the normal frontend build:

```sh
npm --prefix ecosystem/defaultspack/webapp run build
python -B -m scripts.stage_compiled_ui_asset_pack_v4 \
  --ui-root ecosystem/defaultspack/ui \
  --target /absolute/task-owned/output/defaultspack_ui_assets_pack \
  --version 1.0.0
python -B -m scripts.tobkiri_pack sign \
  /absolute/task-owned/output/defaultspack_ui_assets_pack \
  --dev-signer-store /absolute/existing/development-signer-store \
  --dev-signer-key-id EXISTING_KEY_ID \
  --pack-id defaultspack_ui_assets_pack --version 1.0.0 \
  --publisher-id tobkiri.repository --core-compatibility '>=0'
python -B -m scripts.tobkiri_pack verify \
  /absolute/task-owned/output/defaultspack_ui_assets_pack \
  --public-key /absolute/publisher-public.pem \
  --publisher-id tobkiri.repository
```

The signing commands use an already provisioned development signer. A public
key or publisher name in an artifact does not establish Host trust. Successful
offline verification reports `authority_granted: false`.

For a custom build, call the public Python producer with an `assets` mapping
from `ui/...` paths to captured `bytes`. `ui/shell.html` is required. The producer
accepts public HTML, JavaScript, CSS, images, fonts, Wasm and text extensions;
it rejects hidden paths, traversal, runtime code paths and existing output
destinations. Input limits are 32 MiB per file, 128 MiB in total and 256 files.
Generated metadata adds overhead. Use a stable, task-owned output parent;
output publication does not claim protection against a concurrent process
replacing that parent pathname.

The frontend producer verifies file digests again while capturing bytes. An
empty top-level `__init__.py` marker is excluded from public UI assets; nonempty,
nested or unsafe markers are rejected. The Pack contains no Functions,
Contracts, executable variants, capability requests or dispatch routes.

## Renderer authority and read checks

Delegated JavaScript runs in the trusted Application's origin and inherits its
browser session and API authority. Empty Pack capability declarations do not
isolate that JavaScript. This is an explicit renderer delegation, so the Host
requires a nonlegacy publisher-key authorization for the exact Pack ID,
artifact digest and canonical signed-manifest digest. A broad legacy publisher
namespace is insufficient.

Capture verifies the selected immutable CAS bytes, signature, publisher, file
inventory, owner and activation identity. Subsequent reads recheck the selected
identity, current policy, owner, activation and requested asset digest. Current
trust and revocation checks remain in force. A selected renderer that is
missing, corrupt, stale or unauthorized fails closed. When no renderer slot is
selected, the original authenticated Application mount remains available only
with its captured identity verified. A failed selected renderer cannot fall
back silently.

Host authentication, Origin, CORS and CSRF checks remain in place. This path
does not introduce a separate browser origin or a new CSP isolation boundary.

## Initial deployment and later updates

The Launcher and Host must already support the renderer slot and the Native
authorization ceremony. Existing predecessor-only Native key enrollment does
not authorize a newly introduced renderer ID. Do not fabricate a predecessor,
broaden legacy trust, patch sealed Python, or strip an existing provider's
Functions to make the first renderer fit an older Launcher.

An initial authenticated foundation release must supply the missing first
exact-scope enrollment path where required. After that foundation is installed,
each renderer revision follows ordinary signed admission, exact Profile
selection, review and activation. Updating renderer bytes need not replace
Launcher, VM registration, credentials or conversation storage.

Compiler, signature and isolated CAS tests prove their individual boundaries.
They do not establish that the installed Native has admitted the first renderer
or that a paid model conversation has succeeded. Record those acceptance
results separately.
