# Rumi AI Stream Pack

This pure service owns the global stream event schema and normalization. It
preserves text, thinking, tool-intent, usage, finish, and error as distinct
types; binds request ID, sequence, and provider attempt; rejects unknown event
types and data emitted after a terminal event; and requires a terminal event.

Validation was not executed by the implementation agent. Independent testing
is required before merge, including malformed iterables, ordering, terminal
events, error streams, usage events, and replacement/removal.

