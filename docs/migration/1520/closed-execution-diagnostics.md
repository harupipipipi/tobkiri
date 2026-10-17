# Closed execution and native transport diagnostics

Component export failures retain the original typed cause and add a closed
observation: phase, canonical ABI stage, inherited context/deadline, owner failure
and optional remaining fuel. Typed Wasmtime fuel exhaustion, interruption and
other traps have distinct labels. Allocation and sealed unknown storage/filesystem
outcomes retain priority. A numeric zero alone never determines the error kind.

The fuel observation reads the still-owned failed Store. A typed canonical-call
stage is required so an earlier admission failure cannot accidentally report fuel
from a previous invocation of a warm instance. Observation failure is `None`.
This does not refuel, change limits, run a guest, renew a deadline or authorize
repeating an unresolved model/tool attempt.

Native reqwest errors have fixed `network-timeout`, `network-connect`,
`network-body`, `network-decode` or `network-transport` audit codes. The typed Core
failure packet keeps its existing `unavailable` code and contains only a fixed
message. Authority, allocation and sealed filesystem/storage failures take
priority; expired/cancelled calls keep their existing terminal disposition.
Guest text cannot impersonate these concrete native error types. URLs, headers,
provider bodies, private reasoning and raw error chains are never interpolated.
No HTTP read, grant, retry, ownership or transport-budget behavior is changed.

The regressions use real bounded async Wasmtime exports and a disposable private
loopback server for a native body timeout. They do not call an external provider,
read a live Profile, or establish successful Core/MiMo workflow completion.

Earlier live records with only `execution-error` or `host-effect` cannot be
retroactively assigned one of the new diagnoses. Retain their generation IDs,
public prefixes and unknown accounting. A later observation, provider lookup or
model-free reproduction must supply any stronger conclusion.
