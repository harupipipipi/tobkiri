# Rumi Agent Workroom Pack

Declarative async agent workroom contracts for run state, plans, progress events, checkpoint/resume, intervention, replay, and run-board review.

This setup pack makes Rumi more customizable by adding a domain contract that can be selected independently from defaultspack. It is intentionally local-first, declarative, and reviewable: it creates schemas, workflow packets, quality gates, and handoff records instead of executing adjacent runtime actions.

## Provides

- agent_workroom_session
- run_event_log
- task_plan_contract
- progress_event_contract
- checkpoint_resume_contract
- interrupt_redirect_cancel_contract
- deterministic_replay_index
- run_board_ui_contract

## Does Not Provide

- tool execution
- browser action
- desktop action
- schedule execution
- file persistence
- metrics collection
- subagent PR management
- model routing

## Required Secrets

None. Network is denied by default and the pack contains no executable runtime code.

## Defaultspack Promotion

Not eligible by default. Promotion requires:

- no_durable_run_event_bus
- no_signed_interrupt_tokens
- tool_execution_owned_elsewhere
- metrics_owned_by_defaultspack
- file_persistence_owned_by_defaultspack

## Overlap Rule

If another pack can perform a step, Rumi should prefer the narrower owner surface. This pack emits a Handoff packet whenever the request crosses into runtime execution, connector IO, persistence, scheduling, or multi-agent orchestration.
