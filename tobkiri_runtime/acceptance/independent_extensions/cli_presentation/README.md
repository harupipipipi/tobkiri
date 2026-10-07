# Independent CLI presentation authoring experiment

This independent Normal Sandbox Pack owns a bounded plain-text transcript
presentation. It renders user/assistant/system/tool messages, wraps text, preserves
blank lines, escapes ANSI/OSC/control/bidi sequences and rejects output exceeding
a UTF-8 byte limit. It executes no shell commands and needs no network, cloud key,
secret, filesystem or Host capability.

The canonical four Pack documents are the unmodified official minimal scaffold:
empty Functions, contracts and executable variants, with `declarative_only`
execution. The renderer source is deliberately outside its indexed runtime
closure. `contracts.draft.v4.json` and `function.draft.v4.json` are separate
non-authoritative authoring sketches, not a captured executable Pack.

The explicit draft contract is `acceptance.presentation.transcript.v1`, operation
`acceptance.presentation.render`, provider `acceptance.cli.presentation`, Function
`acceptance.cli.presentation.render`. Inputs and outputs are closed schemas;
request-supplied approval, paths, commands and identity fields are rejected. This
is a presentation replacement component rather than a duplicate web frontend.

## Reproduce offline source checks

From `tobkiri_runtime`, using the preexisting environment:

```sh
PYTHONDONTWRITEBYTECODE=1 /path/to/existing/repository/.venv/bin/python -B acceptance/independent_extensions/cli_presentation/build.py
PYTHONDONTWRITEBYTECODE=1 /path/to/existing/repository/.venv/bin/python -B -m unittest discover -s acceptance/independent_extensions/cli_presentation -p test_cli_presentation.py -v
```

12 source tests pass: offline rendering, terminal-control denial, identity and
approval injection denial, unknown role/field denial, boolean bounds denial,
multibyte output limits, wrapping, official and draft public schema validations, execution-binding absence, exact indexed
file digests, input/output conformance and absence of activation authority.

## Explicit CLI Profile source

`cli.profile.intent.v1.json` is a public-schema-valid `needs_resolution` authoring
intent. It selects a CLI Shell and the **explicit**, independent
`acceptance.cli.application` Application identity. That Application and
`acceptance.base` are unresolved required inputs, not claimed installed artifacts.
It requests one exact presentation edge. All artifact/revision/authority fields
remain null or empty. There is no generated source-release lock, active
ProfileLock, ResolvedPlan, ActivationRecord, approval or captured authority.

## Public authoring gaps and minimal reproduction

Only the permitted public docs, public schemas, generated SDK and official scaffold
CLI were consulted. No runtime/Host/Defaults/other-Pack private implementation,
existing test internals, APIs, credentials or live checkout were used.

1. Run the documented `python -m core_runtime.pack_scaffold` with `--template
   minimal` or `capability` in a fresh owned destination. Both generated v4
   declarations have empty Functions/contracts/executable variants. The capability
   example's `run(context,args)` is not documented as a production PackVM ABI.
2. `docs/pack-development-guide.md` describes PackVM integrity/admission but does
   not define a public invocation ABI or complete canonical self-reference digest
   construction. `build.py` uses an explicitly local source-digest convention;
   schema-valid digest fields and verified file bytes are **not** production
   capture validation. No executable sketch, runtime ABI or backend binding is declared for this
   component.
3. `cli_io_v1.schema.json` allows only `health`, `echo`, `profile.identity` commands.
   It has no arbitrary presentation or terminal-app launch command. The result
   projection here matches its stdout/stderr/status shape; it does not add an
   undeclared CLI command or call a private dispatcher.
4. `docs/pack_v4_minimal_profile.md` explicitly says the CLI Shell does not yet
   have a complete canonical CLI Application/frontend composition. The web map's
   frontend selection is not a public CLI Application authoring API. Merely changing
   Shell IDs or relabeling a Tauri Application would not establish a CLI product.

The generic missing surface is documented PackVM authoring ABI/digest generation
and an explicit CLI Application composition/presentation binding API. No
feature-specific core edit, legacy fallback or invented grant was added.

## Evidence boundary

| Check | Evidence |
| --- | --- |
| Useful offline success and input/output denial | 12 local source/schema tests |
| Normal Sandbox canonical declarations | Unmodified official declarative-only scaffold; empty variants |
| Explicit CLI Profile source selection | Unbound, unresolved public-schema-valid intent |
| Host canonical digest/capture compatibility | Not established |
| Signed admission/install/approval/activation | Not performed |
| Production Broker/PackVM invocation | Not performed |
| Actual CLI frontend replacement | Blocked on explicit public CLI Application composition |
| Host composition/removal/renamed IDs/unselected/unknown/conflict | Root-owned acceptance, not simulated here |

The Pack is unsigned, untrusted and disabled. Source provenance is non-normative.
Historical data and the live acceptance checkout are untouched.
