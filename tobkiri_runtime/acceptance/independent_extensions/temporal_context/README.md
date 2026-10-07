# Independent temporal extension — Issue #1409

Scope: pure independent Normal Sandbox Pack authoring plus Flow/prompt draft.
Issue source: https://github.com/harupipipipi/tobkiri/issues/1409 (public captured
receipt supplied by the acceptance coordinator). **Issue #1409 is not complete.**
Defaults integration and durable completion persistence remain unimplemented.

## Public declarations

- Pack: `acceptance.temporal.context`, kind `normal_sandbox`, version `0.1.0`.
- Exact draft Contract: `acceptance.temporal.context.v1`; operation `temporal.reduce`;
  provider `acceptance.temporal.provider`; empty effect/capability ceilings.
- Pure authoring entrypoint: `source/temporal.py:run(context, args)` follows the
  official full-scaffold example authoring shape. It is not a documented PackVM ABI.
- `temporal.contract.draft.v4.json` is schema-valid, non-normative authoring source,
  not an installed catalog or proof of executable admission.
- Official minimal scaffold canonical artifacts remain unchanged. In particular
  `executables.v4.json` has no variants; the draft reducer is not falsely bound to
  an invented backend. Pure source is outside the canonical Pack root and is **not execution eligible**.
- `profile_projections/temporal/flow.source.json` and `prompt.md` explicitly replace
  turn-context reduction and temporal interpretation instructions. These are
  authoring drafts. They are not active Profile projections or a release lock.
  Removing that projection removes the proposed context step and temporal prompt.

## Useful reducer behavior

The input is a host-authenticated namespace, host-issued ordered lifecycle event,
and a caller-owned snapshot. Successful final `assistant.completed` replaces the
completion baseline. Tool completion, error, and cancellation consume event order
but never change it. A `user.received` event emits a separate internal context only
at 3600 seconds or more. Offset-aware timestamps are compared in UTC, including DST
transitions. No user text, tool invocation, network request, or filesystem write is
part of the operation. The returned snapshot is serializable for a future trusted
store port, but serializability is **not evidence of durable persistence**.

The composer must authenticate final-turn events, serialize events, and durably
commit each returned snapshot within the profile/conversation namespace. A caller
cannot be trusted merely because it supplies an `assistant.completed` label or a
larger sequence. The reducer has no authority to establish that trust.

## Minimal public-interface gap reproduction

Run from `tobkiri_runtime` with the existing Python environment (no installs):

```sh
PYTHONDONTWRITEBYTECODE=1 /path/to/existing/python -B -m core_runtime.pack_scaffold --help
PYTHONDONTWRITEBYTECODE=1 /path/to/existing/python -B -m core_runtime.pack_scaffold example.temporal --template minimal --output /new/empty/output
```

Observed official output: canonical `functions=[]`, `contracts=[]`, and executable
`variants=[]`; no `runtime/handler.py` despite the quick-start tree example.
`--template full` also has an empty canonical executable catalog and adds legacy
manifest/function examples, which this extension does not activate.

The four requested public docs and `generated/pack_sdk` supply no documented
trusted final-turn lifecycle subscription, durable per-conversation snapshot store,
internal model-context insertion port, or executable ABI/backend and digest
renderer interface usable by an independent author. Public Flow spec documents
function step syntax, but does not bind these missing v4 ports. Public Profile
content-projection schema declares immutable files; it cannot establish lifecycle
or context authority. Using Host/Defaults internals, forging activation, selecting
an in-process legacy fallback, or making up a PackVM ABI would conceal these gaps.

Required next public contracts: authenticated final lifecycle/user-receipt events;
namespace-bound atomic snapshot storage; separately typed internal-context model
input; an official sandbox executable authoring ABI/compiler. No broad foundation
change is attempted here. Root owns Broker/VM composition and denial tests.

## Verification

```sh
PYTHONDONTWRITEBYTECODE=1 /path/to/existing/python -B -m unittest discover -s acceptance/independent_extensions/temporal_context/tests -v
```

Seven deterministic reducer tests cover 3599/3600 seconds, hours/day/week, UTC offsets,
spring/fall DST, baseline reset, tool/error/cancellation distinctions, unknown kinds,
naive/invalid/backward time, stale/duplicate events, namespace isolation, snapshot
round-trip, and input immutability. These are pure authoring tests, **not** Broker,
PackVM, signed admission, activation, hidden AI prompt, or product acceptance.
