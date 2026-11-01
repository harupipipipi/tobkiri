# Operations

## Install

Install this optional setup pack after defaultspack and rumi_local_agent_pack are available.

Setup metadata lives at `ecosystem/setup_pack/rumi_model_evals_pack/pack.json`.

## Develop

When changing this pack:

- Keep files under `ecosystem/rumi_model_evals_pack/` and setup metadata under `ecosystem/setup_pack/rumi_model_evals_pack/pack.json`.
- Keep runtime code limited to the artifact-bound catalog reader,
  non-executing plan builder, and deterministic local scorer. Do not add remote
  eval runners, shell scripts, notebooks, SQL files, routes, or provider
  adapters.
- Keep network default as `none`.
- Do not include provider keys, credentials, private endpoints, or account-specific payloads.
- Treat defaultspack provider catalog as authoritative; use overlay and gate metadata only.
- Keep pass@k, pass^k, flakiness, cost, and latency definitions stable across recipes.

## Test

Run the focused contract test:

```bash
python -m pytest tobkiri_runtime/tests/test_rumi_model_evals_pack_contract.py
```

Manual checks:

- JSON files parse.
- YAML files parse.
- Required docs/assets exist.
- PackSelector discovers setup metadata and dependencies.
- Overlap and defaultspack promotion metadata are explicit.
- Runtime code remains covered by `artifact-manifest.json` and contains no
  provider-call or credential-loading path.

## Common Breakages

- A provider smoke recipe accidentally includes a real key or private endpoint.
- A preset describes running provider calls without approval.
- A promotion gate mutates defaultspack instead of producing advisory evidence.
- pass@k and pass^k are mixed without recording sample count and independence assumptions.
- Flakiness is reported without retry count or time window.

## Review Checklist

- Contract, smoke, and e2e layers are distinct.
- Provider smoke recipes state runtime credentials are external.
- Cost and latency evidence include units and capture window.
- Fit matrix entries include task family, evidence source, confidence, and caveats.
- Defaultspack promotion remains gated and advisory.
