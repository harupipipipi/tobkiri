# Proc/PTY next-slice handoff

Status: **incomplete; no Proc/PTY backend availability is established here**.
This records the 2026-10-15 read-only inspection and the approved
[Q16 boundary](resource-boundaries.md#2026-10-15-astra-high-q16-filesystem-processes-and-wasm-reviewers).
It is an implementation handoff, not a new protocol or acceptance decision.

## Existing interfaces and hooks

`crates/tobkiri-wit/wit/core/core.wit` (1.0.0) and
`crates/tobkiri-wit/wit/core-v1-1/core.wit` (1.1.0) have identical Proc/PTY APIs:

```text
proc.spawn(program: string, args: list<string>,
  cwd: option<borrow<fs.dir>>, env: list<tuple<string, string>>)
  -> result<process, error>
process: read-stdout(max: u32), read-stderr(max: u32)
  -> result<option<list<u8>>, error>
process: write-stdin(data: list<u8>) -> result<_, error>
process: close-stdin(); kill(); wait() -> result<s32, error>

pty.spawn(program: string, args: list<string>,
  cwd: option<borrow<fs.dir>>, env: list<tuple<string, string>>,
  cols: u16, rows: u16) -> result<terminal, error>
terminal: read(max: u32) -> result<option<list<u8>>, error>
terminal: write(data: list<u8>), resize(cols: u16, rows: u16)
  -> result<_, error>
terminal: kill(); wait() -> result<s32, error>
```

- `crates/tobkiri-wasm/src/effects.rs` still links
  `UnavailableDirectory`, `UnavailableProcess`, and `UnavailableTerminal`;
  its dispatch rejects `fs`, `proc`, and `pty` as unavailable. Existing
  `link_resource`, `push<T>`, `resource_rep<T>`, and `get_resource[_mut]<T>`
  are the resource adapters. `Nominal<T, 0/1>` keeps package versions distinct;
  same-version `fs.dir` imports in fs/proc/pty must use the same Rust identity.
- `EffectValues` already provides owned byte/result conversion. A wait result
  can use `codec.ok(Some(Val::S32(code)))`; no signed-value primitive is missing.
  Its generic `args_hash` is unsuitable for spawn: it is unkeyed and cannot
  encode a resource. Bind resolved cwd identity instead of its table number.
- `crates/tobkiri-gate/src/protocol.rs` has only
  `Proc { program: String }` and `Pty { program: String }`. Permission checks
  require exact `PermissionSet.proc` membership; PTY also requires `pty`.
  Both currently have Elevated risk and per-effect trusted approval.
  `ApprovalPrompt` contains the complete effect; `engine.rs::prompt_for`
  supplies no process-specific redaction.
- Existing authority path: `Gate::authorize` → `Grant::consume` → narrowed
  `CallCx` → `Gate::{narrow_lease, watch_lease, check_context}`. The permit
  exposes `matches_context`, `deadline`, `limits`, and its exact effect/args.
  `record_outcome` and `settle_deadline` preserve immutable terminal decisions;
  deadline settlement does not establish physical cleanup.
- `ResourceLimits` supplies Wasm fuel/memory/table, wall, and output bounds.
  It is not a native process RSS/jail policy.

## Missing boundaries to implement

1. **Exact launch and private snapshot.** There is no Rust Proc executable catalog,
   executable-to-launch binding, native process/PTY backend, or process session
   supervisor. Q16 requires the actual bounded executable/argv/environment/cwd
   snapshot. Extend the durable effect to bind executable identity/digest,
   ordered argument/environment commitments, cwd, terminal parameters, policy,
   and limits while retaining the exact permission/catalog ID. Keep arbitrary
   strings private; prompts show trusted catalog identity, counts, bounds, and
   explicitly displayable fields. Represent explicitly Dangerous unsandboxed
   execution accordingly. A checked path followed by an unrelated path-based
   launch does not establish this binding.
2. **Keyed commitments.** Workspace HMAC-SHA256 and the legacy
   `tobkiri_canon::seal` exist, but no versioned, domain-separated process
   commitment API or its Core-private key custody exists.
   `Vault::secret_digest(name)` is an unkeyed name digest, not this primitive.
   Preserve existing canonical/seal semantics. Q16 adds no public-key signing
   requirement.
3. **Native cwd capability.** The separate filesystem work exposes
   `FsRegistry::{register_root, protect_root, issue_open_token, prepare_open}`
   and `FsDirectory::{root_id, rights, prepare}`. `FsDirectory` retains a
   private pinned `File` and bound scope; it exposes no checked native cwd
   accessor. Coordinate that small Core-private capability boundary with the
   filesystem owner. Do not reconstruct it from a root string, inode tuple,
   table rep, or pathname. A provisioned pinned empty cwd is also missing.
   `tobkiri-store::DataRoot::directory_capability` is private to Store and is
   not a Wasm cwd API.
4. **Physical lifecycle.** Reserve Core task/registration/queue control before
   construction and move actual child/FD/snapshot/input/output/permit custody
   into an owning-runtime supervisor before awaiting guest delivery. Resource
   Drop signals that owner; importer cancellation cannot destroy unfinished
   work. `host.rs::discard_instance` and
   `tobkiri-core/src/tracked_stream.rs` illustrate retained task/receipt and
   immutable-delivery patterns; neither is a reusable native process backend.

## I/O and completion boundary

Use bounded owner-funded stdin and stdout/stderr/PTY storage with finite
per-operation and aggregate output bounds. `CoreBuffer::builder` and
`CoreBufferBuilder::write_chunk` can reserve the complete borrowed region
before synchronous native reads. Retain submitted stdin through completed or
uncertain writes and output through delivery/discard. Service both process
output streams so an unread pipe cannot deadlock wait; enforce wall/output
limits and process-group cleanup even when the guest does not read.

One consumed spawn grant may cover that exact session's bounded methods under
the original epoch/deadline. Success requires exit, settled output, and reap.
An immutable unknown outcome, dropped resource, or returned timeout must not
refund unfinished native work or allow launch/partial-write replay. Keep actual
custody through cleanup and terminal handling; metadata delivery may retry the
same proposal without repeating execution.

## Minimum phased ownership

| Phase | Owner and files | Boundary |
| --- | --- | --- |
| 1 | Root: `crates/tobkiri-gate/src/{protocol,engine}.rs`, necessary Cargo changes | Durable launch binding, risk/redaction, private commitment boundary; no public WIT change is implied. |
| 2 | Filesystem owner: `crates/tobkiri-wasm/src/filesystem.rs` | Checked retained cwd capability; coordinate trusted empty-cwd provisioning with Root. |
| 3 | Proc implementer: new `crates/tobkiri-wasm/src/process.rs` and focused backend tests | Actual launch, bounded I/O, process/PTY session, cancellation, settlement, reap and native custody. |
| 4 | Root/integration owner: `crates/tobkiri-wasm/src/{effects,host,lib}.rs` and focused adapter tests | Trusted configuration, nominal resources, owned ABI conversion and session dispatch. |

Do not modify the filesystem owner's module concurrently. Unsupported native
platform paths remain unavailable. Enforce explicitly requested stronger OS
limits or reject the launch. Q16/R2.5 do not add universal OS jail, native RSS,
or detached-descendant containment prerequisites. True Native typed-port
launch remains incomplete; a Wasm CLI adapter uses Proc. No additional Astra
decision was identified by this inspection.
