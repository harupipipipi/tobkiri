# Interfaces

## Inputs

- Local artifacts supplied by the user or by an adjacent owner pack.
- Schema-bound records listed in `ecosystem.json`.
- Evidence IDs, source spans, and explicit uncertainty notes.

## Outputs

- Evidence-linked drafts.
- Review checklist results.
- Handoff packets with owner pack, reason, and artifact path.
- Decision records with explicit `result_claim` and `analysis_boundary` fields.

## Decision Claim Boundary

Design-only records must set `result_claim.status` to `not_claimed`. Claims about winners, significance, lift, or metric movement require supplied result artifacts and must not come from queries executed by this pack.

## Handoff Owners

- `defaultspack`: Runs analytics queries, telemetry-adjacent tooling, rollout prep, feature-flag handoff packets, and model-benchmark handoff packets outside this pack.
- `rumi_operations_team_pack`: Owns approval-aware downstream business execution when a design packet is ready for operating review.

## Required Secrets

None.

## Does Not Provide

- analytics query execution
- production rollout
- runtime telemetry collection
- model benchmark execution
- business decision execution
- feature flag mutation
