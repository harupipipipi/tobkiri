# Public boundary for Issue #1409 and named selection

`author_profile.render_intent` authors a new named unresolved Profile from supplied
public Base/Shell/catalog identities, an explicit provider selection, exact requested
operation edge, and `profile_projections/temporal` content selection. It never emits
a handwritten source-release lock, activation record, or approval. Source selection
is not proof that a Flow executes or that a prompt becomes model input.

Sources read:

- `docs/profile-artifact-generation.md`: official source-release generation and
  unbound activation authority; generic bundle generation needs verified catalog.
- Public `profile_intent_v1` / `profile_content_projection_v1` JSON schemas:
  explicit projection root and unresolved digest fields; not lifecycle or IO hooks.
- `docs/flow_spec.md`: function alias step fields; does not publish a v4 request
  target binding or trustworthy turn lifecycle subscription for that format.
- `docs/prompt_authoring.md` / `docs/prompt_workspace.md`: passive rules, stable IDs,
  source precedence and approved reads/writes; no independent v4 model input port.
- Public Workflow Pack `workflow-definition.v4.schema.json`: a different finite
  Workflow definition with exact Contract revision/Operation/principal requests.
  This may document standalone Workflow calls; it is not the Defaults turn Flow.
- Published neutral `profile_projections/local-agent/prompts/planner.system.md`
  sample: Markdown prompt resources. It provides no executable authority.

Precise remaining requirements:

1. A formally selected trusted event source for final assistant task/turn completion
   and next user receipt, with host time, namespace, final outcome, event identity,
   and ordered/deduplicated delivery. Tool events cannot substitute completion.
2. A namespace-bound durable snapshot Contract with atomic concurrency/replay rules,
   allowing baseline replacement only by authenticated successful final completion.
3. A separately typed internal-context model input path. Runtime-computed previous
   completion, current receipt, and elapsed duration must not modify user text.
4. A public selected Flow catalog format and operation binding that routes the
   reducer through the approved v4 Broker while retaining existing tool policy.
5. A typed internal model context consumer alongside the now-documented prompt
   catalog (`prompts/*.system.md`, filename ID, plain Markdown, no frontmatter).
   Correct prompt selection still does not establish hidden AI context delivery.

The public `docs/named_profile_authoring.md` API is now used successfully by this
worker. It resolves named source selection and exact Pack-origin content capture.
Selected/unselected/removal/renamed public compiler cases pass, including expected
failure when a removed Pack retains its projection. That source selection is
separate from execution and product acceptance. Catalog resource paths now follow
published `prompts/*.md` and `flows/*.flow.yaml`; no unpublished function catalog
record or approved alias binding is fabricated. The executable
Pack authoring ABI/compiler is already published and validated; it is no longer a
missing API. Issue #1409 remains incomplete until the lifecycle, store, and model
context integrations actually exist and pass acceptance.

Public documentation now explicitly rejects `functions/*.function.yaml`
registration and confirms legacy Flow aliases fail `V4_OPERATION_UNAVAILABLE`.
Those are no longer proposed integration routes. A schema-valid Workflow v4
source intent is captured in this Pack's `content/workflows` subtree, with
exact Function ID, Contract ID/revision and Operation. The public selected compiler
obtains a principal only from exactly one matching actual captured palette entry. The public selected-Workflow lookup and real compile-preview route
are documented; Root owns Host-captured engine verification. No fake engine,
active palette, authority, or Flow execution evidence is authored here.
