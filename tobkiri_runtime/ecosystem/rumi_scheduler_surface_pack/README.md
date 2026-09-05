# Rumi Scheduler Surface Pack

Owns scheduler content and a removable, isolated, profile-scoped UI route. The
surface reads only the global schedule and scheduler resource contracts. It
does not import scheduler storage/runtime implementations and does not issue
authority receipts.

Mutating controls intentionally remain in receipt-aware tool/action adapters;
the isolated UI cannot self-approve schedule changes.

