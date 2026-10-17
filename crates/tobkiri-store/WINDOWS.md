# Windows storage backend

This backend admits a private directory on a local NTFS volume. Each path
component is opened relative to an already held directory handle. Reparse
points, remote volumes, unsupported filesystems, unprotected or foreign ACLs,
and regular files with multiple hard links are refused. Opening a private root
does not change an existing directory's ACL to make it admissible.

The absolute root and its ancestor names retain Windows spelling, including
ordinary names such as `Users`. **Descendants in the Core-owned storage
namespace must use lowercase ASCII letters, digits, `.`, `_`, and `-`.**
Windows reserved names, trailing dots/spaces, and traversal are also refused.
The backend rejects an ambiguous Profile/store/filename; it never lowercases
the logical authority key. A name accepted on a case-sensitive platform can
therefore be unsupported on Windows. Secret names inside the encrypted Vault
remain logical keys and are unaffected by this filesystem restriction.

Allocation ownership uses the actual directory's full volume and 128-bit file
identity. A retained native handle requests child-creation access while denying
write sharing, so another process cannot acquire an owner handle through a path
alias. Normal directory handles still permit metadata and Profile CAS work.
All pinned handles deny delete sharing, preventing root rename/replacement
while custody is live. The last owner lease releases the OS exclusion; there
is no mutable ownership slot, PID takeover, expiry, or proof substitute.

SQLite keeps its bundled `win32` byte-lock and I/O implementation. Registered
virtual paths route create/open/delete/metadata requests to the pinned native
directory. Guest DATA uses the common extent/reservation VFS and `LockFileEx`
locks on private, pinned, single-link lease files. Only the main database and
DELETE journal are admitted; WAL/SHM are refused without deleting them. The
current durable DATA receipt has a 64-bit inode field, so an NTFS identity that
does not fit that field is refused rather than truncated.

Unknown file cleanup retains the original custody. Unknown directory-lock
release returns an error, quarantines every DATA group in that physical
directory, and retains its already admitted native lock. Later calls cannot
turn that uncertainty into success. Process exit is the recovery boundary for
such retained OS state.

The encrypted Vault uses the existing XChaCha20-Poly1305 format and Profile-bound
authentication data. Vault updates, key initialization, and Profile CAS use
one native Profile lock. The normal Windows key provider remains the OS
credential store; tests use explicit fixture keys and do not read or change
real credentials.

Windows DATA takeover/move recovery is not implemented and continues to return
`Unavailable`. This backend does not change the existing Unix platform proof
requirements. An execution token that cannot perform the required relative
native opens also receives an error; the backend does not relax the ACL or
path checks for an AppContainer.

Focused validation commands (run in an ordinary permitted Windows process):

```powershell
cargo test -p tobkiri-store --test owner_windows --test windows_storage_names
cargo test -p tobkiri-store --lib vfs::tests::
cargo test -p tobkiri-store --lib vault::windows_tests::
cargo test -p tobkiri-store --lib quota_vfs::directory_lock_tests::
cargo test -p tobkiri-store --lib secure::windows::tests::
```

These tests cover native ownership, aliases, rename exclusion, child-process
termination, output custody, SQLite persistence and byte-lock contention,
quota rollback, ambiguous-name rejection, encrypted concurrent updates, and
unknown-unlock retention. A passing compilation or isolated fixture does not
prove successful Core/Gate/Wasm execution; record that acceptance separately.
