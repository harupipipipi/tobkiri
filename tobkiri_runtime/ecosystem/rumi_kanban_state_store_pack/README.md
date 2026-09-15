# Rumi Kanban State Store Pack

Owns profile-scoped Kanban boards, columns, cards, and bounded audit events.
It provides no UI and imports neither chat, Company, agent, connector, nor
scheduler implementation. Every mutation is revision-bound and receipt-gated.

`migration.import_snapshot` accepts a caller-supplied legacy board snapshot
once. It records the normalized source hash and rejects a different retry, so
the old Kanban store can never become a live fallback or a dual writer.

