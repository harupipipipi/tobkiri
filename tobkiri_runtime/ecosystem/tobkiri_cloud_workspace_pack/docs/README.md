# Tobkiri Cloud Workspace

This optional Pack prepares local work for a portable container. It does not
connect to a cloud account. AWS upload/deploy, public ingress, remote secrets,
PC/mobile authentication, background synchronization and remote execution belong
to a separate deployment Pack and approved Host ports.

The composer contribution displays the actual local checkpoint state beside
the input area. The Cloud workspace tab captures an explicit comma-separated
list of complete UTF-8 files from the selected Host workspace, imports a portable
capsule, verifies retained bytes, restores a local immutable copy, and prepares a
future handoff. Automatic source reads and all actions still require the selected
v4 operation, capability, Profile, Plan and Host approval policy. A UI declaration
does not supply approval or activate a missing provider.

`workspace-resource/export` returns a bounded base64 ZIP and its SHA-256 digest.
The archive can be transferred to another local Pack installation or consumed by
a future deployment Pack. A native file/download picker is not part of the
current neutral renderer; the public operation is the transfer boundary.

Capsules contain an immutable public Profile/Plan provenance reference, a local
revision and parent digest, a reviewed recipe digest, relative work paths and
content-addressed artifact bytes. Import validates all bytes before writing and
creates a new checkpoint bound to the current local Profile/Plan. An imported
Profile ID, Plan digest or handoff offer cannot activate grants, tools or a writer.
Live principals, writer leases, approvals, credentials, Host paths and another
Pack's state are never serialized. Known secret paths are rejected; files remain
explicitly selected work and are not claimed to undergo general secret scanning.

Limits are 128 files, 1 MiB per file, 8 MiB total work, a 10 MiB archive and 32 MiB
retained unique content per Profile. ZIP links, traversal, encrypted entries,
duplicate names, platform path aliases, excessive compression ratios, unlisted
blobs, altered manifests and mismatched content digests are rejected. Arbitrary
binary files can travel in imported capsules; selected Host capture uses its
existing complete UTF-8 inspection contract. Capture checks the selected mount
before and after reads; it is not a filesystem-wide frozen snapshot.

SQLite `BEGIN IMMEDIATE` serializes local checkpoint publications. Exact revision
CAS, parent digest, request replay and local writer epoch fencing prevent two local writers
from publishing the same head. Preparing a handoff releases that local writer.
Every local mutation carries its opening writer epoch; a request from before
writer release is rejected even when its checkpoint revision is still current.
The versioned offer describes a future receiver's expected head; it is not a
distributed lock or proof that a remote machine resumed. A deployment coordinator
must atomically accept/reject the expected head, fence the previous writer and
obtain fresh local approval before starting work.

`container/Dockerfile` is an actual nonroot container recipe. It validates and
restores the capsule into an empty writable mount, starts a fixed Python service,
and offers `/health` and finite `/workspace` metadata. It runs no user command and
has no write API, login, cloud client or secret mount. The approved Host must
resolve a digest-pinned base image, supply resource limits, drop privileges,
isolate networking, bind the input read-only and own the writable workspace.
The current Pack has no bound provisioning port, so it always reports container
start as unavailable. Developer Docker smoke evidence is separate from production
Host support and does not substitute for the Tobkiri PackVM backend.
`docs/container-smoke.v1.json` records an actual local Docker build and healthy
nonroot run using a digest-pinned cached worker base with Python 3.10.12. No image
was pulled. Exact capsule restoration, UID 65532, health and workspace endpoints
passed with networking disabled and resource limits; the test container was
removed. This does not activate production Host provisioning or cloud deployment.

Regenerate only this Pack's records from `tobkiri_runtime/`:

```sh
python -B ecosystem/tobkiri_cloud_workspace_pack/source_record.py
python -B ecosystem/tobkiri_cloud_workspace_pack/build_metadata.py
python -B -m pytest tests/test_cloud_workspace_pack.py -q
```

`integration-input.v1.json` is a source-reviewed adoption input. It provides exact
semantic operations, capabilities, effects, byte digests and UI references for
the integration owner. It is not a signature, install receipt, default selection,
runtime activation or release claim. This change intentionally leaves the global
catalog and existing Profile/user data untouched.
