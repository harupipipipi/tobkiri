# Rumi Agent Continuity Pack

    `rumi_agent_continuity_pack` is a declarative setup pack for long-running agent continuity, restart evidence, compaction handoffs, branch resume notes, and human-readable health packets. It adds schemas, policies, examples, review gates, and handoff packets while leaving runtime execution to the existing owner packs.

    ## Required Secrets

    None.

    ## What It Provides

    - `continuity_packet_contract`
- `restart_evidence_packet`
- `compaction_handoff_packet`
- `attention_drift_recovery_note`
- `run_summary_for_resume`
- `continuity_artifact_manifest`
- `branch_resume_packet`
- `agent_health_note`

    ## Does Not Provide

    - run boards live run events checkpoints interventions replay indexes
- memory objects recall storage knowledge updates skill learning
- metrics telemetry run ledgers cost latency postmortems
- schedules monitors wakeups retries recurring follow-ups
- file artifact persistence and export
- git branch creation switching push

    ## Handoff Boundaries

    - `run_board_events_checkpoints` -> `handoff_to_rumi_run_lifecycle_pack`
- `memory_storage_recall` -> `handoff_to_rumi_memory_knowledge_pack`
- `metrics_telemetry_ledgers` -> `handoff_to_rumi_observability_pack`
- `schedules_wakeups_retries` -> `handoff_to_rumi_workflow_scheduler_pack`
- `artifact_persistence_export` -> `handoff_to_rumi_workspace_pack`
- `git_branch_mutation` -> `handoff_to_defaultspack_or_coding_owner`
- `continuity_packets` -> `owned_by_rumi_agent_continuity_pack`
- `restart_evidence` -> `owned_by_rumi_agent_continuity_pack`
- `tool_aliases` -> `prefer_explicit_pack_namespace`
