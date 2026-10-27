# Rumi Schedule Store Pack

Owns canonical profile schedules, dispatch leases, retry counters, cancellation,
and run status. It does not own a clock and never imports or executes chat,
agent, Company, connector, or product implementations. Every state transition
is revision-bound and consumes an exact one-shot authority receipt.

