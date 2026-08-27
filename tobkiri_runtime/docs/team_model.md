# Canonical Team model

`ecosystem/defaultspack/domain/team_model` owns the versioned organizational
contracts used by the Team Console and the Team Coordinator.  It is a pure
validation and snapshot boundary: it does not run a model, execute a tool, or
grant host authority.

## Organizational semantics

- A Team Definition is reusable configuration. `materialize_team` creates a
  persistent Team runtime record with an ID, generation, Definition hash, and
  immutable default-policy snapshot.
- A Manager is an ordinary Member referenced by `manager_member_id`.  The
  Coordinator is a separate enforcement service and is never materialized as a
  Member.
- Departments are flat v1 boundaries.  A Member has at most one primary
  Department; a Department Lead and Manager must be enabled Members of the
  same Team.
- Member Pools are routing-only candidate sets.  Pool selectors are validated
  against Member identity/state and cannot contain policy, capability,
  workspace, or authority fields.
- Members carry an exact `profile_id`, adopted revision, and content hash.
  Profile changes are visible through `plan_profile_update` and require an
  explicit materialization/adoption operation. Mutation helpers never trust a
  caller-supplied boolean: a Host-owned callback must verify and consume a
  one-shot approval token bound to the exact Team generation, plan/Profile
  hash, actor, and active-work strategy. Only the receipt ID and binding hash
  are retained; the token is never persisted.

## Policy and work snapshots

`resolve_effective_policy` applies typed layers from broadest to most specific:
security collections use intersection/deny-wins, limits use the strictest
bound, review/assurance use the strictest level, and mandatory checks are
unioned.  Preferences only select an allowed and available backend, unless an
explicit fallback is present.  Every decision is represented in the
machine-readable `trace`.

`create_assignment` resolves a Department/Pool to exact Member IDs and freezes
the complete effective policy, review identity, reviewed input revision, and
hash. `create_attempt` carries the same hash and Profile adoption provenance.
Later Team/Profile edits therefore cannot drift in-flight work.

Members have independent configuration (`enabled`, `disabled`, `archived`) and
availability (`offline`, `idle`, `assigned`, `running`, `blocked`, `paused`).
Members never become `done`; settlement returns availability to `idle` (or
`blocked`). Disabling prevents new Assignments while preserving active work.

The JSON Schemas under `schemas/team_model/` are the v1 wire contracts for
Definitions, policies, Assignments, and Execution Attempts.  `team_console_snapshot`
exposes generation, adopted Profile revisions/hashes, effective policies, and
resolution traces without exposing mutable internal references.
