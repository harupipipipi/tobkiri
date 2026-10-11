# Example Echo — authored external Pack source

This directory is the smallest complete input for the bounded Pack authoring
builder (`core_runtime.pack_authoring`, CLI: `tobkiri-pack build`).  It is a
*source* pack: the canonical `pack.v4.json` / `contracts.v4.json` /
`executables.v4.json` / `artifact-index.v4.json` quartet is **generated** into
an explicit output directory by the build — it never belongs inside this
source tree, and the builder refuses a source root that contains it.

## Layout

- `pack-source.v1.json` — the authored Pack source document
  (`source_api_version: io.tobkiri.pack-source.v1`).
- `runtime/echo.py` — the PackVM implementation.  It exports
  `tobkiri_packvm_invoke(operation_id, payload) -> dict`, the production
  Python PackVM callable ABI executed inside the isolated guest interpreter.
- `README.md` — this file (indexed as an asset; arbitrary Pack files are
  preserved verbatim and pinned in the artifact index).

## Build

```bash
python -m scripts.tobkiri_pack build path/to/example.echo dist/example.echo
python -m scripts.tobkiri_pack build path/to/example.echo dist/example.echo --check
```

The output directory name must equal `pack_id` and must not already exist:
publication is a single directory rename of a fully validated staged copy —
the builder never overwrites.  The source tree is read-only during the build.
`--check` performs the same staged build and requires the existing output to
match byte-for-byte; it writes nothing.

Inside the staged build, the quartet is validated with `validate_document`
and `compile_pack_root` — the same gate the Host runs at admission.  Author
code is never imported or executed — the implementation is checked with
`ast.parse` only (must statically define a synchronous `tobkiri_packvm_invoke`
callable with the two production positional arguments; stdlib-only imports;
no relative imports).

## Source contract (`pack-source.v1.json`)

Required: `source_api_version`, `pack_id`, `version`, `display_name`,
`contracts[]`, `functions[]`.  Optional: `description`, `publisher_id`, and
requirement declarations (`capabilities`, `contract_dependencies`,
`pack_dependencies`, `network`, `secrets`, `approval_policy`,
`workspace_boundary`) which pass through to the manifest verbatim — declaring
a requirement grants nothing; admission policy belongs to the Host.

Each `contracts[]` entry: `contract_id` (canonical id ending `.v<N>`),
`version` (semver), `provider_id`, optional `status`/`security`, and
`operations[]` with `operation_id`, `input_schema`, `output_schema`,
optional `error_schema`, `effect_class`, `effect_ceiling`, `scope_semantics`,
`idempotency`, `timeout_default_ms`/`timeout_hard_max_ms`.

Each `functions[]` entry: `function_id`, `implementation_path` (a relative
`.py` path inside the Pack), `operations` (all declared by exactly one of the
pack's own contracts), optional `role` and `isolation`
(`pack_vm` or `dedicated_process`).

## Stated subset

The builder currently supports Normal Sandbox Packs only
(`kind: normal_sandbox`, `execution_boundary: sandbox`) whose functions
compile to `tobkiri.python-pack-v4` / `python3.13` / `on_demand` /
`sandbox.default.v1` variants — one implementation file per function, one
contract per function.  Host Extension packs, WASM/remote isolation, signing,
and admission are out of scope.  Static source checks bound what can be
proved without running author code; they cannot verify arbitrary dynamic
Python.  Compilation verifies artifact consistency; it is not native guest
execution.

The output must be outside the authored source tree. Source inventory is bounded
to 4096 entries, 32 directory levels, 16 MiB per file and 256 MiB total before
copying file content. The builder does not provide a repository-wide snapshot
of concurrent editor changes; the verified artifact digest identifies the
exact copied bytes. Use a quiescent source checkout for reproducible builds.
