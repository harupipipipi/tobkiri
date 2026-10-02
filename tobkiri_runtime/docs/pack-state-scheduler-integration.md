# Scheduler, secondary model policy, and Team state integration

This source lane starts at PR #1496 commit
`51bbc8180732f7f6f525256d0ab8a0f3dc878899`. Global catalogs, packaged bundles,
source closure manifests, and migration receipts are integrated separately.
The source changes alone do not establish production admission or GUI evidence.

## Public operations (version 1.0.0)

| Contract | Exact operation | Function / factory module |
| --- | --- | --- |
| `tobkiri.resource.schedule.v1` | `rumi_schedule_store_pack.schedule-resource` | `rumi_schedule_store_pack.schedule-store.resource`, `ecosystem.rumi_schedule_store_pack.runtime.store` |
| `tobkiri.action.schedule.v1` | `rumi_schedule_store_pack.schedule-action` | `rumi_schedule_store_pack.schedule-store.action`, same module |
| `tobkiri.resource.scheduler.v1` | `rumi_scheduler_runtime_pack.scheduler-resource` | `rumi_scheduler_runtime_pack.scheduler.status`, `ecosystem.rumi_scheduler_runtime_pack.runtime.scheduler` |
| `tobkiri.action.scheduler.v1` | `rumi_scheduler_runtime_pack.scheduler-control` | `rumi_scheduler_runtime_pack.scheduler.control`, same module |
| `tobkiri.action.scheduler.clock.v1` | `rumi_scheduler_runtime_pack.scheduler-clock-control` | `rumi_scheduler_runtime_pack.scheduler.clock`, `ecosystem.rumi_scheduler_runtime_pack.runtime.clock` |
| `tobkiri.action.job.v1` | `rumi_job_action_broker_pack.job-action-broker` | `rumi_job_action_broker_pack.job-action.broker`, `ecosystem.rumi_job_action_broker_pack.runtime.broker` |
| `tobkiri.resource.ai.model.profile.v1` | `rumi_model_registry_pack.model-profile-resource` | `rumi_model_registry_pack.model-registry.profile`, `ecosystem.rumi_model_registry_pack.runtime.process` |
| `tobkiri.resource.company.v1` | `rumi_company_state_store_pack.company-state-resource` | `rumi_company_state_store_pack.company-state.resource`, `ecosystem.rumi_company_state_store_pack.runtime.store` |
| `tobkiri.action.company.state.v1` | `rumi_company_state_store_pack.company-state-action` | `rumi_company_state_store_pack.company-state.action`, same module |

Each module exports `HOST_PROVIDER_FACTORY`, keyed by the exact Function IDs.
New scheduler/store/job variants require Host-extension admission, like the
existing model registry owner; they must not retain the old `read` effect class
for mutations. Store action, scheduler control, and job broker require a
privileged effect ceiling. Resources remain read-only (model owner retains its
existing effect ceiling). The Host Broker owns approval, current activation,
exact request digest, scope, and audit; no client `approved` field is accepted.

Scheduler runtime declares only schedule read/action and job action dependencies.
Job broker declares only the public job adapter dependency. Neither imports a
target Pack implementation. Persistent owner modules depend on standard-library
modules plus shared `core_runtime.paths`, `profile_workspace`, `runtime_locks`,
and captured Host-provider interfaces. Model policy additionally depends on the
pure `tobkiri_protocol.secondary_model_policy_v1` module and the already-declared
public provider-registry resource for current connection availability.

## Scheduler wire

All requests have an `operation` discriminator; `profile_id`, when supplied,
must equal the captured Profile. Resource operations are `list`, `get`, `due`.
`get` returns `{schedule: record_or_null}`. `list` returns `{version, profile_id,
revision, schedules}`; `due` takes `now_ms`, `limit`, optional `schedule_id`.
Action operations are `create`, `update`, `delete`, `pause`, `resume`, `cancel`,
`claim`, `complete`, `fail`. All require `schedule_id` and a nonnegative integer
`expected_revision`. Create adds `name`, `action_id`, object `payload`,
`next_run_at_ms`, `interval_ms`, `max_attempts`. Update adds object `updates`
containing only those editable fields. Claim adds `lease_id` and
`lease_expires_at_ms`; complete/fail require the exact `lease_id`, optional
bounded `error`. Revision/CAS failure leaves the store unchanged.

Runtime operations are `status` (resource) and `tick`, `trigger`, `stop` (control).
Tick accepts optional `now_ms` and `limit`; trigger requires `schedule_id`.
Clock and lease decisions are injectable for isolated execution tests.

The job broker receives `dispatch`, `cancel`, or `status`, plus `action_id`,
`idempotency_key`, optional object `payload`, `schedule_id`, and `lease_id`.
It selects only captured public `tobkiri.action.job.adapter.v2` providers. Each
selected adapter's exact operation supports `describe` and returns
`{action_ids: [registered IDs]}`. Exactly one matching provider is required.
Dispatch/cancel/status use that selected exact operation with the same envelope
and `operation` discriminator. Shared adapter schema must be identical across
providers: an object with `operation` enum `describe|dispatch|cancel|status`,
optional `profile_id`, `action_id`, object `payload`, `idempotency_key`,
`schedule_id`, `lease_id`; each owner validates stricter per-operation fields.

The strict adapter wire is a distinct v2 Contract at version 2.0.0, with a
consumer range `>=2.0.0 <3.0.0`. Existing v1 adapter providers and their historical
schemas are preserved; the canonical broker selects v2 only.

`accepted`/`running` remains pending. After restart, an expired pending schedule
polls the broker's durable entry and adapter status before deciding to complete
or retry. No timer produces fabricated success. Missing/ambiguous adapters fail
closed; sanitized exception types reach schedule state. Running schedules cannot
be edited or paused; cancel invalidates the exact lease.

## Finite Host clock source

The separately selected clock Function declares a fixed public scheduler tick
payload and a 60-second Host pump. `status`, `arm`, and `disarm` are its exact
public operations; `arm` requires an explicit `duration_ms` from 60 seconds to
one day. It is unarmed by default. A signed selected clock-to-scheduler-control
edge and exact current recurring Grant are required; Shell sessions and expired
provider invocations are never reused. The generic driver stores registration
expiry, generation and wall deadline, reconstructs monotonic due time on restart,
collapses missed periods and fences disabled/stale/expired callbacks. The wake
kernel gate is consumed by the Host driver and is never a Broker execution lease;
actual execution must use a fresh canonical audited Broker request.

An in-flight durable claim at restart retains its finite registration intent but
reports `reconciliation_required: true` and `armed: false`. The current Broker
has no durable clock receipt lookup, so claim expiry cannot authorize replay of
a possibly dispatched wake. A new approved arm or explicit disarm resolves this
state. Unclaimed registrations still catch up normally. A closed old capture
cannot disable the new capture's persisted intent. Shutdown fences every driver
and attempts every adapter and Broker cleanup even if another source fails;
failed cleanup remains retryable with the dispatch session permanently fenced.

The explicit adapter scope is `host_process_only`: it runs while the Host process
lives and does not wake a sleeping Mac. Missing adapters, bindings, grants or
production hookup report unarmed/unavailable. The source driver and injected
tests do not establish an armed production registration.

## Model-policy wire

Use the model resource exact operation with `operation: "policy.resolve"` and:

```json
{
  "model_policy": {
    "mode": "inherit_conversation",
    "required_capabilities": ["model.tool_calling"],
    "on_unavailable": "fail"
  },
  "thinking_policy": {"mode": "inherit_conversation"},
  "context": {
    "conversation_model_profile_id": "saved-profile",
    "conversation_thinking_level": "high"
  }
}
```

Model modes: `inherit_conversation`, `fixed` (required `profile_id`), `snapshot`
(optional `snapshot_profile_id` or persisted `snapshot_receipt`). Context may
also contain `turn_*` and `global_*` model/thinking fields. Thinking modes:
`inherit_conversation`, `fixed` (required supported `level`), `model_default`.
Fallback requires both `on_unavailable: "fallback"` and `fallback_profile_id`.
There is no implicit fallback. Snapshot consumers persist the full initial
receipt; subsequent calls pass it as `snapshot_receipt`, retaining model and
thinking while still validating current availability and capabilities.

Existing gateway generation and streaming operations retain their exact IDs and
native `{identifier: saved_profile_or_alias}` input. The shared model resource
schema admits that wire as well as discriminator-based list/get/resolve/policy
calls. Current provider connections are read without secret values; saved profile
metadata `requires_credentials` checks a live opaque credential handle. The
resolution receipt never contains that handle.

The receipt version is `tobkiri.secondary-model-policy.v1`; it records requested
policies, resolved profile, model ID, thinking level/source/translation, source
and profile record revision, resolution source, fallback reason, and a stable
error code on failed resolution. Provider keys are never returned. Missing
credentials, disabled connections, unsupported tool calling, or unsupported
thinking fail closed before model invocation. This lane provides resolution;
callers must use the returned receipt in their actual model call and preserve it.

## Team adoption and migration

PR #1378's canonical SQLite WAL store and isolated regression suite are adopted
with per-Team/entity CAS, fenced leases, durable idempotency, stable timeline
pagination, and private migration backups/quarantine. Compatibility adapters
read the exact Team revision rather than a profile-wide revision; retryable
conflicts remain retryable rather than becoming failed work.

The public resource supports `list`, `get`, and `timeline`. The action requires
the exact `company_id` and its `expected_revision`. `task.claim` additionally
requires `task_id`, `member_id`, `idempotency_key`, and `lease_duration_ms`;
`lease.renew` requires the same task/member and the exact `fencing_token`.
Changes to another Team do not advance this Team's CAS revision.

Legacy migration is explicitly opt-in with `migrate_legacy=True` in isolated
fixtures. Production owner construction with unactivated legacy JSON/SQLite
raises `TEAM_MIGRATION_REQUIRED` before creating a new store. This lane never
opens, migrates, or modifies actual user data and never rewrites migration
evidence to claim an unexecuted Pack is GREEN.

## Previous PR mapping

- #1312: adopt inherit/fixed/snapshot semantics and auditable requested/resolved
  receipts through the model owner. Omit old Defaultspack-internal cross-Pack
  calls, auto-route ambiguity, generated bundles, and old receipts.
- #807: retain scheduled-task UX requirements; omit the unrelated historical
  Cloudflare/mobile stack and old `/api/agent/schedules` binding. Current public
  scheduler execution and state transitions are implemented first. UI work and
  actual application operations must be reported separately.
- #1378: adopt owner source and tests; modify automatic legacy activation into
  explicit opt-in and update only affected compatibility revision callers.

## Verification boundary

Pure owner/wire tests use `pytest --noconftest` while the global packaged source
manifest is intentionally stale in the isolated lane. They exercise actual
SQLite/filesystem transitions and dispatch through a public fake adapter; they
do not establish a packaged Profile, real GUI operation, or real model result.
Integrated packaged tests and actual Pack UI testing remain required after the
root regenerates source-authoritative artifacts.

The generic application-owned `RecordEditorView` renders strict public catalog
descriptors. It retains the original CAS snapshot throughout editing, preserves
drafts on approval/error/conflict, and clears a saved draft only after an
authoritative refresh contains the same update. Navigation guard confirmation
does not erase the draft until navigation actually unmounts the view. This source
module alone does not admit the old isolated scheduler iframe or establish GUI
evidence.
