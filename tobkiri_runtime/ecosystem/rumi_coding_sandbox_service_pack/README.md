# Rumi Coding Sandbox Service Pack

Stages a bounded copy-on-write workspace with secret and symlink exclusion.
Observe and control are separate contracts. Prepare, write, patch, execute, and
discard each redeem an exact receipt. Execution uses only a locally available,
digest-pinned Docker image with network disabled, capabilities dropped,
no-new-privileges, read-only root, and resource limits. It never falls back to
host execution and never applies changes to the host workspace.
# Captured Tobkiri workspace tasks

The new `runtime/host_v4.py` providers prepare, execute and read finite portable
workspace tasks through authenticated V4 Broker capture. Legacy `runtime/sandbox.py`
and its compatibility declarations remain unchanged. The new path reads only a
live owner-fenced Cloud capsule and immutable declared recipe data, stages its
own bounded copy, and uses `HostBoundedProcessRunner` for exact Docker argv.

Task execution requires the ordinary `workspace_task` interactive approval route.
The provider accepts no client authority flags or legacy receipts. Captured source,
Profile/Plan/security epoch, nonroot execution, a cached digest-pinned image,
network isolation, one-shot durable claims, cancellation and verified daemon-side
container cleanup are checked before output is retained. Source/adoption inputs
are local to this Pack; ROOT must review exact finite signed edges before activation.

