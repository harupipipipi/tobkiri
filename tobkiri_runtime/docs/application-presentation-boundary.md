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
  keyboard-navigation fields while reading; corrupt-data backup also writes.
- `domain/ai_client/model_runtime_settings.py` updates the `models` namespace
  and uses revision/idempotency-aware `mutate_state` for model mutations.
- `domain/frontend/command_protocol.py` reads registered command declarations
  from the same shared document.
- `domain/frontend_settings_store.py` currently owns file locking, atomic
  replacement, whole-document/state revisions and mutation receipts together.

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

The isolated ABI and captured-consumer tests are not native startup, real AI
conversation, live Profile activation, streaming, cancellation or DMG acceptance.
