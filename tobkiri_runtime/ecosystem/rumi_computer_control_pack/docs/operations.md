# Operations

## Installation

Install through setup-pack selection as `rumi_computer_control_pack`.

Expected prerequisites:

- `defaultspack >=2.0.0`
- `rumi_default_tools_pack >=1.0.0`

The pack is optional, not recommended by default, and not eligible for automatic all-ok grants.

## Development Rules

- Keep this pack declarative.
- Do not add executable code, desktop drivers, functions, handlers, routes, stores, or flows.
- Do not include secrets, credentials, screenshots with sensitive payloads, tokens, or generated terminal logs containing secrets.
- Keep network posture none by default.
- Do not weaken defaultspack grants or runtime approval requirements.
- Update docs whenever profiles, prompts, presets, catalogs, specs, policies, overlap metadata, grants, or network posture change.

## Test Command

```bash
python -m pytest tobkiri_runtime/tests/test_rumi_computer_control_pack_contract.py -q
```

## Common Failure Modes

- A catalog, spec, or policy file becomes invalid YAML or JSON.
- Setup metadata omits overlap/defaultspack promotion fields.
- A prompt implies bypassing approval for keyboard, mouse, app switching, or remote terminal actions.
- A profile implies the pack owns the actual Computer Use or Chrome plugin.
- Network posture drifts away from none by default.

## Review Checklist

- Required docs exist.
- Required catalog/spec/policy/profile/prompt/preset/example assets exist.
- `ecosystem.json` and setup `pack.json` agree on ID, target, version, and dependencies.
- `base_pack_promotion.eligible` remains false.
- Overlap notes mention `defaultspack`, `rumi_browser_automation_pack`, `rumi_security_review_pack`, and `rumi_agent_services_pack`.
- Focused contract test passes.
