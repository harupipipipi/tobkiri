# Operations

## Installation

Install `defaultspack` first, then select `rumi_run_lifecycle_pack` as a separate setup pack.

## Review Gates

- Run the contract test for this pack.
- Confirm `asset_index.yaml` and `ecosystem.json` list every pack file.
- Confirm `supports_all_ok` is false.
- Confirm adjacent actions remain handoffs.

## Failure Handling

If evidence, consent, approval, or ownership is missing, return a blocked packet or Handoff packet. Do not silently execute the next action.
