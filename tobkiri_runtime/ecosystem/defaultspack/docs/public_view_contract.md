# Catalog-bound Pack views

The Application projects signed `frontend/contributions/*.json` sidecars from
the active Pack v4 closure. A descriptor uses `rumi.ui.contribution.v1`,
`kind: view`, `mode: declarative`, and a `view` validated against
`tobkiri_protocol/schemas/ui_view_v1.schema.json`.

`tobkiri.ui.view.v1` fixes six slots: `workspace_tab`, `sidebar`, `settings`,
`chat_header`, `composer_above`, and `composer_below`. The shipped Application
owns the `panel`, `status`, `entity_picker`, and `record_editor` renderers. A Pack cannot supply
URLs, HTML, CSS, SVG, callbacks, modules, permission hints, or approval controls.
This web adapter is not the renderer-neutral Surface Template protocol.

A data source or control names an exact `contribution_id`, `contract_id`, and
`operation_id` from the Host capability catalog. Optional Pack operation IDs
project as `pack.<pack_id>.<operation_id>`. A separate Surface Pack may reference
a public Logic Pack operation. Display declarations never add targets or grants.
Absent, disabled, unapproved, conflicting, or unready providers remain
unavailable. A missing source disables dependent controls.
Automatic data reads additionally require `read_only: true` captured by the Host
from the selected executable Operation's `pure` or `read` effect class. A view
cannot confer this evidence; user-triggered operations retain normal approval.

Controls accept constant `input`, own-property `input_bindings` from the source
snapshot, and an optional `value_key` for text, toggle, or choice input.
Use `input_bindings: {expected_revision: "revision"}` for an operation with CAS.
Choice controls require `options_path`, `id_path`, and `label_path`; duplicates,
unknown IDs, and entries with `disabled_path: true` cannot be submitted.
Failed writes preserve the source snapshot and restore the displayed selection.
Successful invocation refreshes the source rather than inventing local success.
Dirty text survives remote refreshes and failures. `Reload saved value` explicitly
discards that draft; a matching authoritative saved value clears it after a save.

`record_editor` renders at most 256 uniquely identified records, local search,
bounded columns, native edit dialogs, finite editable fields, and exact row actions.
Its save declaration combines constants, source/record/context bindings and a
`draft_key`. Drafts retain their opening CAS revision during remote refreshes and
failures. Nested field edits preserve untouched siblings. Navigation guards block
pending operations and require explicit discard of unsaved changes.

`context_bindings: {conversation_id: "conversation_id"}` and `turn_id` can
bind finite IDs from the current captured App state. Missing context fails
closed; conversation/turn changes unmount old requests. Profile, principal,
approval, activation, and private fields cannot be supplied by declarations or
client input. Host normalization injects `profile_id` only when declared by
the captured operation input schema.
Nested model-policy target `profile_id` values are domain data only at explicit
captured schema properties; they cannot override the Host execution Profile.

Generic operations require a finite `type: object` input schema with explicit
`properties` and `additionalProperties: false`. External schema references
are unsupported. The complete nested schema is validated before Broker dispatch;
the schema digest joins the catalog capture. Existing sensitive static adapters
and workspace-bound media normalization retain their stricter behavior.
Expiry, request replay/reconciliation, exact Provider/function/artifact identity,
Profile revision/Plan/activation checks, Authority, and audit remain Host-owned.

Frontend consumers obtain `catalog`, `activePlanHash`, and `capabilities` from
`useVerifiedFrontendHost()`; a null context offers no extensions.
`viewsForSlot()` returns validated declarations and a `CatalogViewReference`.
Store the complete reference with an extension tab. `FrontendViewSlot` accepts
that reference and renders unavailable when catalog/descriptor/capture changes,
including disable, uninstall, update, and rollback. The Host provider remounts
the Application on Profile changes.

Historical review: PR #1297/#1458 registry and collision principles, #1298
data-driven status, #1299 own-property choice normalization and rollback, and
#1324 exact operation binding are retained in the current v4 adapter. Old
filesystem registries, stale generated bundles, executable Pack modules, and
feature-specific App branches were omitted. PR #1376's renderer-neutral
architecture is retained as a separate contract boundary; resource input,
neutral renderer discovery/pinning, and full Surface Template pattern coverage
require follow-up and are not represented as completed by this adapter.
