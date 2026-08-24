# Rumi Prompt Studio Pack

Optional local-first prompt authoring, composition preferences, linting,
testbench, migration, versioning, rollback, and isolated UI pack.
Published revisions are tracked in the pack's version ledger.

## Provides

This pack is the sole owner of authored prompts and prompt composition edge
preferences for a selected profile. It exposes typed global contracts and runs
through a verified single-request subprocess. Its UI is an opaque sandbox that
uses the host capability broker and receives no bearer credential.

## Does Not Provide

This pack does not provide model benchmarking, model routing, persistent memory storage,
tool creation, API creation, or code edits. Those surfaces are routed
through setup-pack overlap policy and explicit handoff packets; the
`defaultspack` host remains the compatibility destination for existing prompt
consumers during migration.

## Handoff

Model execution, tool/API creation, memory persistence, and code mutation are
handed to their owning contracts; Prompt Studio only emits reviewed prompt
definitions and composition preferences.

## Required Secrets

None.

## Network

None by default.

## Storage

`user_data/packs/rumi_prompt_studio_pack/profiles/<profile>/` contains the
atomic store, owner marker, locks, and migration backups.

The isolated Tobkiri Prompt Studio UI also keeps recoverable tab-local drafts
in the iframe browsing-context history state, which remains available without
granting the opaque sandbox Web Storage access. Each draft key includes the
exact profile, prompt, model context, conversation context, and persisted body
revision. Drafts never cross those boundaries and expire after 30 days.
Successful Save or an explicit Discard removes the matching recovery record.

## Unsaved-change safety

Prompt, profile, model, conversation, refresh, back/route, and rollback
transitions stop while the editor is dirty. The review dialog identifies the
affected context and offers Save (or Save as override for read-only sources),
Discard, and Cancel. Browser refresh and close use the browser's native
before-unload confirmation after persisting the scoped recovery record.

Optimistic-write conflicts keep the draft in place and offer Compare, Reload
latest, or explicit Overwrite. Rollback has a separate consequence review and
does not reload the editor until the version mutation succeeds. Request
generation fencing prevents a late load response from replacing a newer
prompt or model context.
