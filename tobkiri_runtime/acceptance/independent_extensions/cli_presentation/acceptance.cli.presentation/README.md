# acceptance.cli.presentation

Generated Tobkiri Pack `acceptance.cli.presentation` using the `minimal` profile.

The scaffold grants no authority. Its Tool is read-only, network- and
filesystem-denied, and the Pack remains untrusted until reviewed and signed.

## Workflow

1. Replace the example Activity, Skill, Tool, and function with domain logic.
2. Keep instruction-only behavior in `SKILL.md`; use a Tool only for an API,
   binary, stream, or custom execution boundary.
3. Validate manifests, test functions, inspect the exact permission request,
   then review and sign the Pack.
4. Never put secrets in the Pack or widen permissions as a side effect of a
   Skill.
