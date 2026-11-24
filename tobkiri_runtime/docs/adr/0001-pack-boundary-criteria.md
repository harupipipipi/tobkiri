# ADR 0001: Pack Boundary Assessment Criteria

- Status: Draft
- Decision scope: architecture review only
- Runtime authority: none
- Activation input: no

## Context

The current repository contains many directories represented as Packs. A directory
boundary is useful for organization, but it does not by itself prove that the
directory should be a separately installable, independently trusted runtime unit.
Treating the current topology as canonical would turn implementation history into
a public extension contract before ownership and isolation have been reviewed.

## Decision

We will keep a repository-level assessment of every runtime Pack manifest. The
assessment is evidence for a later architecture decision. It is not read by the
runtime, does not participate in activation or approval, and is not included in a
Pack signature or trust decision.

A Pack boundary is justified only when the review finds one or more substantive
reasons for it. Accepted assessment rows select one or more of these exact
criterion keys and explain, in their own words, how the selected evidence meets
them:

1. `independent_lifecycle`: independent release, upgrade, rollback, or
   deprecation lifecycle.
2. `trust_or_authority_boundary`: a distinct trust or authority boundary.
3. `meaningful_isolation`: process, VM, or fault isolation has meaningful
   security or reliability value.
4. `independently_migrated_state`: independently migrated durable state.
5. `third_party_replaceability`: a third party can genuinely replace or
   distribute it independently.

A normal internal responsibility split, an import boundary, or a UI section is
not sufficient by itself. Such code may ultimately be a module or resource inside
a larger Pack.

Unknown values remain explicit until evidence exists. An assessment row cannot be
accepted while its lifecycle owner, state owner, trust domain, execution mode,
canonical owner, or disposition remains unresolved. It must also have a non-empty
`boundary_criteria` selection, an `assessment_justification`, and content-bound
supporting evidence beyond the Pack manifest itself. Each evidence item records a
canonical repository-relative path and a SHA-256 digest. A reviewed row must
include exactly one such manifest evidence item, whose digest is independently
compared with the current manifest, plus at least one canonically distinct,
non-ADR supporting item. Canonically different spelling is not enough: evidence
items that resolve to the same filesystem identity, including case aliases or
hard links, are treated as the same file. The non-ADR classification also uses
the filesystem identities of all readable ADR files, so an ADR case alias or
hard link cannot be relabeled as external support. A changed manifest or evidence
file resets a reviewed row to `unreviewed` when the inventory is regenerated.

## Inventory contract

`docs/status/pack-boundary-assessment.v1.json` records the observed manifests and
their unresolved review state. `scripts/quality/check_pack_boundary_assessment.py`
checks that every production Pack manifest appears exactly once and that stale rows
are removed. It also rejects the assessment filename, schema version, and document
role in covered static source/config files under `tobkiri_runtime`,
`tobkiri_launcher`, and `.github`.

Adding, removing, or moving a Pack manifest therefore creates review drift without
making the assessment an executable catalog.

The non-consumption guard is deliberately static and bounded: it excludes
documentation, tests, vendor directories, and the exact generated-output prefix
`tobkiri_launcher/src-tauri/gen/`; it does not broadly exclude every directory
named `gen`. Exclusion uses the production file's lexical repository path before
any symlink target is resolved, so a production path cannot evade scanning by
linking into an excluded directory; a target outside the repository fails closed.
It cannot prove the absence of dynamic loading or external-process consumption.
It is a regression signal, not a runtime security control or a claim that all
possible consumers are enumerated.

## Consequences

- Existing Packs continue to load exactly as before.
- No `ecosystem.json` file changes and no approval hash is invalidated.
- The present topology, including any larger experimental v4 catalog, is not
  declared standard, canonical, or a supported third-party API by this ADR.
- Consolidation, compatibility aliases, and deletion require later accepted
  decisions with migration evidence.
