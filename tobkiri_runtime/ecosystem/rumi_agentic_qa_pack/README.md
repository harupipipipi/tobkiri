# Rumi Agentic QA Pack

Declarative end-to-end agent QA, scenario replay, acceptance, and regression triage pack.

## Owner Surfaces
  - scenario_replay
  - acceptance_matrix
  - regression_triage
  - cross_pack_quality_gates
  - qa_routing_matrix
  - acceptance_rubric
  - qa_evidence_ledger
  - replay_checklist
  - regression_triage_template

## Quality Assets
This pack now includes a QA routing matrix, acceptance rubric, scenario catalog, replay checklist, evidence ledger schema, and regression triage template. The assets require repeated specialist subagents: a scenario runner, adversarial tester, regression analyst, evidence reviewer, and pack handoff coordinator.

## Handoff Policy
This pack keeps its own scope narrow. When work crosses into code execution, connector delivery, browser operation, security, workspace artifacts, observability, or model scoring, it records the reason and hands off to the named pack in setup metadata.

## Required Secrets
None. This pack is declarative and does not bundle credentials, API keys, or executable network clients.

## Deterministic Offline Smoke Runner
The developer-side `scripts/agent_eval_harness.py` runner turns this pack's
scenario and evidence vocabulary into replayable local artifacts without
making the declarative pack executable. Its built-in smoke tier uses only a
registered in-process stub solver, needs no cloud keys or network, and covers
exact response, artifact diff, and tool/audit trace scoring. Failed tasks can
be replayed from `result.json` with the same finite fixtures.

```bash
python scripts/agent_eval_harness.py smoke --output-dir /tmp/tobkiri-evals
python scripts/agent_eval_harness.py replay /path/to/result.json \
  --output-dir /tmp/tobkiri-eval-replays
```

The runner intentionally cannot load arbitrary solver modules, start commands,
or invoke host tools. A future defaultspack, browser, mobile, or external-model
solver must enter through the canonical V4 Broker, Authority, approval, audit,
and isolation boundary; it must not be added as a direct CLI escape hatch.

## defaultspack Relationship
This pack depends on defaultspack and contributes routing metadata, handoff boundaries, and evidence requirements.

## Evidence
Every workflow must preserve enough evidence for a reviewer to understand the inputs, chosen handoff, and validation result.
