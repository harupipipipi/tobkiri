# Architecture

`rumi_prompt_studio_pack` is an optional authoritative service pack. Discovery
is manifest-only. After approval and resolved-profile selection, the host
registers process-isolated global providers without importing pack code.

Owned surfaces: authored prompt store, composition edge state, authoring,
versioning, diff, lint, compact, provider-free testbench, migration, rollback,
and the isolated Prompt Studio route.

Non-owned surfaces: model/provider routing, credentials, conversation storage,
memory/knowledge, tool authority, browser/desktop authority, and code edits.

The artifact manifest binds runtime and UI files to pack provenance. The active
resolved plan binds provider identity, pack content hash, profile revision,
permissions, and UI contribution identity. Defaultspack retains only finite
compatibility adapters and read-only Chat runtime projections.
