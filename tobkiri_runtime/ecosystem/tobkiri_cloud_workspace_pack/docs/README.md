# Tobkiri Cloud Workspace

The optional local task path prepares JSON argv such as `["python3", "main.py"]`
against one checkpoint. The ordinary Host approval window presents that immutable
plan. After approval, Resume starts one COW container through the existing bounded
Host process runner in the Coding Sandbox Service Pack. Apply verified output
publishes a new capsule only if the opening revision, parent and writer epoch are
still current. This performs a local coding task; AWS upload/deploy remains deferred.

The task recipe is source-reviewed and pins the cached
`rumiai/mimo-coding-company-worker` OCI digest. Its host provider never pulls or
builds an image, runs a Host shell, redeems legacy receipts, or trusts client
`approved` flags. It uses nonroot UID/GID, no network, no capabilities, a read-only
root, fixed memory/CPU/PID limits and read-only input/driver mounts. Work runs in
an 8 MiB tmpfs with at most 512 inodes; `/tmp` is 16 MiB/256 inodes. A reviewed
guest driver captures task output and a capsule before the tmpfs disappears.
Input is limited to 4 MiB of work, with a 5 MiB archive bound. stdout
and stderr are each capped at 32 KiB. Task plans expire after 60 seconds and bind
the captured Profile, Plan, security epoch, exact argv, image and checkpoint.
The journal permits at most 16 retained tasks and 32 MiB of retained capsules.

Task execution is consumed once. The same nonce with a different request is
rejected; retries return the sealed plan or retained receipt. An interrupted
running journal is never automatically executed again. Cancellation/current
capture fencing stops the Docker CLI and then removes only its journal-owned
container ID only after checking its private random ownership label. A pinned
local Unix socket and daemon identity prevent a Docker context switch; the CLI
uses an empty Pack-owned config and no inherited Docker credentials. A Docker
daemon state observation establishes whether the workload
started. Verified absence after cleanup is required before reading output through
bounded descriptor-pinned file inspection. Unverified cleanup remains ambiguous
and produces no output capsule. A nonzero workload or container exit also
produces no output capsule. Output rejects links, secret paths, special files,
nonportable names and excess directory entries.

The composer source contribution and workspace tab expose prepare/resume/status/
cancel/apply controls. They require reviewed optional Pack selection and the
finite signed edges in `docs/task-adoption.v1.json`; declarations create no grants
or automatic activation. Missing current recipe data, approval routes, Docker or
the cached pinned image fail closed. No legacy execution fallback is used.
The Host still coordinates the work. A self-contained Tobkiri agent inside the
metadata service image, background PC synchronization and a shared PC/mobile
cloud session remain outside this first local task checkpoint.

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
retained unique content and 32 MiB restored copies per Profile, with at most 256
retained checkpoint requests. V1 accepts only uncompressed ZIP_STORED members;
declared compressed and uncompressed sizes must agree. ZIP links, traversal, encrypted entries,
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
and offers `/health` and finite `/workspace` metadata. This is a capsule container
with a metadata service. It does not include a Tobkiri task executor or claim that
a user workload ran. It runs no user command and
has no write API, login, cloud client or secret mount. The Dockerfile fixes the official Python 3.13 slim OCI index digest in its FROM
line; the recipe digest includes those bytes and the base reference. The approved
Host must supply resource limits, drop privileges,
isolate networking, bind the input read-only and own the writable workspace.
This metadata recipe has no bound provisioning port, so its start remains
unavailable. The captured task provider executes work in a separate COW container
and reports its own task state. Developer Docker smoke evidence is separate from production
Host support and does not substitute for the Tobkiri PackVM backend.
`docs/container-smoke.v1.json` records a historical local Docker build and healthy
nonroot run using a digest-pinned cached worker base with Python 3.10.12. No image
was pulled. Exact capsule restoration, UID 65532, health and workspace endpoints
passed with networking disabled and resource limits; the test container was
removed. That source used the cached worker base. Subsequent import hardening and
the fixed official Python base produce a different recipe digest. The current
recipe has not been rebuilt or run in Docker: retries stopped at host disk
exhaustion and Docker EOF. Focused tests verify current restore code and runtime
recipe checks. No current-image health or workload execution is claimed. Neither
result activates Host provisioning or cloud deployment.

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
