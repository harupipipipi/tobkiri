# Rumi Company Surface Pack

Owns a removable, isolated, profile-scoped Company route. The surface reads
only the global Company state and coordinator runtime resource contracts; it
does not import a Company store, Company coordinator, agent runtime, connector,
or scheduler implementation.

The route is intentionally read-only. Company changes and task dispatch remain
behind separately approved action contracts, so this UI cannot issue or redeem
authority receipts.

