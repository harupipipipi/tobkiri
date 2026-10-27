# Tobkiri Core

`Core` owns Profile revisions, trusted approvals, immutable Pack verification,
Vault user actions and Session startup. Terminal and Launcher hosts depend only
on this facade. Generic canonical wire types, authoring helpers and HostIo
contracts are reexported here; grant construction and policy storage remain
internal.

A Session freezes its compiled digest, binding catalog and verified `(PackId,
PackHash)` resource map. It installs the same weak FlowRunner into the one Flow
engine and the PackHost before lazy component initialization. FlowRun dispatches
are authorized with that frozen revision. Head updates invalidate old Sessions
through the persistent security epoch.

Native input Flow targets are projected only from the verified owning Pack's
explicit declaration, original Profile ceiling, explicit per-Pack limits and
exact hash approval. Each target must connect to that source's validated input
alias in the frozen compiled revision. Missing boundaries deny; payload identity
or consent fields never contribute authority.

Startup UI attachments expose the Core token, trusted host frame token, verified
Pack hash and declared entry. Asset reads return exact immutable verified bytes.
UI invocations derive their principal from the attachment, never from arguments.
A stream result carries a Core-minted handle. Session owns its original request,
principal, epoch, deadline and cancellation context. Pulls are bounded to 32
values and the original byte ceiling; each frame has at most 64 handles and the
Session at most 1024. EOF, cancellation and frame detach close the handle and
record its terminal outcome. Hosts call `detach_ui` when a frame closes and
`close` when the Session ends.

Grant-owning streams capture their owning Tokio runtime. Result-bearing cleanup
paths await `CoreStream.close` and return cleanup or audit failures. A retained
receipt permits the same immutable terminal proposal to be delivered again;
physical cleanup is not repeated, and physical failures remain visible after
audit delivery succeeds. A Flow stream ending without a
terminal event fails closed, while Completed retains its successful outcome.

Effect authority comes only from the original literal declaration, ceiling,
explicit per-Pack limit and exact hash approval. Bindings and consumes declarations
cannot add permissions. Missing explicit limits deny all effect and resource
authority. The frozen exact WIT view controls callable operations even when a
compatible newer provider is selected. User authority has its own optional
`user_permissions` policy, intersected with the ceiling and approved for the
exact Profile document digest, compiled revision and committed epoch. A ceiling
alone grants no User effects. Imports require a fresh Profile approval.

`Core.open` performs synchronous durable deadline recovery without Tokio and
leaves admission dormant. Authorization and write APIs await an async startup
barrier: it recovers due leases, captures an outbox watermark, starts a supervisor
and drains every recovered terminal event through that watermark. Delivery
failure denies admission and can be retried. One bounded metadata recovery job
uses capped backoff while independent local deadline ticks signal cancellation.
Integrity faults stay latched and cannot be cleared by unrelated successful
delivery. Faulted Profiles remain available for trusted read-only inspection
through `profile_recovery_status`, without creating a Gate or Ready barrier.
Session supervisors reconcile live
cancellation hooks and persisted original deadlines; an ended supervisor requires
a fresh barrier. Read-only inspection remains available without a runtime.

Void Drop has no error-return channel. Cleanup jobs can finish after an ordinary
thread drops a stream while its owning runtime is alive, but shutdown can abort
them and an abandoned Drop cannot return an audit sink failure. Persisted due
consumed leases now receive an atomic terminal decision and durable, idempotent
audit outbox. Expiry records cleanup as unknown; it does not claim that the
physical effect stopped or succeeded. Startup recovery and supervisors retry
outbox delivery. Nonexpired ownership is not guessed from a PID; proven-orphan
recovery requires trustworthy ownership evidence. Explicit close before runtime
shutdown remains the observable physical cleanup path.

Remaining implementation work is tracked by the parent workstream: the approved
private compiled-call admission path, shared loop-cost wiring, opaque allocation
ownership across Flow/Core runtime values, and guest-requested UI attachment
registration. Native isolation has not been validated by this crate. No policy
fallback is provided for unproved authority or allocation paths.

Run the focused checks with `cargo test --locked -p tobkiri-core`.
