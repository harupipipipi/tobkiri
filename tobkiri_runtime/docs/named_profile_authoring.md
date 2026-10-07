# Public Named Profile source authoring

`core_runtime.profile_authoring.build_named_profile` compiles a **new** named
Profile with the existing formal Profile compiler. Inputs are a canonical
`profile_intent` document, an exact locked template bundle, and explicitly
supplied Normal Pack roots. No private runtime adapter is needed.

```python
from pathlib import Path
from core_runtime.profile_authoring import build_named_profile

build_named_profile(
    Path("my-new-profile-release"),
    template_bundle=Path("ecosystem/defaultspack/v4"),
    intent=my_profile_intent,
    additional_pack_roots=(Path("my-normal-pack"),),
)
```

The destination must be new. Template lock entries and all declared additional
Pack files are captured and verified. Duplicate Pack/Profile identities,
unknown selected Packs, unavailable or conflicting executable targets, stale
digests, traversal, and symlink input/output ancestors fail closed. The finite
capture limit is 4,096 files / 512 MiB total, with 64 MiB per input file.

Outputs include `authored.profile.intent.v1.json`, generated
`authored.profile.v5.json`, `authored.profile.lock.v5.json`,
`authored.release.provenance.json`, and the updated `bundle.lock.json`.
The source lock has `activation_authority: unbound`. It grants no admission,
Profile approval, runtime authority, execution, or Shell binary. Source Shell
metadata with `availability: build_required` remains unavailable at runtime;
use a verified packaged template for later runtime resolution. Published
default IDs are `defaults-basepack`, `shell.tauri.default`, and
`runtime.tauri.application.default`. No complete CLI Application is currently
published in this catalog.

## Select content belonging to a Normal Pack

The public `profile_content_projection_v1` schema also accepts a Pack origin:

```json
{
  "projection_id": "my.temporal.content",
  "kind": "profile_content",
  "artifact_root": "content",
  "source_pack_id": "my.temporal.pack",
  "source_artifact_digest": null,
  "content_digest": null
}
```

`artifact_root` is a canonical relative directory inside the Pack, never a
host path. Every file in that subtree must be declared in the Pack manifest
and match its artifact hash. The formal compiler requires the Pack to be in
the intent's resolved closure, captures its exact artifact digest, and pins
the subtree inventory and file count. Removing the selected Pack while keeping
its projection is an error. Omitting the projection leaves those assets out of
the selected content. Renaming requires rebuilding the Pack and updating its
explicit selection, not a private name-based hook.

At runtime, the normal Profile resolver checks selected closure pins and Host
admission before resolving the content. Captured Profile consumers resolve
only their selected projection IDs and reject content mutation or aliased
roots. Existing neutral `profile_projections/...` selections remain supported.

The existing prompt catalog reads `prompts/*.system.md` as plain Markdown; the
filename supplies the prompt ID. It has no YAML frontmatter requirement.
Profile selection makes these resources available to the prompt catalog.
`flows/*.flow.yaml` is the published legacy Flow format, whose function aliases
currently fail with `V4_OPERATION_UNAVAILABLE`; selection does not wire those
aliases to a Pack v4 executable. There is no public `functions/*.function.yaml`
registration format. Do not invent one.

The separate public Workflow v4 format is published at
`ecosystem/tobkiri_workflow_pack/schemas/workflow-definition.v4.schema.json`.
Its requests pin Contract ID, revision, Operation ID and Function principal.
Use the active Workflow operation palette to obtain the exact principal,
then submit the document through `definition.validate` /
`definition.compile-preview`. Compilation itself grants no execution authority.
For selected content, store canonical JSON at
`workflows/<definition-id>.workflow.v4.json`. The public read-only
`core_runtime.profile_workflow_catalog.selected_workflow_definitions()` reads
only the captured Profile's verified projections. Duplicate definition IDs
fail closed. `compile_selected_workflow(engine, definition_id)` passes that
selected document to the real Workflow engine's compile-preview against its
captured operation palette. It rejects unselected IDs and performs no store,
approval, attempt, or invocation operations. Supply the engine captured through
the normal Host Workflow provider; this helper does not create one or fabricate
its activation catalog. This gives Workflow source selection and binding,
while publication and execution still use their existing approval-aware route.
For independent source authoring, use
`workflows/<definition-id>.workflow.intent.v1.json` and the public
`tobkiri_protocol/schemas/profile_workflow_intent_v1.schema.json`.
It has `workflow_intent_api_version: io.tobkiri.profile-workflow-intent.v1`;
each request supplies `function_id` instead of `function_principal_id` and
still pins the exact Contract revision and Operation. The same selected
compiler matches all four fields against the actual captured operation
palette, requires exactly one candidate, obtains that candidate's principal,
then runs the formal Workflow compiler. It has no alias fallback, guessed
principal or lexical Pack-name dependency. Unknown/unselected/conflicting
bindings are errors. This intent is a source declaration, not an activation.
None of these source resources proves a reducer receives trusted lifecycle events. Prompt
selection likewise does not establish a typed internal model-input channel.
Use the public Flow/prompt format documentation to author actual catalog
records. Those runtime behaviors require separate verification.
