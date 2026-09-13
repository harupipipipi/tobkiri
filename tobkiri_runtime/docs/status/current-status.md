# Current Status

Last updated: 2026-08-10

## Implemented

- Core runtime approval, hash verification, trust store, grant manager, audit logging, and capability execution are in active use.
- Canonical runtime code lives in `tobkiri_runtime/`.
- Canonical pack implementation lives in `ecosystem/defaultspack/`.
- Canonical control-panel frontend lives in `ecosystem/defaultspack/webapp/`.
- Desktop-facing runtime surfaces already exist in `core_control_panel`, `core_viewer_capability`, `core_desktop_capability`, `tobkiri_launcher/`, and related API handlers.
- `api_routes` table dispatch is already live for control-panel and pack-defined API endpoints.
- Builtin core API routes now also include `core_system_api`, so shared system GET routes load from manifest data instead of handwritten `do_GET` branches.
- Pack function invocation now runs through explicit execution policy checks before dispatch.
- Compatibility alias use is locally audited without payload data, and non-internal `defaults.*` callers receive structured migration warnings.
- All 143 bundled production Packs have the four canonical v4 artifacts and one
  exact authority classification.
- The `defaults` Profile resolves an explicit Base, `shell.tauri.default`,
  Application, authority snapshot, ProfileLock, ResolvedPlan, and atomic
  ActivationRecord. The CLI Shell remains a separate Pack and is not conflated
  with the default Tauri presentation.
- Pack install, approval, enable/disable, restart, revocation, dynamic UI
  contribution, and external Normal Pack admission use the captured v4 Broker
  contracts. Client approval assertions and direct legacy routes fail closed.
- Normal Pack execution has no Host fallback. It requires an explicitly
  provisioned, separately named PackVM and a healthy authenticated attestation.
- The complete-v4 gate requires zero reachable legacy Registry/installed lookup,
  implicit fallback, double authority, unverified Shell launch, and offline
  projection identity findings.

## Production cutover boundary

- Runtime authority is Protocol v4 only. A finite compatibility projection may be
  generated offline for migration diagnostics, but is never an activation or
  dispatch input.
- Handwritten HTTP transport code remains as a protocol adapter. It does not
  restore Pack-specific route authority or a block/function fallback path.
- Legacy names may remain in stable storage keys, module names, migration input,
  and audit vocabulary as required by repository compatibility policy. They do
  not classify a source as production authority.

## Remaining release validation

- Build and verify installers on every supported CI platform.
- Exercise the exact packaged macOS artifact through Launcher UI, Pack lifecycle,
  restart persistence, PackVM capacity denial, stop, and cleanup ceremonies.
- Run a real PackVM guest operation when the host has the displayed bounded free
  space. Lack of host capacity must remain a visible fail-closed result, never a
  Host execution fallback.
