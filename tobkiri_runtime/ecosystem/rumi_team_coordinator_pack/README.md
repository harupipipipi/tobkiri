# Rumi Company Coordinator Pack

Routes stored inbound records to an explicitly mentioned, uniquely routed, or
explicitly configured fallback member. Ambiguous and missing routes remain
unassigned. Company state stays authoritative in the state-store pack and work
is selected only through the global `rumi.action.team.work.v1` contract.

The coordinator also exposes a `company.supervisor` global job adapter for
bounded priority dispatch and projects cancellation to the selected work
adapter before recording the state transition. It imports no connector,
scheduler, agent, or Company state implementation.

