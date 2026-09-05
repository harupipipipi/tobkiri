# Rumi Provider Registry Pack

This pack is the authoritative owner for configured provider instances. A
record references an adapter ID, optional endpoint, and opaque credential
handle. It contains neither provider catalog data nor provider execution code.

Remote capability and health are `unknown` until fresh, verified adapter
evidence is stored. Client-supplied optimism is never treated as availability.
Mutations require an expected revision; migration is source-hash pinned and
retains an owner-only rollback backup.

Validation was not executed by the implementation agent. Independent testing
is required before merge, including startup, stale revision, secret rejection,
unknown health, migration, rollback, and pack-removal isolation.

