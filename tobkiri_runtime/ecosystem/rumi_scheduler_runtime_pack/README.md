# Rumi Scheduler Runtime Pack

Owns only the process clock, due-work coordination, and active dispatch state.
It claims exact schedule leases and dispatches a global `rumi.action.job.v1`
action. It imports no chat, agent, Company, connector, or product runtime.

