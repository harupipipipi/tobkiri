# Managed Sandbox Runtime Scope Snapshot

This repo-local document is adapted from the handoff plan at
`/tmp/rumiai-handoff.aM5MrE/rumiai-managed-runtime-handoff/IMPLEMENTATION_PLAN.md`.
It is no longer the acceptance contract for PR #369. The current PR scope is a
foundation: defaultspack runtime/provider wiring, Linux native desktop seats,
optional Docker-backed non-desktop sandboxes, and managed Ubuntu providers that
can create/provision a guest only after the platform launcher is already
available.

Out of scope for this PR and tracked as follow-up implementation work:

- bundling or silently installing Lima on macOS,
- enabling WSL, handling Windows elevation/reboot resume, or installing WSL when
  `wsl.exe` is absent,
- claiming per-sandbox filesystem/process isolation inside a shared Lima/WSL
  Ubuntu guest,
- automatic primary-device cutover in Continuity handoff flows.

Where the historical plan below describes stronger end-state behavior, the
runtime UI, provider diagnostics, and overview docs take precedence for PR #369.

# Rumi Managed Sandbox Runtime + Desktop Seats - Single PR Implementation Plan

## 0. PR identity

- **Branch:** `feature/managed-sandbox-runtime`
- **PR title:** `[runtime] Add cross-platform managed sandboxes and desktop seats`
- **Base:** current `master`
- **Delivery rule:** one PR for the current foundation scope; bundled launcher
  bootstrap, stronger guest isolation, and full cross-platform setup remain
  follow-up implementation work.
- **Primary user surface:** defaultspack Rumi DP. Add **Desktops** directly below **Kanban** in both the full history sidebar and compact rail.

This PR turns the current sandbox placeholders into a single managed execution platform used by:

1. pack isolation,
2. coding sandboxes,
3. Ubuntu desktop seats for Computer Use,
4. browser sandboxes,
5. future ephemeral tool sandboxes.

For PR #369, Rumi does not install Docker Desktop and does not silently install
or bundle host launchers. If Lima or `wsl.exe` is unavailable, provider doctor
fails closed and gives an explicit launcher requirement. Once the launcher is
available, Rumi can create/provision the managed Ubuntu guest from its runtime UI.
OS-owned consent cannot be bypassed; later Windows/macOS launcher bootstrap and
reboot-resume flows must keep those prompts explicit.

---

## 1. Current repository facts and migration strategy

Do not build a second sandbox system. Extend and consolidate the existing one.

### Existing execution owner

`tobkiri_runtime/ecosystem/defaultspack/backend/sandbox/sandbox_manager.py`

It already provides:

- persisted sandbox registry,
- create/destroy/status/list lifecycle,
- screenshot/click/type/scroll dispatch to a backend,
- failure-closed behavior when a backend is unavailable.

Today it is still a prototype:

- lifecycle is only `ready/destroyed/error`,
- default screenshot is a deterministic 2×2 fallback PNG,
- `GUISandbox` only records synthetic events,
- no provider doctor/ensure/update/uninstall,
- no process/VM/WSL lifecycle,
- no exec/filesystem/port API,
- no control lease,
- no template/policy binding.

### Existing Docker-only command path

`tobkiri_runtime/ecosystem/defaultspack/domain/tool/sandbox_tools.py`

Today it runs `docker` directly. It must become a compatibility adapter over the managed `SandboxManager`; it must no longer own Docker invocation. Existing function IDs remain stable.

The current string-command conversion to `sh -lc` must not be available to frontend/API requests. Legacy trusted internal callers may be migrated through an explicit compatibility parser, with audit records and the existing terminal risk classifier.

### Existing declarative sandbox pack

`tobkiri_runtime/ecosystem/rumi_sandbox_runtime_pack/`

This pack intentionally owns policies, profiles, prompts, presets, examples, evidence, and handoff contracts—not execution. Keep that boundary. Add templates and provider capability contracts here, while defaultspack remains the execution authority.

### Existing RumiTemplate platform

Use the existing template kernel and its trust model. Do not add an unrelated YAML loader under a new top-level runtime directory.

- Builtin sandbox templates are shipped as trusted RumiTemplate documents.
- Templates describe desired capabilities and policy.
- Backend/runtime remains authoritative for execution, mounts, secrets, network, approvals, and limits.
- USER templates cannot add backend entrypoints or arbitrary executable code.

### Existing Computer Use work

Draft PR #221 contains useful Linux Xvfb/Openbox logic but is based on an old master and includes unrelated LINE/Gemma changes. Do not merge the PR wholesale. Port only the relevant Linux X11 virtual-session implementation and tests onto this branch, adapting it to the provider and guest-agent contracts below.

---

## 2. Product contract

### 2.1 One managed runtime, several consumers

```text
Rumi DP / AI functions / pack runner / coding runner
                    │
                    ▼
          Sandbox Application Service
                    │
                    ▼
             SandboxManager
       ┌────────────┼─────────────┐
       ▼            ▼             ▼
 Runtime setup   Policy engine   Registry/audit
       │
       ▼
 RuntimeProvider
 ├─ LinuxNativeProvider
 ├─ WindowsWslProvider
 ├─ MacLimaProvider
 └─ DockerProvider (optional)
       │
       ▼
 rumi-sandbox-agent (common Linux guest contract where applicable)
       │
       ├─ exec/files/workspace/ports
       └─ Xvfb/Openbox/Chromium desktop seats
```

### 2.2 Provider preference

`provider=auto` resolves in this order:

#### Linux

1. `linux_native` when requirements are satisfied or the bundled runtime is available.
2. existing compatible Docker/Podman context if the selected template specifically requires container isolation.
3. unavailable with actionable doctor output.

#### Windows

1. `windows_wsl` using the Rumi-managed `RumiUbuntu` WSL2 distribution when
   `wsl.exe` is already available.
2. existing Docker-compatible context only when explicitly selected or a template requires it.
3. unavailable with actionable diagnostics when WSL2 is unavailable.

#### macOS

1. `mac_lima` using an Ubuntu guest when `limactl` is already available.
2. existing Docker/Colima/Podman context only when explicitly selected or required.
3. unavailable with virtualization diagnostics.

Docker Desktop is never silently installed and never the default prerequisite.

### 2.3 User installation promise for this PR

This PR only provisions guests after the required host launcher is present.
Bundled Lima installation, WSL feature enablement, Windows reboot resume, and
strong per-instance guest isolation are follow-up work.

Allowed user experience:

- Click **Set up Rumi Managed Runtime**.
- See a fail-closed diagnostic when `limactl` or `wsl.exe` is missing.
- Create/provision the Rumi Ubuntu guest after the launcher is available.
- Approve any macOS security/virtualization prompt shown by the OS.

Disallowed user experience:

- “Open PowerShell and run these seven commands.”
- “Install Docker Desktop first.”
- representing launcher installation or guest isolation as completed when it is
  still follow-up work,
- silently accepting a Docker/Desktop third-party license,
- silently enabling OS features without a clear Rumi confirmation screen.

---

## 3. Directory and ownership plan

Evolve the existing defaultspack sandbox package:

```text
tobkiri_runtime/ecosystem/defaultspack/backend/sandbox/
├── __init__.py
├── loader.py
├── models.py
├── errors.py
├── sandbox_manager.py
├── policy.py
├── template_binding.py
├── runtime_service.py
├── artifact_store.py
├── state_store.py
├── audit.py
├── control_lease.py
├── frame_cache.py
├── provider_registry.py
├── providers/
│   ├── __init__.py
│   ├── base.py
│   ├── linux_native.py
│   ├── windows_wsl.py
│   ├── mac_lima.py
│   └── docker.py
├── guest/
│   ├── protocol.py
│   ├── client.py
│   ├── artifact_manifest.py
│   └── bootstrap.py
└── testing/
    ├── fake_provider.py
    └── fake_guest_agent.py
```

Guest agent source:

```text
tobkiri_runtime/ecosystem/defaultspack/runtime_agent/
├── __init__.py
├── __main__.py
├── server.py
├── auth.py
├── models.py
├── sandbox_ops.py
├── exec_ops.py
├── file_ops.py
├── port_ops.py
├── desktop_ops.py
├── process_registry.py
├── resource_limits.py
├── redaction.py
└── health.py
```

Declarative templates and contracts:

```text
tobkiri_runtime/ecosystem/rumi_sandbox_runtime_pack/templates/
├── pack.safe/template.json
├── pack.networked/template.json
├── coding.python/template.json
├── coding.node/template.json
├── coding.rust/template.json
├── desktop.ubuntu/template.json
├── desktop.browser/template.json
└── tool.ephemeral/template.json
```

Frontend:

```text
tobkiri_runtime/ecosystem/defaultspack/webapp/src/features/sandboxes/
├── api.ts
├── types.ts
├── runtimeStatus.ts
├── useRuntimeDoctor.ts
├── useSandboxTemplates.ts
├── useSandboxInstances.ts
├── useDesktopFrames.ts
└── useDesktopControlLease.ts

tobkiri_runtime/ecosystem/defaultspack/webapp/src/components/desktops/
├── DesktopMonitorWorkspace.tsx
├── DesktopToolbar.tsx
├── DesktopGrid.tsx
├── DesktopTile.tsx
├── DesktopInspector.tsx
├── DesktopCreateDialog.tsx
├── DesktopControlSurface.tsx
├── DesktopProviderNotice.tsx
├── RuntimeSetupDialog.tsx
└── desktopCoordinates.ts
```

Support tooling:

```text
scripts/support/collect_managed_runtime_bundle.py
```

---

## 4. Core models

### 4.1 Runtime provider status

```python
@dataclass(frozen=True)
class RuntimeProviderStatus:
    provider_id: str
    platform: str
    available: bool
    installed: bool
    ready: bool
    version: str | None
    capabilities: frozenset[str]
    missing_requirements: tuple[str, ...]
    requires_user_action: bool
    user_action: str | None
    reboot_required: bool
    diagnostics: tuple[Diagnostic, ...]
```

Capabilities use stable names:

```text
sandbox.exec
sandbox.files
sandbox.overlay_workspace
sandbox.port_forward
sandbox.network_policy
sandbox.resource_limits
sandbox.desktop
sandbox.desktop_input
sandbox.snapshot
sandbox.container
runtime.managed_install
runtime.update
runtime.uninstall
```

### 4.2 Sandbox template resolution

```python
@dataclass(frozen=True)
class ResolvedSandboxTemplate:
    template_id: str
    template_version: str
    runtime_os: str
    provider_requirements: frozenset[str]
    packages: tuple[PackageSpec, ...]
    desktop: DesktopSpec | None
    filesystem: FilesystemPolicy
    network: NetworkPolicy
    secrets: SecretsPolicy
    resources: ResourceLimits
    lifecycle: LifecyclePolicy
    allowed_operations: frozenset[str]
    source_template_ids: tuple[str, ...]
```

Template data is loaded through the existing RumiTemplate catalog. The frontend sends only `template_id` plus a narrow set of user-overridable fields. It cannot send authority fields such as host mounts, secret paths, provider commands, guest entrypoints, or privilege flags.

### 4.3 Sandbox instance

Replace the prototype status model with:

```text
creating
provisioning
starting
ready
busy
stopping
stopped
failed
destroying
destroyed
```

Persist:

```python
SandboxInstance:
    sandbox_id
    name
    template_id
    template_version
    provider_id
    provider_instance_id
    runtime_id
    state
    created_at
    updated_at
    started_at
    stopped_at
    destroyed_at
    last_activity_at
    last_error
    capabilities
    resource_limits
    workspace_binding
    network_policy
    desktop_spec
    assigned_agent_id
    generation
    recovery_token_hash
```

Never persist raw control tokens, guest bearer tokens, secrets, clipboard data, typed text, or screenshot bytes.

### 4.4 Desktop seat

A desktop seat is a sandbox capability, not a second registry.

```python
DesktopSeatView:
    sandbox_id
    seat_id
    name
    status
    width
    height
    display_backend
    frame_seq
    last_frame_at
    control_owner
    assigned_agent_id
    isolation_summary
```

`seat_id` may initially equal `sandbox_id`, but keep separate fields in the API so one sandbox can support more than one seat later.

---

## 5. Provider interface

```python
class RuntimeProvider(Protocol):
    provider_id: str

    def doctor(self, request: RuntimeRequirements) -> RuntimeProviderStatus: ...
    def ensure(self, request: EnsureRuntimeRequest, progress: ProgressSink) -> EnsureResult: ...
    def update(self, request: UpdateRuntimeRequest, progress: ProgressSink) -> UpdateResult: ...
    def uninstall(self, request: UninstallRuntimeRequest, progress: ProgressSink) -> UninstallResult: ...

    def create(self, spec: SandboxCreateSpec) -> ProviderInstance: ...
    def start(self, instance: ProviderInstance) -> ProviderInstance: ...
    def stop(self, instance: ProviderInstance, *, force: bool = False) -> None: ...
    def destroy(self, instance: ProviderInstance) -> None: ...
    def reconcile(self, persisted: ProviderInstance) -> ReconcileResult: ...
    def connect_agent(self, instance: ProviderInstance) -> GuestAgentClient: ...
```

Requirements:

- no provider may invoke a shell string assembled from API input,
- all subprocesses use argument arrays and `shell=False`,
- every mutating operation is idempotent,
- each operation emits structured progress events,
- cancellation must leave an inspectable recoverable state,
- provider-specific state is opaque to the application service and versioned.

---

## 6. OS provider implementation

## 6.1 Windows WSL provider

### Doctor

Check using `wsl.exe` argument arrays:

- Windows build and virtualization support,
- WSL command availability,
- WSL status/version,
- WSL2 default capability,
- whether reboot is pending,
- `RumiUbuntu` distribution presence and version,
- guest-agent health,
- free disk space,
- localhost forwarding availability.

Do not parse localized human output when a machine-readable alternative exists. Where WSL output is unavoidable, isolate parsing behind versioned adapters and include raw redacted diagnostics.

### Ensure flow

1. If WSL2 is enabled, continue without elevation.
2. If it is disabled, return a user-action plan to the frontend.
3. After explicit confirmation, launch a signed Rumi elevation helper.
4. The helper runs only allowlisted WSL enable/update commands.
5. Persist a resumable setup transaction before the helper runs.
6. If reboot is required, register a one-time resume marker and show **Restart now** / **Later**.
7. After restart, download the locked RumiUbuntu rootfs artifact.
8. Verify artifact manifest, SHA-256, size, architecture, and signing metadata.
9. Import it as a private `RumiUbuntu` WSL2 distribution under Rumi application data.
10. Start the guest agent and complete a nonce-based local handshake.

Example internal command shape, never shown as a manual requirement:

```text
wsl.exe --import RumiUbuntu <install-dir> <verified-rootfs.tar> --version 2
```

### Lifecycle

- Do not import the Microsoft Store Ubuntu distribution.
- Do not modify the user's default WSL distribution.
- Do not inherit the user's shell profiles.
- Use a dedicated Unix user and state path.
- `uninstall` must offer to remove only `RumiUbuntu`, never other distributions.
- Shutdown should not kill unrelated WSL distributions.

### Networking

Use the guest agent over a loopback-only endpoint with an ephemeral per-boot token. Do not bind the guest agent to LAN interfaces. Validate that the process owning the forwarded port is the expected Rumi guest agent.

## 6.2 macOS Lima provider

### Distribution

Bundle Lima and required helper binaries in the signed Rumi application, or download them from a version-pinned Rumi artifact manifest. Do not require Homebrew.

Maintain architecture-specific artifacts:

- `darwin-arm64`
- `darwin-x64`

### Ensure flow

1. Doctor checks macOS version, architecture, Hypervisor/Virtualization support, free disk, Rosetta requirement, and existing Rumi VM state.
2. Verify Lima binary and Ubuntu image artifacts.
3. Create a VM named `rumi-ubuntu` from a locked generated template.
4. Provision the guest agent through cloud-init or a sealed base image.
5. Use loopback port forwarding for the guest-agent API.
6. Reconcile interrupted creation using Lima state plus Rumi's transaction journal.

### VM configuration

- headless by default,
- no host desktop window,
- no Docker daemon unless a selected template explicitly requires container capability,
- explicit workspace mounts only,
- read-only base image with a writable data disk/overlay,
- resource limits derived from template and host capacity,
- no automatic mount of the entire home directory,
- no ambient SSH agent or host environment forwarding.

### Uninstall/update

- update guest components independently from the base image when compatible,
- snapshot/migrate state before destructive base-image upgrades,
- remove only Rumi-owned VM, disks, caches, and forwarding configuration.

## 6.3 Linux native provider

### Runtime

Use the ported Xvfb/Openbox driver as the desktop backend. Provide execution isolation through the best available mechanism:

1. bubblewrap + user namespaces + overlay workspace,
2. systemd-run transient scope for resource accounting when available,
3. a clearly labelled reduced-isolation fallback only when the selected template permits it.

The provider must report an honest isolation summary. Xvfb isolates the display, not the filesystem/process/network namespace by itself.

### Bundling

For `.deb`, declare/install runtime dependencies through package metadata.

For AppImage, ship a private runtime directory containing Xvfb/Openbox/xdotool/ImageMagick/terminal dependencies and set `PATH`, `LD_LIBRARY_PATH`, and `XDG_DATA_DIRS` only for provider subprocesses.

Do not mutate global environment variables for the Rumi process.

### Desktop process lifecycle

Per seat:

- allocate an exclusive display number with a lock containing PID and boot identity,
- start Xvfb,
- wait for X readiness,
- start Openbox,
- launch the selected preset application,
- track child PIDs/process groups,
- capture logs to a per-seat directory,
- terminate child app, WM, then Xvfb,
- remove sockets, locks, temporary frames, and logs subject to retention policy.

## 6.4 Docker provider

This is optional and reuses an existing compatible runtime.

Detect:

- Docker Engine/Desktop,
- Podman Docker-compatible socket/CLI where supported,
- Colima Docker context,
- other contexts only after capability checks.

Rules:

- never install Docker Desktop automatically,
- never silently accept a Docker license,
- never start arbitrary user containers,
- label all Rumi-owned objects,
- use locked image digests, not floating tags, for shipped templates,
- default network is none,
- workspace mounts follow the resolved template policy,
- remove only Rumi-labelled objects.

---

## 7. Guest agent protocol

Use one guest implementation for Windows WSL and macOS Lima. Linux native may run the same agent locally through a Unix socket so behavior remains consistent.

### Transport

- Unix socket for Linux native where practical.
- Loopback TCP for WSL/Lima.
- Random per-runtime endpoint.
- Mutual nonce challenge during bootstrap.
- Short-lived bearer token rotated on each agent start.
- Host validates agent build ID, protocol version, runtime ID, and artifact generation.

### API

```text
GET    /v1/health
GET    /v1/capabilities
POST   /v1/sandboxes
GET    /v1/sandboxes/{id}
POST   /v1/sandboxes/{id}/start
POST   /v1/sandboxes/{id}/stop
DELETE /v1/sandboxes/{id}

POST   /v1/sandboxes/{id}/exec
POST   /v1/sandboxes/{id}/files/read
POST   /v1/sandboxes/{id}/files/write
POST   /v1/sandboxes/{id}/files/apply-patch
POST   /v1/sandboxes/{id}/ports/expose
DELETE /v1/sandboxes/{id}/ports/{port}

POST   /v1/sandboxes/{id}/desktop/start
GET    /v1/sandboxes/{id}/desktop/frame
POST   /v1/sandboxes/{id}/desktop/input
```

### Exec request

```json
{
  "argv": ["python", "-m", "pytest", "-q"],
  "cwd": ".",
  "env": {"PYTHONUNBUFFERED": "1"},
  "timeout_ms": 120000,
  "stdin": null,
  "client_request_id": "uuid"
}
```

No raw `command` string in the public API. Compatibility conversion is allowed only for trusted internal legacy functions and must produce an argv plan before execution.

### Desktop input request

```json
{
  "action": "click",
  "client_action_id": "uuid",
  "x": 410,
  "y": 220,
  "button": "left",
  "lease_token": "opaque"
}
```

Supported actions:

- move,
- click,
- double_click,
- drag,
- scroll,
- type_text,
- key.

The agent validates bounds, action schema, rate limits, seat state, and lease ownership.

---

## 8. Sandbox templates

Templates are RumiTemplate documents, not a second untrusted execution language.

### 8.1 `pack.safe`

- network: off,
- filesystem: ephemeral overlay,
- workspace: read-only when explicitly selected,
- secrets: denied,
- desktop: off,
- default TTL: 15 minutes,
- max CPU: 1,
- max memory: 1 GiB,
- operations: restricted exec/read.

### 8.2 `pack.networked`

- network: allowlist or approval-gated,
- no ambient host credentials,
- workspace access explicit,
- package installation approval-gated,
- default TTL: 30 minutes.

### 8.3 `coding.python`, `coding.node`, `coding.rust`

- writable workspace overlay,
- source workspace bind chosen by user/session,
- port preview capability,
- branch-session persistence,
- network asks on first use or follows project policy,
- secrets mounted individually and read-only after explicit approval,
- 2 CPU / 4 GiB defaults, host-adjusted.

### 8.4 `desktop.ubuntu`

- Ubuntu guest,
- Xvfb + Openbox,
- no visible host window,
- 1440×900 default,
- filesystem ephemeral by default,
- limited/approved network,
- desktop snapshot/input capability,
- no host home mount,
- no secrets by default.

### 8.5 `desktop.browser`

Extends `desktop.ubuntu` and launches a managed Chromium profile with ephemeral storage unless persistence is explicitly selected.

### 8.6 `tool.ephemeral`

- short TTL,
- no desktop,
- no network,
- no secrets,
- no workspace unless explicitly supplied,
- strict output/CPU/memory limits.

### Template override rules

User-supplied templates may select from registered capabilities and narrow policy. They cannot:

- name provider command lines,
- mount arbitrary host paths,
- request privileged mode,
- load executable backend modules,
- bypass approval or audit,
- broaden a builtin deny policy without a trusted local policy grant.

---

## 9. Pack and coding integration

## 9.1 Pack manifest/template binding

Expose a sandbox request through the pack's trusted manifest/template contribution:

```json
{
  "sandbox": {
    "template_id": "pack.safe",
    "workspace_access": "read_only",
    "network": "off"
  }
}
```

At activation/run time:

1. resolve the trusted pack identity,
2. resolve RumiTemplate dependencies,
3. merge only allowed narrowing overrides,
4. run backend policy evaluation,
5. show an approval card when required,
6. create/reuse a sandbox according to lifecycle policy,
7. pass a scoped sandbox handle to allowed tools,
8. destroy/retain according to TTL and persistence policy.

Pack code never receives a provider object, WSL distro name, Lima path, Docker socket, or guest token.

## 9.2 Coding sandbox

Add a `SandboxExecutionBackend` to the existing coding terminal/execution abstraction. The coding session stores `sandbox_id` and template identity, not provider internals.

Expected functions:

```text
coding.sandbox.create
coding.sandbox.exec
coding.sandbox.apply_patch
coding.sandbox.expose_port
coding.sandbox.status
coding.sandbox.stop
coding.sandbox.destroy
```

Workspace policy:

- create an overlay/copy-on-write layer,
- present resulting file changes as a diff,
- applying changes back to the host workspace is a separate approved operation,
- never treat sandbox success as permission to mutate outside the selected workspace.

## 9.3 Existing `sandbox_exec`, `python_exec`, `node_exec`

Preserve current public function IDs and response compatibility where practical.

Internally:

- resolve the correct template,
- ensure managed runtime,
- create/reuse an ephemeral sandbox,
- translate trusted legacy command input to a reviewed argv plan,
- call the common guest-agent exec API,
- return provider/template/runtime metadata in additive fields,
- remove the direct `DockerRunBuilder` ownership from `sandbox_tools.py`.

---

## 10. Defaultspack host API

All routes are same-origin, authenticated, CSRF-protected, and registered through the canonical transport/template route catalog.

### Runtime

```text
GET    /api/runtime/providers
POST   /api/runtime/doctor
POST   /api/runtime/ensure
GET    /api/runtime/operations/{operation_id}
POST   /api/runtime/operations/{operation_id}/cancel
POST   /api/runtime/update
POST   /api/runtime/uninstall
```

`ensure` returns an operation object. Long-running progress is read by SSE or bounded polling through the normal same-origin API; do not expose provider-local ports directly to the webapp.

### Templates and sandboxes

```text
GET    /api/sandbox/templates
GET    /api/sandboxes
POST   /api/sandboxes
GET    /api/sandboxes/{sandbox_id}
POST   /api/sandboxes/{sandbox_id}/start
POST   /api/sandboxes/{sandbox_id}/stop
POST   /api/sandboxes/{sandbox_id}/restart
DELETE /api/sandboxes/{sandbox_id}
POST   /api/sandboxes/{sandbox_id}/exec
POST   /api/sandboxes/{sandbox_id}/files/apply-patch
POST   /api/sandboxes/{sandbox_id}/ports
```

### Desktops

```text
GET    /api/desktops
POST   /api/desktops
GET    /api/desktops/{seat_id}
POST   /api/desktops/{seat_id}/start
POST   /api/desktops/{seat_id}/stop
POST   /api/desktops/{seat_id}/restart
DELETE /api/desktops/{seat_id}
GET    /api/desktops/{seat_id}/frame
POST   /api/desktops/{seat_id}/input
POST   /api/desktops/{seat_id}/control/acquire
POST   /api/desktops/{seat_id}/control/renew
POST   /api/desktops/{seat_id}/control/release
```

### Request safety

- IDs use strict canonical formats.
- Template ID must exist in the authoritative catalog.
- Provider override is a known provider ID only.
- URLs allow `http`/`https` only.
- paths are workspace-relative IDs, not arbitrary host paths.
- exec uses argv arrays.
- action requests have idempotency IDs.
- bodies have size limits.
- all errors return stable machine codes plus user-safe messages.

### Desktop lifecycle confirmation contract

Stop and delete confirmations are single-submit transactions, not optimistic
buttons. The webapp creates one operation ID before dispatch, locks every
conflicting lifecycle control for that seat synchronously, and keeps the modal
focus trap active until the server outcome is known. Close, Cancel, Escape, and
backdrop dismissal remain unavailable while the operation is pending because
desktop lifecycle cancellation is not safe after submission.

The server durably reserves `(operation_id, seat_id, action, principal)` before
calling the runtime provider. Reservations are serialized across service
processes. A repeated ID for the same authorized request returns the stored
result; the same ID for another request fails closed. Desktop operation status
and replay are visible only to the principal that created the operation, and
generic runtime cancellation rejects desktop lifecycle operations.

After any response or ambiguous transport failure, the webapp compares the
requested action with a fresh authoritative desktop list. A stopped or
externally deleted seat confirms Stop; a destroyed/deleted tombstone or absent
seat confirms Delete. A terminal provider failure permits Retry with a new
operation ID, while an ambiguous or completed-but-not-yet-visible outcome keeps
the original ID so retry cannot duplicate work. Provider exception text is not
shown in the dialog. Failures keep the affected desktop, action, safe reason,
operation ID, Retry, and Cancel in the modal. Success is announced only after
reconciliation, then focus moves to the replacement Start control, an adjacent
desktop, or the Desktops workspace.

---

## 11. Human takeover and AI coordination

### Control lease

A server-side lease is mandatory.

```text
acquire → renew every 10s → expire after 30s → release
```

Rules:

- one human lease per seat,
- lease token is random and returned once,
- store only a hash,
- token is never logged or persisted,
- page unmount/visibility loss triggers best-effort release,
- TTL handles crashes/disconnects,
- stop/restart/delete invalidates the lease,
- AI actions during a human lease return `DESKTOP_CONTROL_CONFLICT`,
- human input without a valid lease returns `DESKTOP_LEASE_REQUIRED`,
- rate limits are per seat and actor.

Audit events record action category and coordinates/key names but not typed text, clipboard contents, or tokens.

### AI functions

Expose stable functions over the same service:

```text
sandbox.create
sandbox.status
sandbox.exec
sandbox.apply_patch
sandbox.stop
sandbox.destroy

desktop.create
desktop.observe
desktop.click
desktop.type
desktop.key
desktop.scroll
desktop.stop
desktop.destroy
```

AI cannot acquire a human lease. AI ownership is represented separately and automatically yields to an active human lease.

---

## 12. Desktop frame transport

The first complete implementation is **live snapshots**, not WebRTC video.

### Cadence

- unselected tile: 1200 ms,
- selected tile: 500 ms,
- human takeover: 250 ms,
- hidden document: paused,
- error backoff: 1s, 2s, 4s, 8s.

### Backend

- capture at most once per 250 ms per seat,
- cache encoded frame by `frame_seq`,
- support `after=<frame_seq>` and return `204`/not-modified semantics,
- apply JPEG/WebP quality tiers for grid/focus,
- bound image dimensions and encoded size,
- retain last successful frame metadata,
- never persist frame bytes unless explicit redacted diagnostics are requested.

### Frontend

- prevent overlapping requests per seat,
- use `AbortController`,
- preserve the last good frame during transient failures,
- revoke replaced object URLs,
- stop polling on unmount/hidden document,
- display `Live snapshots`, not `Live video`.

### Coordinate mapping

Account for `object-contain` letterboxing:

```ts
const scale = Math.min(viewWidth / frameWidth, viewHeight / frameHeight);
const drawnWidth = frameWidth * scale;
const drawnHeight = frameHeight * scale;
const offsetX = (viewWidth - drawnWidth) / 2;
const offsetY = (viewHeight - drawnHeight) / 2;
const desktopX = Math.round((pointerX - offsetX) / scale);
const desktopY = Math.round((pointerY - offsetY) / scale);
```

Clicks outside the drawn frame are ignored.

---

## 13. Frontend specification

## 13.1 Sidebar placement

File:

```text
webapp/src/components/HistoryBoard.tsx
```

Add `Monitor` from `lucide-react`.

Props:

```ts
onDesktopsOpen?: () => void;
isDesktopsActive?: boolean;
```

Full sidebar order is fixed:

```text
New Chat
New Group
Calendar
Kanban
Desktops
Search
Tag filter
```

Compact rail order is also:

```text
Calendar
Kanban
Desktops
```

The Desktops button must use the same spacing, hover, active, focus, title, and `aria-current` patterns as Kanban. No custom glow or raw z-index.

## 13.2 Workspace tab

Extend the existing workspace tab type with `desktops` and add the creation option directly after `kanban`.

Behavior:

- clicking the sidebar item activates an existing Desktops tab,
- otherwise creates exactly one tab titled `Desktops`,
- repeated clicks never duplicate it,
- closing it returns to the most recent valid tab,
- persisted tab state remains forward/backward safe.

## 13.3 App integration

Add `isDesktopsMode` and render `DesktopMonitorWorkspace` in the main workspace switch.

While active, hide chat-only composer/header/peek surfaces. Do not leave a chat composer below the desktop monitor.

## 13.4 Desktop monitor layout

```text
┌────────────────────────────────────────────────────────────┐
│ Desktops  4 running  [All] [Running] [2×2]    New Desktop  │
├──────────────────────────────────────┬─────────────────────┤
│ desktop grid                         │ inspector           │
│                                      │ status/provider     │
│ ┌─────────────┐ ┌─────────────┐      │ isolation           │
│ │ seat frame  │ │ seat frame  │      │ agent/control       │
│ └─────────────┘ └─────────────┘      │ actions/logs        │
└──────────────────────────────────────┴─────────────────────┘
```

Use existing defaultspack zinc/black styling:

- workspace base `#09090b`,
- panels `#0a0a0c`,
- borders `zinc-800/70`,
- running emerald,
- provisioning amber,
- failed red,
- selected/takeover visible through border and text, not color alone.

Responsive behavior:

- under 900 px: one column,
- 900–1399 px: two columns,
- 1400+ px: three columns,
- high-density mode permits four columns,
- inspector is 320 px side panel at large width and a bottom drawer below 1100 px.

### Tile

Show:

- name,
- status text and icon,
- provider label,
- display/seat ID in inspector only,
- frame,
- assigned agent,
- AI/Human control state,
- last-frame age,
- error overlay while retaining last good frame.

Actions:

- Take over / Return to AI,
- Snapshot,
- Restart,
- Stop,
- Delete.

Tile click selects. Actual frame interaction is disabled until takeover is acquired.

### Runtime setup states

Provider unavailable must list exactly what is missing and offer:

- **Set up Rumi Managed Runtime**,
- **Run doctor again**,
- **Copy diagnostics**.

The UI must never claim VM isolation for Linux native. Display an `Isolation` section based on backend facts.

### New Desktop dialog

Fields:

- name,
- template (`desktop.ubuntu`, `desktop.browser`),
- provider (`Auto` plus available explicit providers),
- resolution,
- starter (`Empty`, `Browser URL`, `Terminal`),
- URL for browser preset,
- workspace binding,
- workspace access,
- network policy summary,
- agent assignment.

Warn clearly when Linux native shares host process/filesystem/network namespaces beyond configured sandboxing.

## 13.5 Runtime & Sandboxes settings

Add a settings view containing:

- provider doctor cards,
- setup/update/uninstall operations,
- runtime version and guest protocol,
- installed templates,
- active sandboxes/desktops,
- disk use,
- remove-unused action,
- sanitized diagnostics copy/export.

---

## 14. Artifact distribution and setup transactions

### Artifact manifest

Every downloaded or bundled runtime artifact is described by a signed/versioned manifest:

```json
{
  "schema_version": 1,
  "runtime_version": "...",
  "protocol_version": 1,
  "platform": "windows-x64",
  "artifacts": [
    {
      "id": "rumi-ubuntu-rootfs",
      "url": "artifact-channel-id",
      "sha256": "...",
      "size": 123,
      "compression": "zstd",
      "build_id": "..."
    }
  ]
}
```

Do not let an API client supply URLs. The backend resolves an artifact channel from built-in signed metadata.

### Transaction journal

Persist setup steps:

```text
planned
downloading
verified
installing
reboot_pending
starting_agent
health_checking
completed
failed
cancelled
```

Each step is idempotent and recoverable after process crash/reboot.

### Updates

- separate host provider, guest agent, template, and base image versions,
- protocol compatibility range,
- staged update with health check before switching active generation,
- rollback to previous guest generation when possible,
- never update while a sandbox has uncommitted writable state without explicit policy.

---

## 15. Security requirements

Non-negotiable:

1. No public raw shell command API.
2. No arbitrary host path mounts from frontend/template input.
3. No ambient host environment or secret inheritance.
4. Network off by default for `pack.safe` and `tool.ephemeral`.
5. Artifact verification before install/import/run.
6. Loopback-only guest-agent transport with per-boot credentials.
7. Backend authority for template resolution and policy merge.
8. Explicit approval for privilege, network broadening, secret mount, host writeback, or long-lived persistence.
9. Human takeover via expiring server lease.
10. Full cleanup of child processes, VMs/distributions owned by Rumi, sockets, display locks, temp frames, and tokens.
11. Honest isolation reporting.
12. Audit redaction for typed text, clipboard, tokens, secrets, frame bytes, and sensitive paths.
13. Strict resource/output/time limits.
14. Request IDs/idempotency for mutating and input actions.
15. `GUISandbox` remains test-only and cannot be selected in production registration.

---

## 16. Migration and compatibility

### Registry

Migrate schema v1 sandbox records on read:

- prototype `ready` with no provider instance becomes `stopped`/`legacy_placeholder`,
- destroyed/error records remain inspectable,
- never pretend an old fake instance is live,
- write schema v2 atomically after successful migration,
- retain a backup until a complete startup succeeds.

### API/functions

- existing sandbox tool IDs remain,
- response fields remain additive where possible,
- old Docker-specific errors map to new stable runtime errors,
- legacy callers can request a managed Docker provider explicitly but no longer own `DockerRunBuilder`.

### PR #221

Port relevant Linux files by content, with current imports and tests. Do not carry:

- LINE model/profile changes,
- unrelated browser dialog behavior,
- old master merge noise,
- redundant platform code already present on current master.

---

## 17. Testing matrix

## 17.1 Unit tests

### Manager/state

- registry v1→v2 migration,
- lifecycle transition validation,
- concurrent create/start/stop/delete,
- idempotent retries,
- crash recovery/reconcile,
- corrupt state backup,
- max sandbox/desktop limits,
- TTL cleanup,
- provider selection and capability requirements.

### Policy/template

- authoritative template resolution,
- user template cannot broaden builtin policy,
- network/mount/secrets default deny,
- invalid provider/template rejected,
- unknown capability rejected,
- pack/coding/desktop template contract tests.

### Security

- shell string rejected at public API,
- path traversal rejected,
- arbitrary artifact URL rejected,
- artifact hash/signature mismatch rejected,
- bearer/control tokens redacted,
- typed text not audited,
- lease replay/expiry/conflict,
- idempotent action replay does not double click,
- desktop double activation and key repeat reserve only one operation,
- close, Cancel, Escape, and conflicting seat actions stay blocked while pending,
- timeout before reservation and timeout after commit reconcile without duplicate work,
- stale seat, external stop/delete, terminal retry, operation authorization, and
  focus/announcement outcomes are covered,
- rate limits.

### Provider mocks

- WSL absent/enabled/reboot/imported/agent-unhealthy,
- Lima missing/created/stopped/corrupt/agent-unhealthy,
- Linux dependency/bundle/isolation modes,
- Docker context present/absent/wrong capability,
- cleanup affects only Rumi-owned resources.

## 17.2 Real Linux integration

On Ubuntu CI:

1. start three Xvfb/Openbox seats,
2. assert distinct displays/locks,
3. launch a deterministic test window,
4. capture non-empty frames,
5. click/type/scroll and assert visual/state change,
6. run an exec sandbox,
7. verify network policy where CI permits namespaces,
8. stop/destroy,
9. assert all child PIDs, X sockets, locks, and temp frames are gone.

## 17.3 Windows CI/smoke

Automated unit tests mock WSL command output. A dedicated self-hosted or release smoke lane verifies:

- WSL doctor,
- RumiUbuntu import from test rootfs,
- guest handshake,
- exec,
- desktop frame/input,
- stop/unregister cleanup,
- reboot-resume transaction through a test hook where feasible.

Do not make normal PR CI enable OS features on arbitrary hosted runners.

## 17.4 macOS CI/smoke

Unit tests mock Lima subprocess and state. A release smoke lane on both Apple Silicon and Intel (while supported) verifies:

- VM creation,
- agent handshake,
- exec,
- desktop frame/input,
- port forwarding,
- stop/delete cleanup.

## 17.5 Frontend tests

- Desktops below Kanban in full and compact layouts,
- workspace tab singleton behavior,
- runtime doctor states,
- setup progress/reboot-required states,
- desktop empty/grid/error states,
- polling overlap/backoff/hidden-document cleanup,
- coordinate conversion and letterbox rejection,
- takeover acquire/renew/release,
- AI conflict rendering,
- responsive inspector/drawer,
- object URL cleanup,
- accessible status/action labels.
- pending and failed lifecycle confirmation focus traps, announcements, and
  deterministic return focus.

## 17.6 Playwright

Mock API contract scenario:

1. open Rumi DP,
2. verify Desktops below Kanban,
3. open Desktops workspace,
4. provider unavailable → run setup,
5. setup progress → ready,
6. create four desktops,
7. observe four frames,
8. select seat,
9. acquire takeover,
10. click/drag/type with correct coordinates,
11. return to AI,
12. restart/stop/delete,
13. inspect failure diagnostics,
14. verify compact rail and narrow layout.

## 17.7 Quality commands

At minimum:

```bash
python -m pytest tobkiri_runtime/tests/test_defaultspack_sandbox_manager.py -q
python -m pytest tobkiri_runtime/tests/test_managed_runtime_*.py -q
python scripts/quality/check_template_contracts.py

cd tobkiri_runtime/ecosystem/defaultspack/webapp
npm test
npm run lint
npm run build
npm run test:e2e:ui-contract

cd ../../../../tobkiri_launcher/src-tauri
cargo test --locked
cargo fmt --check

git diff --check
```

---

## 18. Commit sequence inside the single PR

1. `docs(runtime): define managed sandbox and desktop seat contract`
2. `refactor(sandbox): version models, state, errors, and provider interface`
3. `feat(runtime-agent): add authenticated common guest protocol`
4. `feat(runtime): add Linux native managed provider and real X11 seats`
5. `feat(runtime): add Windows WSL managed provider and resumable setup`
6. `feat(runtime): add macOS Lima managed provider and artifact verification`
7. `feat(runtime): add optional Docker-compatible provider`
8. `feat(templates): ship pack, coding, desktop, and ephemeral sandbox templates`
9. `feat(defaultspack): route pack and coding execution through managed sandboxes`
10. `feat(defaultspack): expose runtime, sandbox, and desktop APIs/functions`
11. `feat(defaultspack-ui): add Desktops under Kanban and runtime setup UI`
12. `feat(defaultspack-ui): add desktop grid, frames, and human takeover`
13. `build(viewer): bundle managed runtime artifacts and setup helpers`
14. `test(runtime): add provider, security, integration, and UI coverage`
15. `docs(runtime): finalize operations, troubleshooting, and release evidence`

Commits may be reordered to preserve green intermediate states. Items marked as
future below are intentionally deferred beyond the PR #369 foundation scope.

---

## 19. Definition of Done

For PR #369, this checklist records foundation readiness plus explicit future
work. Future items are not claimed as complete by this PR.

### Product

- [ ] Desktops is directly below Kanban in full and compact Rumi DP.
- [ ] Linux creates and controls at least three hidden desktop seats without Docker.
- [ ] Windows creates/provisions RumiUbuntu when `wsl.exe` is available.
- [ ] Future: Windows performs guided WSL2 enablement/elevation/reboot resume.
- [ ] macOS creates/provisions a Lima Ubuntu VM when `limactl` is available.
- [ ] Future: Rumi bundles or installs the Lima launcher without Homebrew.
- [ ] Existing Docker-compatible runtimes are optional providers.
- [ ] User is not told to install Docker Desktop as a prerequisite.
- [ ] Missing Lima/WSL launchers fail closed with explicit diagnostics.
- [ ] Future: required OS consent/reboot is explicit and resumable.
- [ ] Pack isolation uses managed sandbox templates.
- [ ] Coding execution uses managed sandbox templates.
- [ ] Desktop Monitor uses the same manager/provider/agent stack.
- [ ] AI can target a sandbox/seat through stable functions.
- [ ] Human takeover blocks AI input until lease release/expiry.

### Integrity/security

- [ ] No public arbitrary shell-string execution.
- [ ] No arbitrary host mounts.
- [ ] No ambient secrets/environment.
- [ ] Runtime artifacts are verified.
- [ ] Guest-agent endpoint is loopback/private and authenticated.
- [ ] Linux native isolation claims are accurate.
- [ ] Cleanup leaves no owned processes, sockets, locks, tokens, VM/WSL remnants, or temp frames.
- [ ] Diagnostics ZIP contains no secrets, raw typed text, cookies, browser profiles, or unredacted frames.

### Quality

- [ ] Unit, integration, frontend, Playwright, template contract, Rust, lint, and build checks pass.
- [ ] Windows and macOS release smoke evidence is attached.
- [ ] PR body contains screenshots for full sidebar, compact rail, grid, setup, takeover, and error states.
- [ ] No placeholder buttons, fake “ready” state, TODO implementation, or `coming soon` text.
- [ ] No unrelated LINE/Gemma changes from PR #221.

---

## 20. Explicit non-goals for this PR

These are not required for completion and must not derail the core work:

- 60 fps WebRTC desktop streaming,
- GPU passthrough,
- Kubernetes/remote cloud orchestration,
- arbitrary user-supplied VM images,
- migration of every legacy terminal path in the repository,
- full GNOME shell; Xvfb/Openbox is the managed desktop implementation,
- silent OS privilege or third-party license acceptance.

The APIs should leave room for later providers, but do not implement speculative complexity.
