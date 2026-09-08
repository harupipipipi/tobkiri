# Application presentation ownership

Defaultspack owns its UI control definitions, built-in UI layout and default
command declarations. Other Packs must request that data, not import the
application's Python implementation or discover its files.

The normal PackVM Function `defaultspack.application-presentation` provides
`tobkiri.resource.application.presentation.v1` operation
`defaultspack.presentation.read`. Its single-file implementation is generated
from the existing application-owned pure sources and JSON definitions with
`python -B scripts/generate_application_presentation.py`. `--check` verifies
freshness. Host and relative imports are rejected during generation. The ABI
performs no Host state access, provider discovery or command execution.

## Captured consumers

- `rumi_command_protocol_pack.catalog.read` requests the sealed command
  presentation. It retains its own execution-availability and approval policy:
  only its known high-risk adapter references can become available when the
  captured adapter exists, and they remain approval-required.
- `tobkiri.ui.settings.read` and `tobkiri.ui.catalog.read` obtain model display
  options through the model owner, then request application control definitions
  through the presentation contract. No direct application-code fallback exists.
- Three exact caller-to-presentation edges are candidates in the source Defaults
  intent. They have not been activated in the existing native Profile.

The command Pack owns `schemas/command-protocol-v1.schema.json` and validates its
outgoing catalog against that local, artifact-pinned schema. The application
retains a generated compatibility copy; the presentation generator checks or
refreshes that copy. No schema supplied by an untrusted VM controls Host output
validation.

## Disclosure policy and remaining state boundary

UI definitions returned by the VM cannot grant access to saved settings fields.
The settings Pack's `runtime/public-settings-fields.v1.json` is an independently
reviewed, artifact-pinned Host disclosure policy. It is deliberately not refreshed
automatically from application templates. New controls do not expose saved values
until their public fields are approved in that policy. Password/key/token controls
remain filtered even when a public field identifier is present.

The settings reader still imports the legacy application shared-settings reader
for the existing `defaultspack/shared/frontend_settings.json` snapshot. This is
the remaining cross-Pack implementation dependency. Its ownership/compatibility
transition is not solved by this change; do not replace it with a foreign-path
read or hide it behind an import alias. No state migration, repair or write is
performed by these read contracts.

The legacy transition must cover more than the presentation reader:

- `domain/frontend/registry.py` updates the shared document and can migrate
  keyboard-navigation fields while reading; it requests corrupt-data preservation
  from the store rather than opening or replacing settings files itself.
- `domain/ai_client/model_runtime_settings.py` updates the `models` namespace
  and uses revision/idempotency-aware `mutate_state` for model mutations.
- `domain/frontend/command_protocol.py` reads registered command declarations
  from the same shared document.
- `domain/frontend_settings_store.py` currently owns file locking, atomic
  replacement, whole-document/state revisions and mutation receipts together.
- Optional settings reads in AIClient, chat requests, tool recommendation and
  permissions, trigger decisions, chat debug logging and LINE addressing/output
  policy now use `domain/frontend_settings.py` and the owner's `read_snapshot()`.
  These nine reads no longer parse the file independently. Their existing
  unreadable/corrupt-preference fallback remains local compatibility behavior;
  it must not absorb future captured-contract authorization failures. They do
  not recover from backup or create locks/directories/diagnostics.
- The model service no longer caches resolved values using filesystem size and
  modification time. Those attributes can remain identical across different
  settings documents and are not an owner revision. Each resolution now reads
  the current owner snapshot and credential availability without retaining a
  second global settings cache. Captured owner binding is still pending.
  The command registry derives its separate event/offline database paths
  from the settings path; those databases are not settings-owner state and must
  retain an explicit independent location when the settings client loses paths.

A transition that relocates only public-value reads would split these owners
and can return stale defaults while old writers continue updating the original
document. Reads, writes, recovery and migration need a single explicit cutover
and captured authorization; copying a snapshot without fencing the old writers
does not establish that cutover. This inventory is a design constraint, not an
implemented migration or permission to modify the live shared file.

The current store now fails closed if the platform locking module is missing
or the OS refuses the lock: recovery, update callbacks and mutation receipts
cannot run without acquisition. Windows retains its bounded contention retry;
POSIX lock errors propagate. Unlock errors are reported and the file handle is
closed rather than presenting a known release failure as success. A release
failure after writing is not proof of rollback and must not trigger blind replay.
The unused duplicate lock helpers have been removed. This fixes a prerequisite
for safe ownership work, not the cross-Pack import or an ownership cutover.

The existing in-process lock and advisory file lock do not fence older binaries
after a future ownership change. A same-path transfer must establish that old
writers are stopped or no longer authorized; introducing a new owner/read API
alone does not provide that evidence. No live cutover is authorized by source
development or isolated tests. A reviewed typed operation must also replace
legacy callable transforms: arbitrary Python callbacks cannot cross the Pack
boundary as a write capability.

The local data-only document/state CAS port is not a public PackVM API. A full
legacy document can contain private fields, arbitrary extension namespaces and
internal receipts. Exporting that document to the application and accepting an
arbitrary replacement would bypass the disclosure boundary above, even with
revision checks. The captured settings owner must expose reviewed namespace/
field projections and mutations, keep control metadata local, and merge changes
under its own transaction. Local CAS tests do not establish those permissions.

UI recovery now calls `read(preserve_corrupt=True)`. Unrecoverable bytes are
preserved by the store under the same transaction lock, with a full-digest
filename and the original permissions. Existing differing backup bytes are not
overwritten. Ordinary `read()` and read-only `read_snapshot()` do not create
diagnostic copies; the latter still cannot repair state. The registry's duplicate
raw writer and unused replacement helper were removed, and both valid JSON and
diagnostic bytes use one store-owned atomic writer. This consolidates I/O but
does not yet migrate ownership or authorize a new live settings operation.

The DeepThink state read now derives the value and logical revision from one
store snapshot. A second read (or an independently cached value paired with a
new revision) could describe a state that never existed during concurrent
updates. The shared pure revision reader rejects negative and non-integer
revision values consistently with the mutation path.

Retained mutation receipts now bind their result's exact state reference as
well as the request fingerprint. A reused key cannot return another resource's
result. A corrupt receipt collection or selected receipt rejects mutation
instead of discarding replay evidence and running the callback again. This
does not repair the record, change the existing 64-receipt retention limit,
provide indefinite replay protection or migrate the storage owner.

The isolated ABI and captured-consumer tests are not native startup, real AI
conversation, live Profile activation, streaming, cancellation or DMG acceptance.
