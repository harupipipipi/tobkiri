# Surface Templates v1

A Surface owns inert declarations, Logic owns exact public Operations, and a
RendererPack supplies an API manifest selecting a finite implementation shipped
by the Application. No descriptor loads JavaScript, URLs, SVG, CSS, native code,
or another Pack's private modules. The public schemas and Python normalizer live
in `tobkiri_protocol/`; the frontend independently validates the same wire.

| Public contract | Purpose |
| --- | --- |
| `tobkiri.ui.surface-template.v1` | Ten semantic patterns and bound Operations |
| `tobkiri.ui.surface-renderer.v1`, API `1.0.0` | Finite renderer/pattern manifest |
| `tobkiri.ui.surface-intent.v1` | Exact template/node/intent/event and typed values |
| `tobkiri.ui.surface-outcome.v1` | Same identity, accepted/rejected/pending, optional text |
| `tobkiri.ui.surface-resource.v1` | Inert public ticket syntax; Host lookup required |

## Patterns

| Pattern | Events | Intent values |
| --- | --- | --- |
| content | activate | `{}` |
| problem | retry, dismiss | `{}` |
| notice | dismiss | `{}` |
| progress | cancel | `{}` |
| resource_input | submit | `{resource: exchanged ticket}` |
| choice | select | `{selected: declared choice IDs}` |
| form | submit | `{fields: declared typed fields}` |
| confirmation | confirm, decline | `{}` |
| collection | select | `{selected_id: source item ID}` |
| detail | close | `{}` |

Progress uses actual source values and a positive total. Bounds are 32 nodes,
eight intents per node, 16 form fields and 64 choices. Unknown fields, versions,
patterns, prototype paths, identity/binding collisions and recursive authority
hints fail closed. Confirmation is a Logic intent, not Host approval.

Use `view.renderer: "surface_template"` with `view.surface_template`. The shared
ten-pattern example is `tests/fixtures/surface_templates_v1/ten_patterns.json`.
Each intent supplies an exact catalog Operation and finite request. Source
bindings retain CAS values; context bindings accept only captured conversation/
turn IDs. The renderer injects `surface_intent` after checking collisions.
Logic validates it with `validate_surface_intent(value, immutable_template)` and
independently validates business data and authorization.

Outcomes must match the exact preceding intent. `outcome_path` can select the
typed object within a public response. A normal approval-required response stays
pending in the trusted Tobkiri approval surface; unconfirmed/rejected outcomes
preserve drafts. Rendering never invokes actions.

## Renderer capture

Optional `tobkiri_surface_renderer_pack` provides standard and compact manifests
with no executable Function or Host factory. Both interpret the unchanged
template into identical typed intents/outcomes. The Host first verifies the
selected artifact, compiled manifest, descriptor bytes and artifact index.
Only then may metadata select `semantic_standard` or `semantic_compact`.

Capture pins renderer contribution/owner, API, artifact, descriptor, build and
Host-issued bounded expiry into the existing Profile/revision/activation/Plan/
catalog view reference. Disable, uninstall, update, collision, rollback or expiry
makes the old capture unavailable. No builtin fallback or silent tab recapture
occurs. Both layouts use native accessible controls and static presentation that
also respects reduced-motion preferences.

## Resource port

Resource input uses the declared acquire/exchange refs through normal captured
action dispatch. Public requests are `{operation:"acquire",kind:"file"}` and
`{operation:"exchange",kind:"file",selection_id:"<opaque ticket>"}`; image/audio
are the other finite kinds. The response has exactly `version`, `selection_id`,
`kind`, `display_name`, `expires_at_ms`, and selected/exchanged `stage`.

The Host-owned `SurfaceResourcePort` stores one-use random tickets bound to the
server presentation owner/session, Profile/revision/activation/Plan/catalog/
security epoch, view/renderer identity, recipient Operation/Provider/Function/
artifact/schema and deadline-bound expiry. Ticket syntax grants nothing.

`SurfaceResourceActionAdapter` checks the authenticated origin and server scope.
A registered factory must verify its selected Function/server-injected Profile,
then forward only the finite public fields. It derives recipient/renderer pins
from captured verified records and retains the normal Broker authorization and
approval policy. No client `approved`, Profile, raw path or handle is accepted.

The explicit captured provider supplies acquisition, portable private exchange,
revocation and optional fresh-context consumption callbacks. A later exact
Logic RPC consumes once and privately rebinds to that invocation; existing
`ResourceHandleTable` handles remain request-local. Private paths, references,
principals and descriptors never enter the public response. Expiry, replay,
changed owner/Profile/recipient, errors and close races fail closed with cleanup.

No native picker or production acquisition provider is installed by this source.
An unbound acquisition/consume hook remains unavailable. There is no ambient
provider discovery or resource-handle exception in generic dispatch.

## Adoption and validation limits

Root adopts `ecosystem/tobkiri_surface_renderer_pack/integration-input.v1.json`
as a data-only optional Pack, without an executable-source entry or fabricated
Function. Regenerate current artifacts separately; historical migration receipts
remain unchanged. Select the renderer Pack explicitly before publishing a
Surface that references its contribution. Logic/resource Operations need their
own public dependencies/capabilities.

Contract, artifact projection, capture, resource fences and React source tests
do not prove installation or native execution. Native PackVM/official Computer
Use acceptance, alternate native rendering and a registered native acquisition
provider remain separate work. This source follows Draft #1501 and adapts
#1352/#1376 without copying old registries, bundles or execution evidence.
