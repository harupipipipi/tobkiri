# Rumi Credential Broker Pack

This pack is the authoritative owner of encrypted provider credential handles.
Public operations expose only configuration metadata. Secret material can be
resolved only when the manifest-bound caller pack, provider instance, requested
scope, and expiry match the stored record.

The consumer pack identity is injected by the restricted global contract
client and overwrites any payload value. Gateway, router, catalog, logs, and UI
receive only `credential:*` handles. Management requires approval; provider
resolution does not allow a pack to approve itself.

