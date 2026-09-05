# Rumi Desktop Host Service Pack

This pack owns desktop window/observation/capture and input/control operation
descriptors. It never imports an OS driver or executes an input event. The core
Authority path approves caller-bound `host_intent` values and the Viewer host
broker remains the only desktop executor.

Observation and control are independently permissioned contracts. Client
approval flags and tokens are rejected.

Validation was not executed by the implementation agent.
Independent testing is required before merge.

