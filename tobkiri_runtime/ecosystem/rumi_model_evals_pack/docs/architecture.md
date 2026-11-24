# Architecture

## Responsibility

Rumi Model Evals Pack defines how Rumi should describe, review, compare, and promote model and provider evidence. It owns eval suite shape, metrics, flakiness policy, provider smoke recipes, model fit matrices, cost/latency evidence templates, and defaultspack promotion gates.

It does not own provider execution or runtime routing mutation. Defaultspack remains the provider catalog and runtime core. This pack can produce evidence overlays that maintainers or higher-level packs may consume.

## Directory Map

- `ecosystem.json`: declarative component inventory and load-order hints.
- `catalog/capabilities.yaml`: local-first eval capability catalog.
- `catalog/provider_eval_catalog.json`: provider-evidence overlay schema and seed provider categories.
- `specs/layered_eval_contract.yaml`: contract, smoke, and e2e eval layers.
- `specs/metrics.schema.yaml`: pass@k, pass^k, flakiness, cost, and latency fields.
- `specs/model_fit_matrix.schema.yaml`: task-to-model fit evidence shape.
- `specs/base_pack_promotion_gates.yaml`: promotion gate requirements.
- `recipes/`: repeatable declarative eval recipes.
- `profiles/`, `prompts/`, `presets/`, `examples/`: role and workflow declarations.

## Runtime Contact Points

The pack expects these existing areas to provide execution if a runtime elects to run evals:

- `defaultspack`: provider catalog, model routing, approval policy, audit, and core runtime.
- `rumi_local_agent_pack`: local agent profile conventions.
- `rumi_agent_services_pack`: optional consumer for service-routing confidence.
- `rumi_code_ide_pack`: optional consumer for coding model fit and IDE smoke results.
- `rumi_data_analysis_pack`: optional consumer for analysis model fit, SQL, chart, and report evals.

## Eval Model

The layered evaluation model follows this progression:

1. Contract evals: validate model/provider interface shape, tool-call schema, refusal shape, streaming shape, and error envelopes.
2. Smoke evals: run tiny representative prompts per provider or route to detect broken keys, model ids, auth, latency spikes, and unsafe regressions.
3. E2E evals: run task workflows such as coding edits, research synthesis, data analysis, and agent handoffs with evidence capture.
4. Metric review: calculate pass@k, pass^k, flakiness, cost, latency, and regression deltas.
5. Fit matrix: map evidence to task families and routing suitability.
6. Promotion gates: decide whether a provider/model should be recommended, held, quarantined, or demoted.

## Local-First Boundary

Network is none by default. Provider smoke recipes may declare runtime requirements for provider keys, but keys are never embedded. Any provider call, remote routing test, or cost-bearing eval requires explicit runtime approval and externally supplied credentials.
