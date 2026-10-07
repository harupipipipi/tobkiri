# Independent executable CLI presentation component

This independent `normal_sandbox` Pack provides a bounded plain-text transcript
renderer. It wraps messages, preserves blank lines, escapes ANSI/OSC/control/bidi
sequences and rejects output exceeding a UTF-8 byte limit. It executes no terminal
commands and requests no Host capabilities, network or secrets.

Canonical declarations use the **public producer** documented in
`docs/python_pack_authoring.md`: `PythonPackFunction` / `build_python_pack`, public
canonical digest rules and `compile_pack_root`. Each build renders a fresh
producer output, validates it with the actual compiler, then replaces only this
owned generated Pack directory. No handwritten seals or invented ABI remain.
The self-contained renderer implements the existing synchronous
`tobkiri_packvm_invoke(operation_id,payload)->dict` ABI, pinned to `python3.13`
and backend `tobkiri.python-pack-v4`.

The draft Contract is `acceptance.presentation.transcript.v1`, exact Operation
`acceptance.presentation.render`, provider `acceptance.cli.presentation`, Function
`acceptance.cli.presentation.render`. Closed schemas reject request-supplied
approval, paths, commands and identity fields. `contracts.draft.v4.json` and
`function.draft.v4.json` retain separate authoring sketches. The public producer
preserves draft status and non-normative author source provenance.

## Reproduce offline checks

From `tobkiri_runtime`, using an existing environment; no dependencies installed:

```sh
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 /path/to/existing/repository/.venv/bin/python -B acceptance/independent_extensions/cli_presentation/build.py
PYTHONDONTWRITEBYTECODE=1 /path/to/existing/repository/.venv/bin/python -B -m unittest discover -s acceptance/independent_extensions/cli_presentation -p test_cli_presentation.py -v
```

14 tests pass: offline rendering, controls/identity/approval/unknown-field denial,
boolean bounds, UTF-8 output limits, wrapping, public schemas, exact indexed file
bytes, input/output schemas, exact actual compiler route, unknown-operation ABI
denial, byte-identical producer builds and absence of activation authority.
`build()` accepts generic Pack/Contract/Function/Operation IDs and an owned output
destination for later independent composition; no runtime legacy ID fallback.

## Explicit unresolved CLI Profile source

`cli.profile.intent.v1.json` is a public-schema-valid `needs_resolution` authoring
intent. It requests a CLI Shell, the explicit independent
`acceptance.cli.application` Application and one exact renderer edge. That
Application and `acceptance.base` are unresolved required inputs, not installed
artifacts. All artifact/revision/authority fields remain null or empty. There
is no generated source-release lock, active ProfileLock, ResolvedPlan,
ActivationRecord, approval or captured authority.

## Remaining product boundary

`cli_io_v1.schema.json` permits only `health`, `echo`, `profile.identity` commands;
this component does not invent a new Shell command. The public Python authoring
API explicitly does not create CLI Shell/Application composition or presentation
bindings. `docs/pack_v4_minimal_profile.md` says complete canonical CLI Application
closure is future work. A renderer with a valid production-compatible executable
catalog therefore **does not establish replacement of the product frontend**.

Only public docs/schemas/SDK/API and this worker's files were used; no runtime,
Host, Defaults, other-Pack implementation or existing test internals were read.

| Check | Evidence |
| --- | --- |
| Useful offline success and denial | 14 local component/source/schema tests |
| Canonical executable Pack | Official public producer plus actual compiler validation |
| Deterministic output | All producer files byte-identical across fresh builds |
| Explicit CLI Profile source | Unbound, unresolved public-schema-valid intent |
| Signed admission/install/approval/activation | Not performed |
| Production Broker/PackVM invocation | Not performed |
| Actual CLI frontend replacement | Unresolved public CLI Application integration |
| Composition/removal/renamed IDs/unselected/unknown/conflict | Root-owned Host acceptance |

Compiler validation is offline, and ABI invocation here is local source execution.
Neither is captured Broker/VM execution. The Pack remains unsigned, untrusted and
disabled. Historical data and the live acceptance checkout are untouched.

## New named Profile using published identities

`profile_authoring.py` independently constructs and validates
`independent.tauri.renderer.profile.intent.v1.json`. It selects published
`defaults-basepack`, `shell.tauri.default`, `runtime.tauri.application.default`
and our executable `acceptance.cli.presentation`. Identity, selected Packs and
platform are parameters. This is a **new** Named Profile intent; it neither edits
Defaults nor asserts that Tauri consumes our renderer. No hypothetical caller
edge is added. The published Shell declaration is `build_required`, with no
verified executable variants: selection of published IDs is source authoring,
not proof of a runnable installed product.

Six additional public-schema tests verify identities and renamed source
selection, malformed IDs, duplicate/non-Application selection, absent platform
targets, and request approval/bound-digest injection denial. Combined author tests
now total **20**. Run with `-p 'test_*.py'` to include both suites. Profile compilation now uses the public `build_named_profile` API; no
source-release lock is handwritten.

The missing CLI product surface is precise: a complete CLI **Application Pack**
artifact plus its formal presentation bindings/consumer, and a public producer
for that Application/Shell composition. Existing Python Normal Pack authoring
produces pure components; Profile intent selects existing providers; neither
constructs a terminal Application nor extends the three finite CLI commands.
No distinct ApplicationDefinition schema appears in the public protocol schema
set; the available public manifestation is `pack.kind = application` and
separate presentation declarations. These findings do not infer private startup
behavior or grant native/VM authority.


## Formal named Profile compilation

The new `docs/named_profile_authoring.md` public producer successfully compiles
this intent into `named-profile-release/`, using the locked published template
and only our own additional Normal Pack. Outputs include generated
`authored.profile.intent.v1.json`, `authored.profile.v5.json`,
`authored.profile.lock.v5.json`, `authored.release.provenance.json` and
`bundle.lock.json`. The generated source lock pins our exact Pack digest and
has `activation_authority: unbound`. It does not provide a verified Shell binary.

To compile another fresh release under the owned namespace:

```sh
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 /path/to/existing/repository/.venv/bin/python -B acceptance/independent_extensions/cli_presentation/profile_authoring.py --compile-output acceptance/independent_extensions/cli_presentation/fresh-profile-release
```

Five additional public-producer tests exercise exact selected Pack pins,
supplied-but-unselected exclusion, a genuinely rebuilt renamed Pack, unknown
selection denial and duplicate additional-ID denial before publication.
**25 total tests pass.** Unknown selection returns `Profile Pack is unavailable`;
duplicate input returns `additional Pack identity is duplicate or unsupported`.
No producer bug or special-case workaround was observed. This proves source
composition through the formal compiler; actual Tauri renderer consumption,
CLI replacement, Host admission, activation and VM dispatch remain separate.

The generated `named-profile-release/` remains on disk but is ignored by Git;
review the recipe, tests, `verification.json` and
`named-profile-source-receipt.json`. The receipt records the formal compiler's
current source lock, revision, catalog, definition, provenance and closure pins.
It was refreshed against the current locked template/compiler/schema inputs.
