# Host Extension inventory guide

The Host Extension inventory is a read-only, nonauthoritative review aid. It
joins canonical Pack and executable documents to tracked Profile reachability
and advisory Python AST import evidence. It never imports Pack code, executes an
operation, grants runtime admission, or recommends an automatic move.

The generated artifacts are:

- `docs/host-extension-inventory.md`: compressed facts for every Host Extension.

JSON is generated on demand and intentionally has no tracked schema or stability
contract until a real consumer exists. Pass `--format json` for stdout or
`--json-output <path>` with `--write` or `--check` for temporary inspection.

## Refresh and diff check

Run from `tobkiri_runtime/`:

```bash
python ops/quality/scan_host_extension_inventory.py --write
python ops/quality/scan_host_extension_inventory.py --check
python ops/quality/scan_host_extension_inventory.py --write \
  --json-output /tmp/host-extension-inventory.v1.json
python -m pytest tests/test_host_extension_inventory.py -q
```

By default, `--check` compares only the compact tracked Markdown report. It also
compares a JSON artifact when `--json-output` is explicit. Console output is one
compressed status line; full deterministic diagnostics remain available in the
generated JSON and are grouped by code in the Markdown summary.

## Facts and manual review

The inventory has no integration-candidate or automatic-admission concept. It
records only `ai_runtime_signal`, `tool_runtime_signal`, or `none`, canonical
schema validity, explicit Profile inclusion and graph reachability, advisory AST
I/O imports, and manual-review reasons.

Every AI or Tool Runtime signal carries `runtime_signal_requires_human_review`.
Schema failures, missing reachability, graph edges to omitted Packs, incomplete
implementation evidence, and scanner diagnostics add more reasons. No reason or
observed factory assignment authorizes integration.

## Static-analysis limits

The scanner recognizes direct Python imports for common filesystem, network,
process, database, stream, and host-OS modules. Imports show that I/O is
available to an implementation; they do not prove that every operation executes
it. Dynamic imports, reflection, runtime aliasing, monkeypatching, native code,
and non-Python implementations cannot be resolved reliably and are diagnosed or
left for human review. A `HOST_PROVIDER_FACTORY` assignment is advisory AST
evidence only; it does not prove conformance or admission. Tracked Profile
reachability is not proof of production activation; runtime-added edges and
activation authority are intentionally outside this report.

Profile discovery is product-neutral and limited to tracked files below
`ecosystem/`. Every `*.profile.intent.v1.json` is selected as authoritative.
A same-name `*.profile.v4.json` compatibility projection is not read when the
intent exists; otherwise it is recorded explicitly as a compatibility fallback.
Duplicate selected `profile_id` values are diagnosed and every conflicting
definition is excluded from aggregate reachability.

The current repository discovery selects exactly one tracked authoritative intent,
`defaults`. Every Profile input, Pack manifest, and executable catalog is validated
with the canonical Tobkiri protocol validator. A requested graph edge qualifies
only when the same Profile explicitly includes the operation's Pack; an edge to an
omitted Pack is retained as diagnostic evidence but is not aggregate reachability.

Manifest, executable, implementation, and profile symlinks are never followed.
Malformed JSON, canonical schema failures, Python parse failures, unsafe paths,
and missing references are emitted as stable diagnostics so partial evidence does
not silently become a clean result.
