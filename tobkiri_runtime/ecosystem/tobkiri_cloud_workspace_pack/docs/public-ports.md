# Versioned boundaries

The Pack publishes `tobkiri.resource.cloud.workspace.v1` through
`tobkiri_cloud_workspace_pack.workspace-resource` and
`tobkiri.action.cloud.workspace.v1` through
`tobkiri_cloud_workspace_pack.workspace-manage`.
The exact finite request schemas and effect classes are in `pack-source.v1.json`.
Host Profile/Plan/security epoch and presentation principal come from the
authenticated envelope. Client `approved`, actor, lease and path fields fail
closed. Public workspace metadata and file inspection are optional dependencies;
local initialized or imported capsules work without them or network access.

`tobkiri.workspace-capsule.v1` uses `manifest.json` and `blobs/<sha256>` ZIP
members. `manifest_digest` hashes canonical JSON excluding that one field.
The blob set must exactly match the manifest. Public provenance does not convey
authority. The reviewed container recipe digest joins the immutable manifest.

`tobkiri.workspace-handoff.v1` fields are `workspace_id`, `checkpoint_digest`,
`source_revision`, `expected_receiver_head`, `status: prepared_locally`, and
`remote_execution: unavailable`. The expected receiver head is a CAS precondition
for future coordination. The offer contains no lease, approval or private path.
Local writer release is recorded separately in the Pack's private store.

Future `tobkiri.action.workspace.container.v1` must be a Host-owned approved
provision/start/status/stop boundary. It must capture the artifact/recipe/image
digests, immutable Profile/Plan, source revision, lease ownership and execution
domain; reject arbitrary shell/privileged flags; constrain resources/network;
and return actual provider evidence. No provider for that contract is added here.

Future `tobkiri.service.workspace.deploy.v1` belongs to the deferred second Pack.
It should accept a verified capsule reference and receiver CAS condition, obtain
its own fresh cloud credentials and approval through public Host ports, and
return deployment identity and execution evidence. Credentials, local guard
state and live leases must never be imported from a capsule. No upload/deploy
provider or cloud SDK is added here.
