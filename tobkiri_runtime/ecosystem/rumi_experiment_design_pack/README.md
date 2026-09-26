# Rumi Experiment Design Pack

Declarative hypothesis, metric, sample-size, assignment, guardrail, instrumentation, and decision-record design pack.

This pack is local-first, declarative, and designed as a customization layer for Rumi. It adds domain-specific contracts, workflows, schemas, review gates, and handoff packets without adding executable runtime code.

## Provides

- hypothesis_contract
- metric_plan
- sample_size_plan
- assignment_plan
- guardrail_plan
- instrumentation_requirements
- decision_record
- experiment_readiness_packet

## Does Not Provide

- analytics query execution
- production rollout
- runtime telemetry collection
- model benchmark execution
- business decision execution
- feature flag mutation

## Result Claims

Design-only packets must not declare a winner, significance, lift, or metric movement. A decision record may summarize result claims only when result artifacts are supplied by the user or an owner pack; this pack never runs analytics queries or statistical calculations itself.

## Required Secrets

None. The pack declares no credential requirement and no network access by default.

## Defaultspack Promotion

Not eligible by default. Promotion requires the blockers below to be cleared with maintainer-reviewed evidence:

- does_not_run_analytics_queries
- does_not_claim_results_without_supplied_data
- rollout_handoff_owned_by_defaultspack
- telemetry_handoff_owned_by_defaultspack
- must_validate_guardrail_and_rollback_assumptions

## Handoff Model

The pack uses defaultspack as the base and hands adjacent runtime actions to explicit owner packs. Runtime tooling stays in `defaultspack`, and approval-aware downstream business execution routes through `rumi_operations_team_pack`.
