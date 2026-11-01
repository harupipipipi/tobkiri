# Pack Architecture Wave 1

Wave 1 makes pack boundaries mechanically enforceable. It does not move a
feature, storage owner, authority, or provider implementation.

## Gate

Run from the repository root:

```text
just pack-architecture
```

The scanner discovers pack roots from `ecosystem.json` without importing pack
code. It constructs the declared manifest dependency graph and source-level
edges for Python, TypeScript, JavaScript, and Dart. Diagnostics include an exact
source path, line, source owner, target owner, rule, and remediation guidance.
JSON and SARIF output are available for CI consumers.

The gate detects:

- imports between ecosystem pack roots;
- foreign pack-ID branches and sibling source/private-storage paths;
- product UI calls to implementation-specific API routes;
- kernel discovery of all installed packs rather than the effective pack set;
- process-global secret injection outside the credential boundary;
- domain/product branching in the kernel;
- unknown targets in the declared pack dependency graph.

## Shrink-only debt policy

`scripts/quality/pack_architecture_baseline.json` contains exact semantic edge
identities. Globs and wildcard patterns are rejected. Each identity includes a
stable AST fingerprint for Python or a normalized source fingerprint for the
other supported languages, so line relocation does not silently grow debt.
Every exception requires an owner, reason, introduced date, fix-by Wave, sunset
date, exact path, diagnostic line, fingerprint, source, target, and violation
category. Expired and resolved exceptions fail the gate; they must be removed
rather than left as dormant waivers.

When reviewing a baseline change, pass the previously approved file as
`--reference-baseline`. The candidate may delete entries only. New identities
and metadata mutation fail closed (diagnostic line relocation is allowed). A
removal followed by reintroduction has a new exact identity and is rejected.

CI reads the protected target-branch version as `--reference-baseline` once it
exists. During the one-time bootstrap, the reviewed candidate baseline must
exactly match the candidate scan, including schema, expiry, and stale-exception
checks. CI emits a warning that protected-base shrink-only enforcement starts
after that baseline lands on the target branch. Repository branch protection and
CODEOWNER enforcement remain required external controls for the baseline
authority.

No suppression is inferred from a package, directory, filename pattern, or
comment in source code.

## Ownership and rollback

The scanner owns no runtime data and grants no authority. The authoritative
owners and storage locations from Wave 0 remain unchanged. Its baseline is a
review record, not a runtime fallback. Rollback removes the CI step and scanner;
it does not migrate or rewrite application data.

## Data ownership matrix

| Resource | Authoritative pack | Schema version | Storage | Backup | Migration | Rollback | Retention | Export/import |
|---|---|---:|---|---|---|---|---|---|
| profiles | existing profile owner | existing | unchanged | unchanged | Wave 2 | Wave 2 plan | unchanged | unchanged |
| settings | existing settings owner | existing | unchanged | unchanged | later Wave | later Wave | unchanged | unchanged |
| secrets | core authority boundary | existing | unchanged | unchanged | none | unchanged | unchanged | unchanged |
| conversations | existing conversation owner | existing | unchanged | unchanged | Wave 7 | Wave 7 plan | unchanged | unchanged |
| messages | existing conversation owner | existing | unchanged | unchanged | Wave 7 | Wave 7 plan | unchanged | unchanged |
| prompts | defaultspack | existing | unchanged | unchanged | Wave 4 | Wave 4 plan | unchanged | unchanged |
| tools | defaultspack | existing | unchanged | unchanged | Wave 6 | Wave 6 plan | unchanged | unchanged |
| provider connections | defaultspack | existing | unchanged | unchanged | Wave 5 | Wave 5 plan | unchanged | unchanged |
| artifacts | existing artifact owner | existing | unchanged | unchanged | later Wave | per-Wave | unchanged | unchanged |
| schedules | defaultspack | existing | unchanged | unchanged | Wave 9 | Wave 9 plan | unchanged | unchanged |
| memory | defaultspack | existing | unchanged | unchanged | Wave 7 | Wave 7 plan | unchanged | unchanged |
| knowledge | defaultspack | existing | unchanged | unchanged | Wave 7 | Wave 7 plan | unchanged | unchanged |
| Company data | defaultspack | existing | unchanged | unchanged | Wave 9 | Wave 9 plan | unchanged | unchanged |
| approvals | core authority boundary | existing | unchanged | unchanged | none | unchanged | unchanged | unchanged |
| audit records | core authority boundary | existing | unchanged | unchanged | none | unchanged | unchanged | unchanged |
| architecture exceptions | repository architecture owner | 1 | versioned JSON | Git history | shrink-only deletion | restore prior reviewed file | until declared sunset | JSON |

Validation was not executed by the implementation agent.
Independent testing is required before merge.
