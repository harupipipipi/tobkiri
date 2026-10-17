# Sequential native User sessions

`tobkiri profile session --profile <ID>` is a new thin CLI host mode. It opens
Core and starts the selected Profile once, then keeps that exact Session while
the operator supplies individual JSON-lines requests. This does not change Core,
WIT, Pack permissions, approval, accounting, deadlines or quotas. No measured
performance improvement or production-host acceptance is implied.

An invocation line has exactly these fields:

```json
{"id":1,"shape":"local.example.control@1.0","op":"status","args":{"key":"example"}}
```

`id` is a positive integer no larger than 9007199254740991 and must strictly
increase in this process. It is a transport label, not a Core request ID,
authorization or durable replay permit. `shape` and `op` use the existing typed
identifiers; `args` must be an object. StrictV1 rejects duplicate keys and unknown
request fields. Each UTF-8 request is limited to 65,536 bytes, excluding its line
ending, and must end with a newline. Empty input EOF ends the Session normally;
an unterminated final request is rejected without dispatch.

For normal interactive shutdown, enter a separate native control line:

```json
{"id":3,"close":true}
```

It has only `id` and `close:true`, is mutually exclusive with invocation fields,
and obeys the same increasing-ID rule. `false`, mixed fields and unknown fields
are rejected. The command performs no Gate/model/tool call. It stops further
input, emits `session_closing` with its ID, and awaits the outer `Session.close`
before `session_finished` and exit. This is explicit operator close, not evidence
of physical stdin EOF; no special CtrlZ or piped-approval behavior is introduced.

The command reader and approval UI share the same cancellable native input and
terminal mutex. Neither uses a separate buffered stdin reader. Enter one request,
then respond to any ordinary Tobkiri trusted approval prompt. Wait for that
request to finish before entering another. Piped input and `--non-interactive`
continue to be unavailable as human approval; there is no yes/bulk-approval flag.
The host does not prefetch the next command while a call, stream or approval is
in progress.

The new mode writes these native transport markers, with the existing raw typed
User value/stream output between each request's markers:

```json
{"kind":"session_ready","protocol":"tobkiri.user-port-session/1","max_request_bytes":65536}
{"kind":"request_started","id":1}
{"kind":"request_finished","id":1,"ok":true}
{"kind":"session_closing","id":3}
{"kind":"session_finished","ok":true}
```

In `profile session` and `profile invoke`, stdout contains only native transport
markers and typed Core return values. Pack terminal display, write text and read
prompts go to stderr, selected before Core opens. Pack text may look like a JSON
marker; consumers must parse stdout separately and must not parse merged
stdout/stderr as the native protocol. Interactive `profile run` keeps its normal
console output on stdout. The trusted approval surface and shared input/terminal
mutex are unchanged. Budgeted output and prompts propagate write/flush errors
without falling back to stdout; the two legacy void display/write callbacks
retain their best-effort error behavior.

The normal one-shot `profile invoke` typed output remains unchanged. Both modes use
the same dispatcher: `Session.invoke_user_port`, producer-owned output encoding,
stream pulls through EOF, and awaited stream cancellation/close. Every operation
still receives a fresh Core RequestId, deadline and allocation scope under the
Session's approved document/revision/epoch. A changed Profile requires a new
Session; keeping a process alive does not refresh authority.

Malformed input, repeated IDs, cancellation, invocation/cleanup uncertainty or
output failure stops the loop. No later line is read or dispatched. The host
awaits `Session.close` even after idle EOF, records a failing session marker when
possible and exits nonzero on errors. Operation and Session cleanup failures are
both preserved. A displayed marker alone is not permission to retry: validate
the typed Core result, complete transport capture and durable operation outcome.
Ordinary Pack terminal display is not a trusted approval channel. This mode does
not automatically generate, reconcile, resume, retry or chain an operation.

The implementation has source-level parser, sequencing, failure/EOF and native
input-boundary regressions. Actual same-Session native/host acceptance and startup
timing must be recorded separately; dedicated success does not replace the
remaining Core/directory fixture failures or certify all hosts, legacy tools,
Defaults, UI, MiMo workflow completion or Terminal-Bench.
