# Tobkiri Agent Runtime Service Pack

Coordinates context, AI generation, bounded tool loops, approval wait/resume,
subagent spawning, cancellation, steering, handoff, turn lifecycle, and result
message projection exclusively through global contracts. Agent profiles and run
state remain owned by `rumi_agent_state_store_pack`.


For new runs, execution and resume use the definition snapshot returned by the
state owner's `run.begin`, including when a definition changes between the
initial existence check and admission. Legacy runs without a snapshot retain
current-definition semantics. Model/provider configuration and Host grants are
not frozen by an Agent snapshot; their existing validation still applies.
Terminal replay after deleting the saved definition remains an existing
limitation because the initial definition-existence preflight is retained.
