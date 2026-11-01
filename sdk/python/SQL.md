# Named DB-API subset

The SDK binds a caller-selected generated `tobkiri:core/storage@1.1.0` interface.
It does not reinterpret the old untyped SQL calls or open native SQLite.
Store names must be manifest declarations admitted by Core.

`connect`, `cursor`, `execute`, `executemany`, `fetchone`, `fetchmany`, `fetchall`,
cursor iteration, `arraysize`, `description`, `rowcount`, `commit`, `rollback`
and `close` form the supported subset. Transactions begin lazily as DEFERRED.
Closing rolls back pending work and explicitly drops resources; it never
commits. Backend errors remain errors, including an uncertain commit outcome;
a failed delivery after commit cannot be described as rollback.

Every `execute` invokes `exec-typed` once with `allow-rows`. The bounded engine
classifies the same prepared statement before step and obtains WRITE authority
before mutation. The SDK does not inspect SQL prefixes or retry queries as
writes. `executemany` uses `forbid-rows` in one transaction: any prepared column
count rejects before step, even if zero rows would be returned. Batch failure
rolls back all pending work.

Parameters are positional sequences of `None`, signed 64-bit `int`, finite
`float`, Unicode `str` or `bytes`, encoded as exact tagged cells. Booleans,
records, named parameters, implicit converters, scripts and modes other than
DEFERRED are rejected. Results are fully bounded and materialized; overflow
fails instead of truncating. `row_limit` and `batch_limit` default to 1000 and
accept an explicit bound up to 10000 without expanding Core authority.

Column order and duplicate names survive a zero-row query. `description`
contains seven fields: the name and six `None` values. `rowcount` is `-1` when
affected rows are unknown. `lastrowid` is the latest **connection-level** engine
snapshot, shared by all cursors, and may be `None`; this does not claim
cursor-local CPython sqlite3 parity.

Native SDK tests use record-only resources to check routing, tags, metadata and
lifetimes. They do not prove the bounded backend, Gate dispatch, Python wasm
execution or full legacy DB-API parity. Runtime fixtures and every migration
inventory status remain pending.
