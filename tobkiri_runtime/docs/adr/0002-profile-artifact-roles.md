# ADR 0002: Profile Artifact Roles

- Status: Draft
- Decision scope: role and trust-boundary vocabulary
- Runtime behavior change: none

## Context

A Profile can be represented at several stages between human intent and an
executable release. Calling every representation a normative Profile obscures
which document is editable, which one is resolved, and which one may authorize
runtime activation.

The compatibility branch does not contain the experimental Profile v4 pipeline.
This ADR deliberately does not add it, activate it, or change a loader.

## Roles

### Human intent

The author-edited request. It may be unresolved and tracked in source control. It
is non-normative and has no activation authority.

### Resolved source lock

A generated record of exact source-release inputs, pins, and digests. It binds a
release candidate for reproduction and review, but it is not runtime activation
authority.

### Platform executable projection

A derived compatibility input for one platform or runtime. It may be consumed by
packaging or a loader, but it is not by itself the normative root of a release. An
unresolved projection or one generated from `working-tree` provenance must not
claim to be normative.

### Release provenance

Evidence that binds generator inputs and outputs. Provenance supports audit and
reproduction; it does not grant authority merely by existing.

### Signed bundle manifest

The intended normative root of a release. A future accepted release contract must
bind the executable projection, resolved locks, provenance, and executable
artifacts that it authorizes. This ADR does not introduce or require Developer ID
signing for macOS distribution.

### Evidence

Tests, fixtures, reports, and migration proofs. Evidence is always non-normative
and is never a runtime activation input.

## Invariants

- Human intent may remain unresolved, but cannot authorize activation.
- A source lock and release provenance cannot silently become runtime authority.
- `needs_resolution` and normative authority are incompatible.
- `working-tree` provenance and normative authority are incompatible.
- Compatibility projections must not be mistaken for the signed release root.
- Profile v4 remains absent and disabled on the compatibility branch.

Enforcement in the experimental full-v4 branch should be local to the Profile
projection validator. Changing generic provenance helpers or activation loaders is
outside this behavior-neutral phase.
