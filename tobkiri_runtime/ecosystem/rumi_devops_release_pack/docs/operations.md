# Operations

## Installation

Install through setup-pack selection as `rumi_devops_release_pack`.

Expected prerequisites:

- `defaultspack >=2.0.0`
- `rumi_default_tools_pack >=1.0.0`

The pack is optional, not recommended by default, and not eligible for automatic all-ok grants.

## Development Rules

- Keep this pack declarative.
- Do not add executable code, handlers, functions, routes, stores, or new flows.
- Do not include secrets, credentials, environment values, deploy keys, or generated logs with sensitive content.
- Keep network posture none by default.
- Update docs whenever profiles, prompts, presets, catalogs, overlap metadata, grants, or network posture change.

## Test Command

```bash
python -m pytest tobkiri_runtime/tests/test_rumi_devops_release_pack_contract.py -q
```

## Common Failure Modes

- A catalog file becomes invalid YAML or JSON.
- Setup-pack metadata points at the wrong target pack ID.
- A prompt implies live deployment, rollback, or remote inspection without approval.
- A release example includes credentials, tokens, or environment values.
- Overlap notes blur code-edit ownership with `rumi_code_ide_pack`.

## Review Checklist

- Required docs exist.
- `ecosystem.json` and setup `pack.json` agree on ID, target, version, and dependencies.
- `base_pack_promotion.eligible` remains false.
- `overlap_policy` mentions defaultspack, code pack, and agent services pack boundaries.
- Focused contract test passes.
