# Tobkiri Tool Registry Pack

Owns provider-neutral tool definitions, schemas, widget projection metadata,
and finite aliases. It does not import or execute domain services and does not
grant approval or capability authority.


The canonical Host factory in `runtime/host.py` binds the read, manage and
migration Functions separately to the captured Profile, data root, principal,
activation and plan. Calls use an explicit `operation` field. Reads support
`list`, `get` and `resolve`; management supports `save`, `delete` and `alias`;
migration supports `migrate` and `rollback`. Management and rollback require the
current revision; initial migration requires an empty destination and the exact
source hash. All calls pass through the Broker's declared Profile authority.
Saving a definition cannot shadow an existing alias. Rollback rejects stale
revisions and preserves the current state in a backup before removing it.
Registration of a definition does not authorize its execution.

Read results omit private migration backup paths. Optional contributions are
queried using their selected canonical operation, with no credentials. Host
mutation lock waits recheck cancellation and expiry, keeping the existing lock
identity and on-disk data layout for compatibility. Ordinary Defaults tool
projection, execution and live data migration require their own captured edges
and acceptance checks.

Primary state, migration backups and lock entries use the shared pinned-directory
persistence operations. Symlinked, hardlinked and replaced captured paths are
rejected. Reads preserve directory permissions and reject state larger than
16 MiB; writes enforce the same limit and check invocation lifetime immediately
before publication. Invalid stored revisions are rejected without coercion or
automatic repair. The existing exclusive-file lock protocol still excludes
legacy writers; switching this owner does not introduce a separate lock domain.
