# Compatibility

This pack declares compatibility with defaultspack `AgentRunStore` records without creating a second run database. Workroom records reference the existing `run_id`, optional `execution_id` alias, `session_key`, `parent_run_id`, `root_run_id`, `current_transcript_id`, checkpoint references, and replay indexes only.

The accepted run status vocabulary follows defaultspack `RunStatus`: `created`, `queued`, `running`, `waiting_approval`, `waiting_user_input`, `compacting`, `paused`, `completed`, `failed`, `cancelled`, `stale`, `resumable`, `planned`, and `error`.

This pack does not register or replace routes such as `/api/agent/*`, `/api/coding/*`, `/api/dev/replay`, `/api/scheduler/*`, `/api/tools/*`, or `/api/browser/*`. Route, tool, browser, scheduler, file, metrics, and subagent PR behavior remains owned by adjacent runtime packs.
