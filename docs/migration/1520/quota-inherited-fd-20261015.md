# Quota lease lifetime and inherited descriptors — 2026-10-15

The real SQLite quota regression intermittently failed during parallel Store
tests. In `snapshot-store-quota-bounded-repeats20-20261015`, all 20 standalone
runs passed, but the first library run with eight test threads failed at
`initial-insert`. The closed diagnostic recorded `SQLITE_IOERR_DELETE` (2570)
and an exclusive journal `open-lease` conflict with Darwin `EAGAIN` (35). It
recorded no directory-lock or delegated SQLite BUSY site. This failure occurred
before the oversized write that must return `DiskFull`.

The private lease previously ended by closing its file descriptor. `flock`
belongs to the open file description: a transient fork copy can keep it alive
after the issuing process closes its descriptor. `O_CLOEXEC` closes the copy at
exec, but does not remove the interval before exec. A deterministic OS-only
pipe/fork experiment confirmed that close alone leaves an exclusive acquisition
blocked while the child holds the inherited descriptor; explicit unlock before
close permits acquisition with that descriptor still open. The original failing
child's descriptor was not captured, so that exact child identity is not part of
the evidence.

## Ownership and failure behavior

[quota_vfs.rs](../../../crates/tobkiri-store/src/quota_vfs.rs) now keeps the
original descriptor, issuing process ID, and held state in a private `Lease`.
After confirmed successful delegated SQLite `xClose`, the issuing process must
successfully perform `LOCK_UN` before releasing the existing file custody. A
fork child's drop closes its copy without explicitly unlocking the parent's
still-live lease. Independently opened process leases remain independent and
continue to block unlink.

If native `xClose` or explicit unlock fails, the domain is quarantined and the
same preadmitted `FileState` is retained, including its descriptor, owner lease,
and Core control registration. No recovery allocation or custody refund occurs.
For an unpublished or scope-only lease, failed unlock retains its existing
descriptor. The SQLite lock callbacks, quota limits, and strict rollback/reopen
assertions remain unchanged; no sleeps, retries, or suite serialization were
added.

## Actual regression receipts

Both successful receipts record identical before/after sources, exit code 0,
and no guard stop. Their `.json` and `.log` files are in the parent workspace's
local `.work/evidence/` directory.

| Receipt label | Actual result |
| --- | --- |
| `snapshot-store-quota-lease-repeats25-20261015` | 20 exact standalone quota runs, then 20 complete library runs with eight test threads; all passed. Each library run contained 36 tests. |
| `snapshot-store-full-lease26-20261015` | Full Store suite: 113 passed, 0 failed, 0 ignored. |

The bounded runner used these commands from the repository checkout:

```sh
cargo test --offline --locked -p tobkiri-store --lib --no-run --message-format=json
```

It selected the reported library test executable, then invoked it 20 times per
case with a 20-second process bound:

```text
<test-executable> --exact quota_vfs::tests::real_sqlite_quota_rolls_back_and_survives_reopen --nocapture
<test-executable> --test-threads=8 --nocapture
```

The full-suite receipt used:

```sh
cargo test --offline --locked -p tobkiri-store --no-fail-fast
```

The library regressions cover a duplicate descriptor surviving journal lease
release, an actual fork child's inability to unlock the parent, independently
opened child-process denial, and retention of the actual Core reservation and
OS lease after an injected unlock failure. Unknown-close retention and the real
SQLite `DiskFull`, rollback, and reopen checks also passed.

The validated `quota_vfs.rs` SHA-256 is
`1ca9cc37a0e61c0f2b4e87befa641805f4d8d80e051e34cd4034a454a8766479`.
These bounded results support this lease-lifetime correction; they do not prove
absence of every concurrency defect. The fork fixture never calls SQLite in
the child. This record does not establish support for using inherited SQLite
connections after fork, protection from a hostile same-UID process, or completion
of the wider migration audit.
