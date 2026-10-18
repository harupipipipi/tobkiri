# Optional native request metrics

Build `tobkiri-cli` with `--features closed-request-metrics` to enable numeric
diagnostics in `profile session`. The feature is off by default; one-shot
`profile invoke` output and the normal session protocol remain unchanged.
The same ordinary Core authorization, deadline, admission and audit paths apply.

When the observation bank was admitted and terminal output succeeds, this build
emits one `request_metrics` row after `request_finished`, with format
`tobkiri.closed-request-metrics/1`. Bank admission or output failure can leave no
metrics row; absence is not zero-cost or success evidence. The fixed 24-slot bank
and 8 KiB encoder
scratch are prepaid from the request's Core control account. Descendants retain
the same bank and its funding through their actual lifetimes. Observing a bank
does not grant authority. Synchronous Store hooks receive a scoped observation;
no thread-local observation crosses an async suspension.

Each observed slot contains seven unsigned integers, in order:
`started`, `returned`, `dropped_unfinished`, `elapsed_ns`, `max_ns`, `units0`,
`units1`. `returned` includes both Ok and Err; it does not mean success. A dropped
or panicking span is counted separately. Consumers must preserve u64 precision.
An unobserved/reserved slot is null. Any counter overflow invalidates all slot
values and sets `overflow`; null does not mean zero.

| Slot | Observation | Units |
| --- | --- | --- |
| 0 | Request wall duration | none |
| 1 | Ordinary approval wait | none |
| 2 | Metered allocation | requested bytes, work |
| 3 | Prepaid conversion allocation | requested bytes, work |
| 4 | Current admission fault + epoch joined SELECT | none |
| 5 | Current admission fault SELECT | none |
| 6 | Current security epoch SELECT | none |
| 7 | Current Pack permission SELECT | none |
| 8 | Current User permission SELECT | none |
| 9 | Selected approval DB mutex waits | none |
| 11 | Selected audit DB mutex waits | none |
| 12 | Existing complete audit image scan | rows, TEXT/BLOB bytes |

Slot 4 counts one fresh combined statement, which still performs two singleton
lookups. Slots 5/6 continue to count standalone or fallback statements. Consumers
summing selected SQL statements must include 4, 5, 6, 7 and 8; they must not infer
physical row/B-tree work from that count. A failed joined prepare/step is observed
before the original fault -> active -> epoch read-only fallback. Fault/type
errors and cancellation retain that priority; no authority/epoch value is cached.
The epoch is physically observed in the joined SQL snapshot before the active
check, rather than by a later standalone statement. This preserves decision
priority, not every possible timing of the old two-read sequence. It does not
make the interval from SQL completion through the subsequent guard atomic against
a database writer. Both pre-Core-lock and in-Core-lock guard checks and all
permission/owner checks remain. The 24-slot bank, encoder bound and request
deadline are unchanged.

Durations are inclusive and can overlap; summing them does not give request or
CPU time. The terminal snapshot is not atomic with live descendants, and unfinished
counts are not a physical cleanup receipt. Cold Core startup, unscoped background
work, semantic audit verification and terminal outcome delivery are not fully
attributed. Coverage fields remain false. Output contains no SQL, body, secret,
path, Pack identity or model content. A metrics output failure still stops the
session and preserves the original operation/cleanup error chain.

Use a model-free request and compare its unchanged typed result before drawing
performance conclusions. A unit-test count or a provider generation latency is
not a measurement of this native request path.
