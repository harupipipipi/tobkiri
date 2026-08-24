# Selected sibling-pack template roots

Defaultspack discovers RumiTemplate files in this loader-controlled order:

1. `defaultspack/templates/` with `BUILTIN` trust.
2. `templates/` in selected sibling packs with `LOCAL` trust, ordered by pack ID.
3. `defaultspack/user_data/shared/templates/` with `USER` trust.
4. Roots listed in `RUMI_DEFAULTSPACK_TEMPLATE_ROOTS`, in environment order, with
   `USER` trust.

Sibling roots are derived only from the effective Pack set of the verified active
Pack v4 `ProfileLock`/`ResolvedPlan`. If no verified v4 activation is available,
no sibling templates load; discovery never falls back to startup state, legacy
selection JSON, approval registries, or a scan of installed Packs. Each selected
Pack is resolved as a direct child of an approved ecosystem root, and its
`pack.v4.json` `pack.id` and `pack.artifact_digest` must match the exact artifact
captured in the active plan. Every discovered `templates/**/template.json` must
also be listed in that manifest's `artifacts` with its exact SHA-256 digest;
undeclared, modified, shadowed, and stale-template bytes fail closed.

Trust and provenance are assigned after parsing. A template's own `trust_level`,
`metadata.source_pack_id`, `metadata.source_kind`, source root, or Pack artifact
digest cannot promote or misrepresent it. Selection only makes declarative
pieces discoverable; it does not grant permissions, approve executable code,
or bypass route registration, tool policy, workspace trust, sandboxing, or
Authority.

Catalog generations cover ordered source roots, the exact v4 effective Pack set,
trust tiers, v4 Pack-manifest hashes, template paths, and template content hashes.
Selection, deselection, Pack upgrades, template edits, and uninstall therefore
invalidate the snapshot without retaining stale projections.

Duplicate public template IDs fail closed. Diagnostics include both source paths
and loader-assigned source pack IDs so collisions can be traced to their packs.
