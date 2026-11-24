# Exact native Input entries

`Profile.startup.inputs` contains records with exactly these required fields:

```json
{"source_pack":"local.terminal-input","exact_input_view":"local.harness-chat.user-input@1.0","operation":"read"}
```

A Pack ID alone is rejected. Missing fields, duplicate fields, unknown fields,
major-only views and empty operation names are rejected. The operation above is
an explicit choice by this fixture's author; Core has no special `read` name,
first-operation selection or unique-operation inference.

The source Pack declares formal entries in optional, default-empty `inputs`:

```json
{"inputs":[{"exact_input_view":"local.harness-chat.user-input@1.0","operation":"read"}]}
```

An absent declaration enables no physical source entry. A declaration must match
an exact `ports.provides` version. Package admission checks locally owned WIT;
Profile compilation resolves external authoritative views from selected verified
Packs and checks the exact operation. A provider alias does not satisfy this
check. Empty metadata is omitted when serializing, preserving existing non-Input
manifest hashes. Adding an actual declaration changes its source Pack hash and
requires the normal new Profile revision and approval.

Compilation freezes `CompiledInputEntry` with its selection, source Pack hash,
Profile plan revision, actual WIT function, fixed structural arguments and exact
trigger-matching Flow keys in deterministic order. No required application
argument is supplied implicitly. Zero arguments and multiple explicitly empty
WIT record parameters are allowed. Manifest conversion resolves aliases before
checking that each record actually has zero fields; each parameter value is
validated with that exact WIT type. Nonempty records, including records with only
optional fields, do not receive implicit values.

Direct typed returns are allowed; `result<T, E>` is not mandatory. Zero-result,
empty-record and result-success-without-payload signatures are also allowed and
remain distinct in the frozen `WitFunction`. The compiler constructs no ingress
payload and does not normalize these cases into an object or null. The existing
raw codec distinguishes an empty record `{}`, a payloadless result `{"tag":"ok"}`
and a result carrying an empty record `{"tag":"ok","val":{}}`. Result errors are
read failures and must never become successful input delivery.

The historical generic host facade flattens some unit cases; it is not acceptance
for this contract. Generic zero-result representation, native codec forwarding
and error-branch runtime proof remain pending under the current Wasm source
freeze. No ingress-only fallback is added here. Stream/callback lifetime and
forwarding are likewise unimplemented; current explicit denial does not complete
that compatibility work. Runtime payload values require validation with the
selected actual WIT signature and common codec.

Core must match `read_input(source, view, operation)` against this frozen record,
then pass the same descriptor through startup registration, physical read and
NativeIngressPolicy. The descriptor is topology, not current permission,
approval, a source-read receipt or an execution grant. Current source approval,
Profile revision/epoch and every downstream grant remain separate checks.
User/UI ingress has its own explicit default-deny policy and cannot forge native
physical-source evidence.

Source fixtures and tests are migrated. Rust compilation and focused runtime
proof are pending the root Cargo lease; no M2, Pi, live-provider or legacy parity
receipt is replaced by these source changes. Python producer checkpoint 002 is
frozen and unaffected.
