# Rumi Connector Outbound Broker Pack

Owns delivery IDs, bounded message retention, deduplication, retry, cancellation,
and redacted status. It consumes a user-facing receipt, then issues an exact
downstream receipt bound to the selected vendor pack, connector registry
revision, connector metadata, and message. Credentials never enter the broker.

