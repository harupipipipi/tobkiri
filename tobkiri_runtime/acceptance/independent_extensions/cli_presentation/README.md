# Independent CLI presentation and authenticated source frontend

The original `acceptance.cli.presentation/` Normal Sandbox Pack remains unchanged.
Its pure `acceptance.presentation.transcript.v1` / `acceptance.presentation.render`
operation wraps transcript text, escapes terminal controls and rejects oversized
UTF-8 output. The public executable Provider/Function is
`acceptance.cli.presentation.render`, as declared by its actual generated catalog.
It requests no network, secret, filesystem or Host capability.

A distinct `acceptance.cli.application/` source Application now uses the documented
`build_python_pack(..., application=True)` producer and actual Pack compiler.
Its finite `frontend_contract_map.v4.json` declares one POST logical
`/api/transcript/render` route targeting that exact renderer Provider, Function,
Contract and Operation. No map metadata creates a new invoke target. Its pure
`launch` Function returns a source presentation descriptor; it does not launch an
OS process or obtain authority.

The captured frontend resources implement a real stdio client through the public
`PanelContractSession` and `run_application_stdio` APIs. The only command is
`transcript.render`, carried inside the closed `application.invoke` frame. The
frontend obtains explicit endpoint/bootstrap JSON from an inherited pipe selected
by `--bootstrap-fd`; no bootstrap is accepted in arguments or environment, and
stdin/file descriptors are rejected. Session exchange, cookies and CSRF use the
normal public transport. Frames cannot select Profile/route/Function/approval.
Unknown commands and invalid renderer input fail before Contract HTTP. The
frontend never imports or directly invokes the renderer Function.

`cli-frontend-source.pyz` is a deterministic executable **source archive** of
captured frontend assets. It requires an existing Python/public SDK environment;
it contains no interpreter and is not a verified native Shell artifact. It is
ignored by Git but remains on disk, together with generated Profile releases.

## Reproduce

From `tobkiri_runtime`, using an existing environment; no dependencies installed:

```sh
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 /path/to/existing/repository/.venv/bin/python -B acceptance/independent_extensions/cli_presentation/build_cli_application.py
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 /path/to/existing/repository/.venv/bin/python -B acceptance/independent_extensions/cli_presentation/build_cli_application.py --compile-profile acceptance/independent_extensions/cli_presentation/fresh-cli-profile-release
PYTHONDONTWRITEBYTECODE=1 /path/to/existing/repository/.venv/bin/python -B -m unittest discover -s acceptance/independent_extensions/cli_presentation -p 'test_*.py' -v
```

The Profile destination must be new and inside this owned namespace. The producer
captures and compiles fresh Application output before replacing our generated
source Application directory. Both Application output and archive bytes are
reproducible. The original Normal Pack can separately be rebuilt with `build.py`.

## Named Profiles and evidence

`independent.cli.transcript.profile.intent.v1.json` selects published
`defaults-basepack`, `shell.cli.default`, our actual source Application and our
actual renderer. It requests the exact Shell-to-renderer edge. The formal public
Named Profile compiler produced `cli-profile-release/` with an authority-unbound
source lock and exact Application/Pack pins. Source CLI Shell metadata remains
`availability: build_required`, with no verified executable variants.

The earlier `independent.tauri.renderer.profile.intent.v1.json` and
`named-profile-release/` remain a separate source selection experiment; selecting
a renderer does not prove that the Tauri UI consumes it. Its recipe is
`profile_authoring.py`; generic identity and selected-Pack parameters remain
available. The historical `cli.profile.intent.v1.json` is an unresolved initial
authoring sketch, not release authority.

**47 tests pass**: 14 component/schema tests, six Profile authoring tests, five
formal producer selected/unselected/renamed/unknown/duplicate scenarios 13 frontend tests and nine native executable tests. Frontend tests include actual archive subprocesses and an isolated
loopback fixture for bootstrap/cookies/CSRF/finite Contract HTTP and stdio results;
the fixture supplies static Broker-shaped responses and is explicitly not a real
Host, approval or VM. Refusal tests cover missing/invalid bootstrap, denied
bootstrap/Contract, identity/approval/unknown/input injection, cancellation,
non-loopback endpoints and descriptor/byte bounds. Scoped Ruff passes.

`cli-frontend-source-receipt.json` records exact source Application, archive and
CLI Profile compiler pins. `named-profile-source-receipt.json` records the separate
Tauri source composition; `verification.json` retains the evidence boundaries.
No handwritten seal, invented ABI, private runtime/Host imports or other author's
implementation was used. The former missing public CLI-binding surface is now
addressed at **source** level by `docs/cli_application_authoring.md`; the reproducible
remaining gap is the published CLI Shell's absent verified prebuilt variant.

Source compiler and isolated authenticated transport success do not establish
trusted Application packaging/install, real Host bootstrap, admission/approval,
active Profile/Plan, production Broker/PackVM execution, native Shell launch,
provider API delivery or complete product frontend replacement. No live user
state, approval, credential or native UI was changed. No commit was made.


## Actual native macOS arm64 candidate

`native/main.m` and `native/build_native.py` now produce
`native-build/TobkiriCLI.app/Contents/MacOS/tobkiri-shell` with system Xcode clang
and the macOS SDK. The actual executable is Mach-O arm64; system Foundation,
CoreFoundation, libobjc and libSystem are its only direct runtime dependencies.
There is no Python interpreter/public SDK runtime requirement. The executable's
fixed command resource is copied byte-exactly from the unchanged Application,
hash-pinned into the binary, and sealed by an ad-hoc signature; strict codesign
verification passes. The build directory stays ignored on disk.

```sh
PYTHONDONTWRITEBYTECODE=1 /path/to/existing/repository/.venv/bin/python -B acceptance/independent_extensions/cli_presentation/native/build_native.py
PYTHONDONTWRITEBYTECODE=1 /path/to/existing/repository/.venv/bin/python -B -m unittest discover -s acceptance/independent_extensions/cli_presentation -p test_native_cli.py -v
```

Nine actual native subprocess tests run with only system PATH and explicit
inherited fixture bootstrap: authenticated stdio/cookie/CSRF/exact route success,
FD/auth/input/duplicate/unknown/identity refusal, cancel/no-dispatch, HTTP redirect
refusal, response/output byte limits, exact result types/keys, and command-resource
mutation refusal. Bootstrap material never enters arguments or logs. HTTP uses
ephemeral state, disables ambient proxies/cookies and never follows redirects.
The native frame parser rejects duplicate keys and has finite depth/byte limits;
Host schema, captured routes and Broker authorization remain authoritative.

`native-cli-receipt.json` records actual binary/resource/file pins, system
library closure and strict ad-hoc verification. Its regular-file inventory is
not presented as the formal platform Shell directory digest. The former absent
native binary is now an actual candidate, but published source Shell metadata
still has no installed verified variant. Isolated executable tests are not a
trusted packaging/install, real Host bootstrap/approval, activated Profile/Plan,
production Broker/VM call or native product Shell launch ceremony. Root owns
that remaining formal acceptance. Original Application/renderer inputs are
unchanged; no new native main/startup path or live state was touched.

The public `describe_shell_variant` API now verifies this exact candidate against
the current source CLI build target, including ordinary tree/entrypoint,
architecture, bundle identity and strict macOS signature checks.
`native-shell-variant-receipt.json` records the returned **ordinary candidate**
variant and current source definition pins. CLI source Profile compilation was
refreshed against the current locked template. Application/renderer/native bytes
remain unchanged; candidate verification changes no Shell availability,
trusted catalog, installation, approval or activation.
