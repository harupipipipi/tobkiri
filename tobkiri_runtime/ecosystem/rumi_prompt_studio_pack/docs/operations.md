# Operations

## Install

Select `rumi_prompt_studio_pack` and explicitly grant its read/write/migration
capabilities. It has no pack dependency, secret, or network requirement.

## Test

Run `python -m pytest -q tobkiri_runtime/tests/test_rumi_prompt_studio_pack_contract.py`.

Independent QA must run the focused runtime, contract, frontend, migration,
rollback, startup, shutdown, and pack-removal checks in the Wave 4 QA draft.

## Failure Modes

Missing permission, stale plan, stale body hash, replay, artifact mismatch,
process failure, changed migration source, or initialized target fails closed.

## Rollback And Revocation

Use `migration.rollback` with the recorded migration ID before removing the pack
when legacy restoration is required. Removing the pack from the effective set
removes its providers, route, API shims, and UI contribution. Backups remain
until explicit retention cleanup.

## Manual Review Points

Review profile/plan binding, permissions, artifact identity, migration source
hash, owner marker, redacted diagnostics, and rollback evidence.

## Isolated UI Accessibility Contract

The Prompt Studio UI remains an opaque, brokered Pack surface. It does not
import the host React application, read host credentials, select model routes,
or call tools directly. All resource and mutation requests continue through
the declared Pack v4 capability contracts.

The UI uses the following stable interaction patterns:

- Every input and select has a localized programmatic label and explicit
  helper/error relationships.
- Prompt filters are a pressed-button group. Prompt selection is a single
  selection listbox with roving focus and Arrow, Home, End, Enter, and Space
  keyboard support.
- The inspector uses the APG tablist/tab/tabpanel relationship with roving
  focus and Left, Right, Home, and End keyboard support.
- Concise progress and completion updates use one polite status region.
  Blocking errors use one assertive region and receive focus once. Full JSON
  operation payloads are not live-announced.
- Rollback names the prompt, version, creation time, and reason, then requires
  confirmation. Pending, failure, and settled states remain inside the modal;
  Cancel or Close restores focus to the originating version action.
- Selection, activation, editability, override, dirty, tokenizer, and safety
  state is repeated as text and never conveyed only by color or an icon.

Manual UI review must cover keyboard-only operation, screen-reader names and
states, English and Japanese copy, 200% zoom, large text, narrow/mobile layout,
long wrapping labels, forced colors, and reduced motion. Filtering, loading,
saving, rollback, prompt switching, and responsive layout changes must not
move focus unexpectedly.

## Version Ledger

Every prompt artifact release must update `ledgers/prompt_version_ledger.yaml` with prompt id, version, fixture ids, change summary, status, and non-overlap compatibility notes.
