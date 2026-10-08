# Public CLI Application source and authenticated frontend

The existing Shell contract remains `app.shell.v1`, with `terminal_stdio`,
structured `io.tobkiri.cli.io.v1` frames, local-auth `runtime-profile`, and
prebuilt artifact/entrypoint pins. Source Shell metadata is `build_required`
until the formal packager supplies and verifies its platform artifact. This
SDK does not mint native bootstrap codes, approve a Profile, install an
Application or supply an implicit interpreter/credential fallback.

`core_runtime.pack_authoring.build_python_pack(..., application=True)` creates
an unsigned source Application through the real Pack compiler. It supports
exactly one pure Python PackVM Function, otherwise retaining the Normal producer's
finite captured bytes, exact canonical Contracts, no capabilities/network/secrets,
and no imports/execution during authoring. Existing Normal outputs are unchanged.
`build_named_profile` accepts this source Application root, selected with role
`application` instead of the existing Tauri Application. Exactly one Application
is still required. Use the published CLI Shell identity `shell.cli.default`;
source compilation does not make its missing prebuilt variant available.

Ship the Application's exact `frontend_contract_map.v4.json` as a declared asset.
Its finite routes use the existing public map shape: schema
`io.tobkiri.frontend-contract-map.v4`, `pack_id` namespace, `routes` with method,
logical `/api/...` path, `presentation: broker_result`, one exact target containing
contribution/Contract/Operation/provider/Function IDs and allowed payload keys.
Its targets must match the selected actual executable catalog; metadata cannot
add an invoke target. Host HTTP capture remains responsible for admission,
Application/Plan pins, schema, caller identity, authority and audit. External
Normal-Pack admission does not accept an Application and is not weakened here;
a real Application/Shell still needs the ordinary trusted packaging/install path.

Public frontend API: `tobkiri_protocol.cli_application.PanelContractSession` and
`run_application_stdio`. The Shell must obtain its endpoint and one-time panel
bootstrap from the Host's normal local-auth ceremony, explicitly passed to this
frontend. Keep the bootstrap out of arguments/output/logs (use an inherited pipe
or descriptor); do not discover credentials or use the debug CLI session.
The endpoint is loopback `http://127.0.0.1:<port>` only; redirects and proxies are
rejected. The SDK exchanges `/api/panel/auth/exchange`, retains cookies/CSRF in
memory, and uses finite captured Contract routes with fresh request identity.
A missing/expired/denied session produces failure without an alternate executor.

The frontend supplies an immutable command-ID map from its pinned Application
artifact. Each declaration contains `namespace`, logical `path` and the exact
self-contained `input_schema`. `application.invoke` frames supply only
`arguments: {command_id, input}` along with the standard request ID, null stdin,
false tty and bounded output limit. Profile/session/Function/route/approval
selection is forbidden in frames. Unknown command IDs fail before HTTP. The SDK
validates input, calls `POST /api/contracts/<namespace>/<encoded POST path>`
through the normal Host adapter, and requires the successful Broker envelope.
The renderer returns exactly stdout/stderr/exit_status/stream, validated as a
CLI result before output. Cancellation does not invoke a renderer. No guest
Function is imported into the frontend process or invoked in place of the VM.

A source frontend or generated executable archive is not a verified platform
Shell variant. Preserve that distinction in receipts: isolated authenticated
HTTP/stdio boundaries and source compiler success do not prove real Host, VM,
native Shell launch, approval, model delivery or frontend replacement.

For an actual prebuilt candidate, place its bytes at the source Shell's exact
`launch.build_targets[].artifact_ref` and entrypoint. The macOS CLI target is
`TobkiriCLI.app`, with `TobkiriCLI.app/Contents/MacOS/tobkiri-shell` inside it.
`tobkiri_protocol.shell_authoring.describe_shell_variant(shell, artifact_root,
platform="macos", architecture="arm64")` validates the definition revision,
declared target, canonical artifact tree, entrypoint, architecture, bundle ID
and strict macOS signature using the ordinary platform verifier. It returns
the existing variant shape. It does not change `availability`, publish a trusted
catalog, install or activate anything; those still require the normal packager
and Host ceremony. An ad-hoc signature check is evidence of intact bytes, not
publisher trust or user approval.
