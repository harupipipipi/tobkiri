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
