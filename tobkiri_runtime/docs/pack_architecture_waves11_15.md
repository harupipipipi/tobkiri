# Pack Architecture Waves 11–15

This document records the final Pack-architecture delivery after Waves 0–10.
The wave numbers describe contract boundaries, not separate releases: all
adopted work ships in one change set.

## Wave 11: Command Protocol v1 cutover

- `GET /api/command-protocol/v1/catalog` is the authoritative resolved catalog.
- All 55 shipped commands have a canonical Pack-qualified identity, a
  presentation contract, a registered operation kind, and an availability
  result.
- Invocation dispatches through `CommandOperationRegistry`; it no longer calls
  the legacy registry's generic `execute` dispatcher.
- Legacy `GET /api/ui/commands` is a deprecated read-only projection. Legacy
  POST execution fails closed and points callers to v1.
- Web and desktop-shell behavior uses only v1 invocation. The mobile client
  exposes the same catalog, invoke, resume, event-query, and offline contracts.
- CI regenerates and checks a complete command inventory so a missing handler,
  unresolved execution, duplicate identity, error diagnostic, or secret-shaped
  catalog field fails the build.

## Wave 12: Approval-safe execution and progress

- High-risk commit, push, terminal, patch, and restore commands create a real
  approval request and stop with `approval_required`.
- Resume requires a signed, argument-bound, Pack-bound,
  conversation-bound, expiring, one-shot execution token.
- Client booleans such as `approved: true` never authorize execution.
- Invocation IDs are idempotent. Reusing one for different arguments returns
  `INVOCATION_CONFLICT`.
- SQLite-backed invocation events allocate monotonic sequence numbers, redact
  secret keys and recognizable secret values, and close terminal streams.
- The SSE route supports `Last-Event-ID`, named events, keepalives, bounded
  long polling, and terminal replay. A JSON event query remains available to
  clients that cannot consume SSE.

## Wave 13: Offline desired-state operations

- Only `state_mutation` commands expressed as explicit `set(value)` may queue.
- Host actions, Pack operations, toggles, approval-required work, and
  secret-bearing payloads are rejected.
- Every queued operation carries an idempotency key and expected revision.
- Reusing a key for different content conflicts. Replay records completed,
  conflicted, cancelled, or failed terminal state.
- Offline state is a local request to reconcile with authoritative backend
  state; it is never treated as authority itself.

## Wave 14: Pack SDK and signed distribution

- `tobkiri-pack` provides `init`, `generate`, `validate`, `sign`, `verify`, and
  `inspect`.
- `init --profile codex|hermes|complete|auto|minimal` generates strict
  Activity v1, Skill v2, Tool v3, `AGENTS.md`, and least-authority function
  templates. `add activity|skill|tool` extends a Pack without overwriting
  files. See [Agent Pack templates](agent_pack_templates.md).
- Generation is deterministic from the Pack v3, global-contract, and command
  protocol schemas, producing a hash-indexed contract inventory plus Python,
  TypeScript, and Dart IDs.
- Validation uses strict Draft 2020-12 schemas and rejects unknown
  security-sensitive manifest fields.
- Signed Pack manifests use Ed25519, bind publisher identity and compatibility
  metadata, hash every Pack file, reject symlinks and file-set drift, and honor
  revoked key IDs.
- Activation fails closed when a declared signed manifest lacks a host
  publisher trust store or does not verify. The trust store must remain outside
  the Pack root.
- A signature proves integrity and publisher identity only. It never grants a
  capability, approval, or host authority.

## Wave 15: Cutover gates and operational acceptance

The following are release gates:

1. Generated SDK and the command inventory have no drift.
2. Every resolved command is available and registered.
3. Legacy execution cannot be reached through the compatibility HTTP route.
4. Approval tokens cannot be replayed or used with different arguments,
   Packs, or conversations.
5. Tampered, unsigned-when-declared, extra-file, symlinked, publisher-mismatched,
   or revoked-key Packs fail verification.
6. Invocation replay, SSE resume, offline conflicts, frontend tests,
   type-checking, build, and Pack architecture scans pass.

## Explicit architecture gates

CRDT synchronization and general event sourcing are not introduced here.
Neither is needed by an accepted product requirement, and either would change
conflict semantics, retention, privacy, and operational recovery. Adoption
requires a separate ADR with concrete multi-writer requirements, deletion and
retention rules, migration/rollback design, and load-test evidence. The
invocation event log is a bounded progress/reconnect ledger, not an
event-sourced domain model.

## Rollback

Rollback is performed by reverting the single delivery commit on the PR head
branch. Existing Wave 0–10 artifacts remain valid. Signed-manifest enforcement
is Host-owned: once an install record has `signature_required=true`, removing
or changing the Pack-side declaration fails closed. A rollback must update the
Host install record through the trusted installer after verifying the target
artifact; Pack files cannot downgrade their own policy. Unsigned local Packs
are limited to an explicitly enabled developer-mode install path and never
inherit publisher trust or capabilities.
