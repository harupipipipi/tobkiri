# Rumi Coding Sandbox Service Pack

Stages a bounded copy-on-write workspace with secret and symlink exclusion.
Observe and control are separate contracts. Prepare, write, patch, execute, and
discard each redeem an exact receipt. Execution uses only a locally available,
digest-pinned Docker image with network disabled, capabilities dropped,
no-new-privileges, read-only root, and resource limits. It never falls back to
host execution and never applies changes to the host workspace.

