# Pack Architecture Wave 8

Wave 8 separates host capabilities by authority: read/write,
observe/control, and inspect/execute. Tool, chat, and UI surfaces are adapters;
they are not host executors and cannot approve their own requests.

## Service boundaries

| Capability | Observe/read owner | Control/write owner | Executor |
|---|---|---|---|
| workspace | `rumi_workspace_mount_pack` resource contract | same pack mount action | workspace service after one-shot receipt |
| files | `rumi_file_inspect_pack` | `rumi_file_mutation_pack`; `rumi_file_patch_pack` | selected service after one-shot receipt |
| shell | `rumi_shell_policy_pack` | `rumi_shell_execute_pack` | execute pack after one-shot receipt |
| terminal | `rumi_terminal_session_pack` resource | same pack action | session pack after one-shot receipt |
| Git | `rumi_git_read_pack` | `rumi_git_write_pack`; publication only in `rumi_git_publish_pack` | selected service after one-shot receipt |
| IDE | `rumi_ide_bridge_service_pack` resource | same pack action | bridge service after one-shot receipt |
| coding sandbox | COW observation | COW control and digest-pinned Docker execution | sandbox service; no host downgrade/apply |
| browser | `rumi_browser_host_service_pack` resource | same pack action | Viewer host broker after core Authority |
| desktop | `rumi_desktop_host_service_pack` resource | same pack action | Viewer host broker after core Authority |
| clipboard | `rumi_clipboard_host_service_pack` read | same pack write | Viewer host broker after core Authority |
| media devices | none | `rumi_media_capture_host_service_pack` capture/output | Viewer host broker after core Authority |
| document/media inspection | `rumi_media_inspect_service_pack` | none | file inspect contract only |
| vision/transcription | `rumi_media_analysis_adapter_pack` | none | existing replaceable AI modality contract |

`rumi_host_authority_bridge_pack` consumes a core-issued one-shot authority
token and creates a 30-second, one-redemption service receipt bound to service,
operation, argument hash, caller, profile, workspace, and session. It cannot
approve. Browser, desktop, clipboard, and media device requests instead return
typed HostIntents which are validated by core Authority and executed only by the
Viewer host broker.

Browser, desktop, clipboard, and screen-capture contracts expose domain-specific
operations but emit the registered `host.intent.execute` envelope with an exact
`host_function_id`. After core Authority consumes the approval, the executor
issues a second one-shot Viewer token bound to the exact function and canonical
arguments. Only then may the Viewer computer endpoint invoke its allowlisted
helper. Unimplemented browser tab/download operations return `unavailable` and
must not downgrade to direct execution.

## Security invariants

- `approved`, `approval_token`, `authority_token`, `viewer_host_approved`, and
  `yolo_mode` supplied by a caller never grant execution.
- Write, execute, publish, control, device capture, and output fail closed.
- Workspace file operations reject absolute paths and symlink escape.
- Git publication is distinct from local Git mutation and binds the expected
  remote URL hash. Bare force and local remotes are denied.
- Shell execution is distinct from pure classification and has no host fallback.
- Coding execution uses a locally available digest-pinned Docker image with no
  network, dropped capabilities, read-only root, limits, and no host downgrade.
- Clipboard payloads and raw screen/microphone/audio/camera media are not
  persisted by their boundary packs.
- Media inspection reads only through the selected file inspection contract.
- Vision/transcription owns no capture, file, provider, credential, or storage
  authority.

## Compatibility and removal

Defaultspack media blocks are finite contract adapters. Clipboard and screen
capture no longer execute subprocesses or return fabricated stub data.
Defaultspack coding blocks now project file, shell, terminal, Git, workspace,
and coding-sandbox contracts instead of importing their legacy executors.
Workspace mutations are exact-revision receipt operations. Sandbox mutation
requires an explicit owner-issued `sandbox_id`; observe actions may prepare a
new COW sandbox and return that ID. Sandbox artifact export is typed
`UNAVAILABLE` until an artifact pack owns a reviewed transfer contract.
`rumi_default_tools_pack` maps the existing `browser_computer`, `browser_use`,
`computer_use`, and `computer_observe` IDs to the new browser, desktop, and
clipboard contracts. HostIntent values remain top-level results so the
capability executor can route them through Authority.

The Viewer helper routes `browser.*` functions to the browser pack runner rather
than importing `BrowserComputerController`. Browser profile, cookie, session,
and tab metadata is written atomically by that runner under its own namespace.
Navigation accepts HTTP(S) only. Download listing reads only the pack-managed
download directory. Collection requires a Viewer-validated conversation artifact
root and one managed filename; arbitrary Downloads paths and symlinks are denied.

The large pre-Wave-8 computer/browser implementation remains only as a sunset
source compatibility surface and is no longer the canonical function entrypoint.
Wave 10 removes it and any remaining defaultspack host implementation after
external migration and rollback evidence exists.

## Data ownership matrix

| Resource | Authoritative pack | Schema version | Storage | Backup | Migration | Rollback | Retention | Export/import |
|---|---|---:|---|---|---|---|---|---|
| workspace mount metadata and selection | `rumi_workspace_mount_pack` | 1.0.0 | profile atomic state | owner snapshot | explicit canonical-path import | receipt-bound restore | profile lifetime | contract JSON |
| browser profiles | `rumi_browser_host_service_pack` contract / Viewer host | 1.0.0 | host-owned profile storage | host policy | one-way legacy adapter | select pinned adapter, never dual-write | profile policy | host contract |
| browser sessions | Viewer host through browser contract | 1.0.0 | host session state | none | finite action aliases | close sessions/select prior adapter | session lifetime | no |
| terminal sessions | `rumi_terminal_session_pack` | 1.0.0 | bounded process state | none | none | shutdown/remove pack | bounded session lifetime | event projection |
| IDE sessions | `rumi_ide_bridge_service_pack` | 1.0.0 | bounded process state | none | none | close/remove pack | bounded session lifetime | event projection |
| coding sandboxes | `rumi_coding_sandbox_service_pack` | 1.0.0 | COW pack user data | discardable | staged copy only | discard | explicit discard/profile cleanup | no |
| clipboard | OS/Viewer host | host | none in pack | none | finite alias | select prior adapter, never dual-write | OS policy | no |
| captured media | caller-selected artifact owner | 1.0.0 | none in capture pack | caller policy | typed HostIntent | stop/remove pack | request/session bound | no raw pack export |
| media inspection | none; derived | 1.0.0 | none | none | contract cutover | remove adapter | request lifetime | result only |

## Migration and rollback

Default Profile normalization selects every Wave 8 service and its declared
dependency deterministically. Removing a pack removes its contracts and tool
surface; no adapter scans all installed packs or falls back to the first source.
Legacy IDs are projections only and do not own data or execute host APIs.

Rollback selects the pinned prior adapter as one atomic profile change. It must
not enable old and new writers/executors together. Sandbox rollback discards COW
state, terminal/IDE rollback closes sessions, and browser/desktop/media rollback
must revoke outstanding tokens and receipts first.

## Cross-platform support and fallback matrix

| Boundary | macOS | Windows | Linux | Unsupported behavior |
|---|---|---|---|---|
| workspace/file/shell/terminal/Git/patch/IDE/sandbox | platform-neutral Python with explicit process/filesystem policy | same | same | missing executable/runtime is typed `unavailable`; no host downgrade |
| Viewer host broker | enabled with local token/audit connection | enabled with local token/audit connection | broker intentionally disabled in the current Viewer | contract remains installable but host action fails closed as broker unavailable |
| desktop native host | Accessibility/CGEvent/visible drivers selected behind `ComputerHost` | UIA/PostMessage/visible drivers selected behind `ComputerHost` | X11/visible drivers only when locally available | no eligible driver returns an error; no simulated success |
| browser runner | atomic local metadata plus system HTTP(S) browser handoff | same | same when a desktop browser handler exists | failed handoff is an error; downloads are limited to the pack-managed directory and validated artifact root |
| clipboard runner | `pbpaste`/`pbcopy` fixed argv | PowerShell read/`clip.exe` write fixed argv | Wayland/X11 command selected only when installed | missing command returns unavailable; no shell string execution |
| media device capture/output | Viewer HostIntent permission model | Viewer HostIntent permission model | current Viewer broker unavailable | approved operation without a platform runner returns an audited error |

This table is source-derived implementation intent, not runtime evidence. The
Wave 8 QA issue requires independent macOS, Windows, and Linux results before
merge.

Focused tests are defined in `tests/test_pack_architecture_wave8.py` but were
not executed by the implementation agent.

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。
