# Actual native IPC observation

This is a prerequisite check before connecting the complete original Launcher
Setup. It does not replace Setup, add configuration commands, or change normal
startup. The legacy Launcher remains the product default.

The workflow builds the real debug Tauri app with the existing Core-host macOS
configuration, capabilities, CSP, custom protocol and opaque Pack iframe. A
private data-only Profile is saved through ordinary Core with a test Host that
approves only permission effects. This preparation does **not** test the real
native approval window. It grants nothing in normal user data.

The observer requires all three existing debug-scope inputs and an explicit
probe flag, on macOS in a debug build. Partial scope or any release request
fails closed before opening Core data. Only this mode defers creation of the
configured root window to attach observations. No commands, permissions or
origins are added. The diagnostic native caller count cannot authorize a call.

The trusted root submits the existing Profile form, receives a report only from
that actual iframe with origin null, and performs the final ordinary native
stop. The child checks parent DOM, parent Tauri internals and local storage,
inventories SDK/raw IPC surfaces, and attempts the existing eleven commands.
Raw messages deliberately have an **invalid empty invoke key**. The real key is
never read or forwarded to the child.

Passing requires the positive start/iframe/stop controls, complete expected
coverage, no unsettled SDK attempts, exactly two admitted native caller entries,
and an independently observed native invalid-key rejection for every raw send.
An absent SDK is explicitly recorded; at least one raw native surface is
required. Missing raw coverage, timeout, process failure, cleanup failure, absent
result or malformed result fails the observation.
The ordinary shutdown watchdog must also emit its actual closed outcome;
a zero process exit code or absence of error logs cannot substitute for it.

Tauri's invalid-key stderr includes the expected key. The Python controller
captures both native pipes in bounded memory, never inherits, prints, tees or
uploads either stream, and publishes only a strict closed receipt with counts
and source/build hashes. CI uploads that one metadata file. Local controller
and script regressions exercise parsing/privacy/failure paths; they are not
native execution evidence.

Even a passing native run proves only the tested macOS/debug binary's existing
eleven-command isolation under this exact configuration. It does not prove
that HostCaller authenticates iframe provenance, or denial after a valid key
has leaked. It proves neither a new configuration API nor full original
Setup/recovery/handover/approval/settings/chat, actual 1B inference, other
platforms, whole-save rollback, resource-custody completion or VM retirement.
