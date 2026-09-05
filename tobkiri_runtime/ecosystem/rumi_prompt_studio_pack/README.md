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
