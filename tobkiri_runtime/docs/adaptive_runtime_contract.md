# Adaptive Runtime Contract

This document locks the one-PR contract for the adaptive Rumiai runtime. The
implementation is local-first and profile-scoped by default. It does not grant
authority from occupation, pack recommendations, model output, or client-supplied
approval flags.

## Scope

- First-run Operating Profile diagnosis and deterministic policy compilation.
- Pack-declared onboarding questions and safe recommendations.
- Profile and project scoped settings, events, context, skills, memory, and
  marketplace state.
- Bounded file reads, contextual code search, repository maps, and evidence
  bundles.
- Durable-event foundation: JSONL event log, outbox markers,
  subscription/ack/retry/DLQ state, state-only continuations, prepared actions,
  automation drafts, and emergency freeze.
- Failure-to-success Skill candidates, replay/canary metadata, rollback, memory
  conflict review, multi-agent leases, budgets, and Activity Center visibility.

## Safety Rules

- The compiler starts from system hard ceilings and only narrows permissions.
- Occupation and role data can change copy, ordering, and pack suggestions, but
  never widen side-effect authority.
- Pack recommendations are data-only suggestions. They cannot grant
  capabilities, request secrets, enable network, install packs, or override an
  explicit user answer.
- `Maximum Local Autonomy` means high autonomy inside managed local boundaries.
  It never implies external messages, production deploys, git push or merge,
  purchase, secret use, or network write.
- High-risk actions use prepared plans with exact-plan binding before commit;
  non-automation prepared commits queue follow-up work rather than bypassing
  host/file/terminal/git/browser approval paths.
- Webhook and automation secrets are stored as references; raw secret values are
  never returned to model-facing or UI-facing normal responses.
- Freeze mode blocks new mutating activity and outbound delivery while preserving
  read-only inspection and recovery data.
- Operating Profile draft and automation mutations require both the last
  confirmed resource revision and a caller-generated request ID. Stale revisions
  fail closed with `REVISION_CONFLICT`; replaying the same request and payload is
  idempotent, while reusing a request ID for different content is rejected.
- The control panel keeps backend-confirmed state distinct from pending, failed,
  offline, and conflicting edits. Unsaved onboarding and profile drafts are
  profile-scoped local data and are never treated as active policy.

## Foundation Boundaries

This PR is an adaptive runtime foundation, not a complete durable workflow
engine. Events, outbox items, subscriptions, retry/DLQ state, and continuations
are persisted local state with explicit resume markers. They do not provide
distributed exactly-once execution, cross-process work stealing, or automatic
post-approval host action replay. Those guarantees require a later continuation
runner that is wired through the same approval, workspace jail, local guard,
capability trust, and audit paths.

## API Surface

The defaultspack adaptive API is exposed through first-party function routes.
Routes are authenticated like other defaultspack control-panel APIs unless a
provider-specific webhook verifier explicitly handles the request first.

| Method | Path | Function |
| --- | --- | --- |
| `GET` | `/api/onboarding/status` | `adaptive_onboarding_status` |
| `GET` | `/api/onboarding/schema` | `adaptive_onboarding_schema` |
| `POST` | `/api/onboarding/answers/normalize` | `adaptive_onboarding_normalize` |
| `POST` | `/api/onboarding/compile` | `adaptive_onboarding_compile` |
| `POST` | `/api/onboarding/simulate` | `adaptive_onboarding_simulate` |
| `POST` | `/api/onboarding/apply` | `adaptive_onboarding_apply` |
| `POST` | `/api/onboarding/undo` | `adaptive_onboarding_undo` |
| `GET` | `/api/onboarding/history` | `adaptive_onboarding_history` |
| `POST` | `/api/onboarding/rediagnose` | `adaptive_onboarding_rediagnose` |
| `GET` | `/api/operating-profiles` | `adaptive_operating_profiles_list` |
| `GET` | `/api/operating-profiles/{id}` | `adaptive_operating_profiles_get` |
| `POST` | `/api/operating-profiles` | `adaptive_operating_profiles_create` |
| `PUT` | `/api/operating-profiles/{id}` | `adaptive_operating_profiles_update` |
| `POST` | `/api/operating-profiles/{id}/preview` | `adaptive_operating_profiles_preview` |
| `POST` | `/api/operating-profiles/{id}/activate` | `adaptive_operating_profiles_activate` |
| `GET` | `/api/packs/onboarding-recommendations` | `adaptive_pack_recommendations_list` |
| `POST` | `/api/packs/onboarding-recommendations/preview` | `adaptive_pack_recommendations_preview` |
| `GET` | `/api/activity-center` | `adaptive_activity_snapshot` |
| `POST` | `/api/activity-center/freeze` | `adaptive_freeze_set` |
| `PUT` | `/api/automations/{id}` | `adaptive_automation_update` |
| `POST` | `/api/context/file-read` | `adaptive_context_file_read` |
| `POST` | `/api/context/code-search` | `adaptive_context_code_search` |
| `GET` | `/api/context/repository-map` | `adaptive_context_repository_map` |
| `POST` | `/api/context/evidence` | `adaptive_context_evidence` |
| `POST` | `/api/prepared-actions/prepare` | `adaptive_prepared_action_prepare` |
| `POST` | `/api/prepared-actions/{id}/commit` | `adaptive_prepared_action_commit` |
| `POST` | `/api/prepared-actions/{id}/revoke` | `adaptive_prepared_action_revoke` |
| `POST` | `/api/events` | `adaptive_event_append` |
| `GET` | `/api/events` | `adaptive_event_list` |
| `POST` | `/api/events/{id}/ack` | `adaptive_event_ack` |
| `POST` | `/api/events/{id}/retry` | `adaptive_event_retry` |
| `POST` | `/api/events/{id}/dlq` | `adaptive_event_dlq` |
| `GET` | `/api/events/outbox` | `adaptive_event_outbox` |
| `POST` | `/api/events/replay` | `adaptive_event_replay` |
| `POST` | `/api/events/subscriptions` | `adaptive_event_subscribe` |
| `GET` | `/api/events/subscriptions` | `adaptive_event_subscription_list` |
| `POST` | `/api/continuations/resume` | `adaptive_continuation_resume` |
| `GET` | `/api/skills/candidates` | `adaptive_skill_candidates_list` |
| `POST` | `/api/skills/candidates/{id}/promote` | `adaptive_skill_candidate_promote` |
| `POST` | `/api/skills/candidates/{id}/rollback` | `adaptive_skill_candidate_rollback` |
| `GET` | `/api/memory/conflicts` | `adaptive_memory_conflicts_list` |
| `POST` | `/api/memory/conflicts/{id}/resolve` | `adaptive_memory_conflict_resolve` |
| `POST` | `/api/orchestration/leases/acquire` | `adaptive_lease_acquire` |
| `POST` | `/api/orchestration/leases/{id}/release` | `adaptive_lease_release` |

## Open PR Audit

This branch is based on `origin/master` at `b34da3e9`. Open PRs were audited for
contracts rather than stacked on directly:

| PR | Area | Decision |
| --- | --- | --- |
| `#362` | Tool selection, selector UX, permissions | Adopt permission and selector intent; rewrite through adaptive compiler and Activity Center. |
| `#369` | Managed sandbox and desktop seats | Adopt managed-local boundary language; do not depend on the branch. |
| `#350` | Subagent team workspace | Adopt DAG, lease, orphan, and reconciliation concepts; implement profile-scoped lease store. |
| `#354` | Prompt Workspace | Adopt prompt provenance and preview expectations; keep prompt editor independent. |
| `#328` | Context compaction and run snapshots | Adopt evidence and bounded context requirements; expose through context APIs. |
| `#355`, `#353` | External input and webhook automation | Adopt draft/review/activate/revoke lifecycle and secret references. |
| `#348` | Ambient scheduler triggers | Adopt scheduler visibility in Activity Center; do not auto-enable external delivery. |
| `#346` | Authority gate hardening | Preserve fail-closed authority behavior and no client-supplied approval trust. |
| `#224` | Profile tool permissions | Adopt profile/project ceiling model; compiler cannot widen parent policy. |
| `#221` | Browser/computer control | Preserve approval-aware control and artifact audit expectations. |
| `#203`, `#237` | Platform/runtime stack | Adopt compatibility language; do not depend on unmerged runtime branches. |

Other open PRs were considered out of scope for direct porting unless their
contracts intersected the adaptive runtime acceptance criteria.

## Validation Matrix

Required focused validation:

- `python -m pytest tests/test_operating_profile_adaptive.py -q`
- `python -m pytest tests/test_defaultspack_adaptive_api.py -q`
- `python -m pytest tests/test_defaultspack_coding_hardening.py tests/test_defaultspack_terminal_policy.py -q`
- `npm test`, `npm run lint`, and `npm run build` from
  `tobkiri_runtime/ecosystem/defaultspack/webapp/`
- Browser verification against the local defaultspack control panel route after
  the build assets are generated.
