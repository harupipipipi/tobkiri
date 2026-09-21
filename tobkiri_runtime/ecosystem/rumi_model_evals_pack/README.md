# Rumi Model Evals Pack

Rumi Model Evals Pack is an optional, local-first evaluation catalog and pure
scoring runtime. It defines provider smoke tests, layered contract/smoke/e2e
eval suites, pass@k and pass^k metrics, flakiness tracking, model fit matrices,
cost/latency evidence, and promotion gates.

The verified runtime reads the immutable catalog, prepares approval-required
operation descriptors, and scores observations supplied by an external runner.
It does not execute providers or grant its own authority.

## Provides

- Eval profiles for suite design, provider smoke checks, metric review, routing fit, and promotion gate review.
- Prompt contracts for interpreting eval evidence without overstating model quality.
- Presets for contract evals, provider smoke evals, coding-model routing, agent-service routing, and data-analysis model fit.
- Catalogs and specs for layered eval suites, metrics, provider overlays, fit matrices, and promotion gates.
- Examples for MiniMax/OpenCode Zen smoke checks, pass@k reporting, flakiness review, and defaultspack promotion evidence.
- Global catalog, plan, and deterministic scoring contracts.

## Does Not Provide

- No provider invocation, test execution, network runner, notebook, or provider adapter.
- No provider keys, tokens, endpoints with credentials, or account-specific payloads.
- No network access by default.
- No mutation of defaultspack provider catalog entries. This pack supplies evidence overlays and advisory promotion gates.

## Documentation

Start with [docs/README.md](docs/README.md), then read [docs/architecture.md](docs/architecture.md), [docs/interfaces.md](docs/interfaces.md), and [docs/operations.md](docs/operations.md).

Validation was not executed by the implementation agent. Independent testing
is required before merge, including catalog integrity, incomplete evidence,
approval handoff, deterministic scoring, and pack removal.
