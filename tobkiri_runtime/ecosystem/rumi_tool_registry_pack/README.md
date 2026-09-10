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

Each registry factory requests the same sealed `tools/*/manifest.json` descriptors from
Defaults (109) and Default Tools (30) when their Packs are selected in the Profile. The Host
captures declared data using the Profile lock's artifact digest and admitted
Pack root, independently of executable bindings. It supplies immutable bytes;
request payloads cannot choose files or digests. Selected missing, modified or
empty descriptor sources fail closed. An unselected Pack adds no definitions.
Defaults' existing tool format uses `config.tool_id` for identity and `name` for
display text; neither format can rename a descriptor from another file's ID.
List results include each captured Pack ID and artifact digest. Management and
migration reject definitions and aliases claiming these packaged names before
creating state, locks or backups. Initial migration must separate packaged
definitions from custom records; conflicting legacy snapshots are rejected
without silently dropping or overwriting their contents.

Packaged descriptors compose with stored definitions and selected contributions;
duplicate IDs or aliases are rejected. This intake preserves their schemas,
display metadata and widgets without importing the legacy Registry or Executor.
It covers sealed tool manifests only. Dynamic, component and memo sources, their
migration, and the local execution Provider still require separate integration.

The shipped Defaults Profile selects this owner and Default Tools. Its
authenticated `/api/tools/catalog` contract route reads the catalog for the
existing tool picker. The application projects only display metadata and the
registry revision. Local tools appear connected only when their exact selected
Provider and operation have a ready backend; otherwise their descriptors remain
visible as unavailable. Remote tools require separate connection-owner health
integration. Neither the read edge nor the UI projection authorizes execution.

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
