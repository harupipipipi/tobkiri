# Interfaces

## Required Secrets

None.

## Required Network

None by default.

## Permissions

- `profile.prompt.author.read`
- `profile.prompt.author.write` (approval required)
- `profile.prompt.author.migrate` (approval required)

## Inputs

Active profile ID, declared prompt operations, optimistic revision hashes, and
fixed-root migration inventories.

## Outputs

Typed prompt resources and actions through:

- `rumi.resource.prompt.studio.v1`
- `rumi.action.prompt.author.v1`
- `rumi.action.prompt.version.v1`
- `rumi.action.prompt.test.v1`
- `rumi.action.prompt.migrate.v1`

## Does Not Provide

Model benchmarking/routing, provider credentials, persistent memory,
conversation storage, tool/API creation, code edits, or authority approval.
