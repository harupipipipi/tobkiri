# Pack Architecture Wave 3

Wave 3 introduces a profile-scoped frontend host that understands only global
routes, renderers, regions, actions, data sources, settings, commands, design
tokens, localization, and schema-driven views. Product identities and backend
implementation pack IDs are contribution metadata, not host branches.

## Trust and isolation model

The default contribution mode is declarative. The host owns rendering and sends
actions/data queries through opaque global contract IDs bound to contribution,
owner pack, and resolved plan hash.

Executable UI uses an opaque-origin sandboxed iframe by default. Its RPC broker
accepts only declared `rumi.action.*` and `rumi.resource.*` contracts, verifies
the iframe window, one-time session nonce, contribution identity, owner, and
plan revision, and never sends local auth or bearer credentials into the frame.

Same-origin JavaScript is restricted to backend-verified `system` packs. The
backend validates owner pack, build identity, descriptor hash, module hash,
declared export, local path, effective profile revision, and resolved plan.
Client-supplied `trusted` flags are ignored.

## Failure isolation

Contribution schema failures, missing exports, hash mismatches, renderer
exceptions, route collisions, and priority ties produce diagnostics and generic
fallback surfaces. Quarantine identities include plan revision, owner pack, and
contribution ID. One failed contribution does not remove unrelated routes.
Pack-wide quarantine is reserved for provenance/path/hash integrity failures.

Route, command, setting, and contribution-ID collisions resolve by explicit
priority. Equal-priority ties fail closed. Removing a pack from the effective
set atomically removes its catalog entries without rebuilding the host.

## Accessibility and localization

Every descriptor declares an accessible name and keyboard support. Host-owned
fallbacks use status announcements. Isolated frames require a title. Localized
labels remain data and are rendered by the host rather than executable pack
code.

## Data ownership matrix

| Resource | Authoritative pack | Schema version | Storage | Backup | Migration | Rollback | Retention | Export/import |
|---|---|---:|---|---|---|---|---|---|
| profiles | core profile owner | resolved-profile v1 | existing profile store | exact copy | Wave 2 | Wave 2 | existing | JSON |
| settings | feature owner through settings contract | contribution v1 | owner storage | owner policy | per feature Wave | remove contribution | owner policy | contract-defined |
| secrets | core authority boundary | existing | secret store only | unchanged | none | unchanged | unchanged | prohibited in UI catalog |
| conversations | existing conversation owner | existing | unchanged | unchanged | Wave 7 | Wave 7 | unchanged | unchanged |
| messages | existing conversation owner | existing | unchanged | unchanged | Wave 7 | Wave 7 | unchanged | unchanged |
| prompts | defaultspack | existing | unchanged | unchanged | Wave 4 | Wave 4 | unchanged | unchanged |
| tools | defaultspack | existing | unchanged | unchanged | Wave 6 | Wave 6 | unchanged | unchanged |
| provider connections | defaultspack | existing | unchanged | unchanged | Wave 5 | Wave 5 | unchanged | unchanged |
| artifacts | existing artifact owner | existing | unchanged | unchanged | later Wave | later Wave | unchanged | unchanged |
| schedules | defaultspack | existing | unchanged | unchanged | Wave 9 | Wave 9 | unchanged | unchanged |
| memory | defaultspack | existing | unchanged | unchanged | Wave 7 | Wave 7 | unchanged | unchanged |
| knowledge | defaultspack | existing | unchanged | unchanged | Wave 7 | Wave 7 | unchanged | unchanged |
| Company data | defaultspack | existing | unchanged | unchanged | Wave 9 | Wave 9 | unchanged | unchanged |
| approvals | core authority boundary | existing | unchanged | unchanged | none | unchanged | unchanged | unchanged |
| audit records | core authority boundary | existing | unchanged | unchanged | none | unchanged | unchanged | unchanged |
| frontend catalog | core frontend host | contribution v1 | immutable plan projection | regenerated | descriptor adoption | omit/remove pack | plan lifetime | canonical JSON |
| UI feature state | contributing feature pack | feature schema | owner contract only | owner policy | per feature Wave | owner rollback | owner policy | owner contract |

Validation was not executed by the implementation agent.
Independent testing is required before merge.
