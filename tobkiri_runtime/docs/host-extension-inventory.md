# Host Extension inventory

Nonauthoritative, read-only facts. This report never grants runtime admission.

## Totals

- Packs: 62
- Operations: 187
- Tracked Profiles: 1
- Tracked-Profile-reachable packs: 17
- Tracked-Profile-reachable operations: 63
- AI Runtime signals: 14
- Tool Runtime signals: 16
- No AI/Tool Runtime signal: 32
- Manual-review packs: 30
- Diagnostics: 0

## Profile inputs

| Profile | Authority | Schema | Packs | Edges | Source |
|---|---|---|---:|---:|---|
| `defaults` | authoritative_intent | valid | 19 | 64 | `ecosystem/defaultspack/v4/defaults.profile.intent.v1.json` |

## Pack facts

| Pack | Ops | Runtime signal | Reachable | Schema | Manual-review reasons |
|---|---:|---|---:|---|---|
| `rumi_ai_gateway_pack` | 3 | ai_runtime_signal | 2 | valid | runtime_signal_requires_human_review |
| `rumi_ai_modality_pack` | 4 | ai_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_ai_pipeline_pack` | 6 | ai_runtime_signal | 4 | valid | runtime_signal_requires_human_review |
| `rumi_ai_routing_pack` | 3 | ai_runtime_signal | 2 | valid | runtime_signal_requires_human_review |
| `rumi_ai_stream_pack` | 1 | ai_runtime_signal | 1 | valid | runtime_signal_requires_human_review |
| `rumi_ai_tool_bridge_pack` | 3 | ai_runtime_signal | 2 | valid | runtime_signal_requires_human_review |
| `rumi_ai_usage_pack` | 4 | ai_runtime_signal | 2 | valid | runtime_signal_requires_human_review |
| `rumi_browser_host_service_pack` | 2 | none | 0 | valid | - |
| `rumi_clipboard_host_service_pack` | 2 | none | 0 | valid | - |
| `rumi_coding_sandbox_service_pack` | 2 | none | 0 | valid | - |
| `rumi_command_protocol_pack` | 1 | none | 1 | valid | - |
| `rumi_context_runtime_pack` | 1 | none | 0 | valid | - |
| `rumi_conversation_store_pack` | 5 | none | 0 | valid | - |
| `rumi_credential_broker_pack` | 4 | none | 0 | valid | - |
| `rumi_default_tool_projection_pack` | 2 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_discord_connector_pack` | 1 | none | 0 | valid | - |
| `rumi_email_connector_pack` | 1 | none | 0 | valid | - |
| `rumi_file_inspect_pack` | 2 | none | 1 | valid | - |
| `rumi_file_mutation_pack` | 1 | none | 0 | valid | - |
| `rumi_file_patch_pack` | 1 | none | 0 | valid | - |
| `rumi_generic_webhook_connector_pack` | 1 | none | 0 | valid | - |
| `rumi_git_publish_pack` | 2 | none | 2 | valid | - |
| `rumi_git_read_pack` | 1 | none | 0 | valid | - |
| `rumi_git_write_pack` | 6 | none | 6 | valid | - |
| `rumi_host_authority_bridge_pack` | 6 | none | 5 | valid | - |
| `rumi_http_api_connector_pack` | 1 | none | 0 | valid | - |
| `rumi_human_operator_provider_pack` | 2 | ai_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_knowledge_store_pack` | 3 | none | 0 | valid | - |
| `rumi_line_connector_pack` | 1 | none | 0 | valid | - |
| `rumi_mcp_server_pack` | 2 | none | 0 | valid | - |
| `rumi_memory_store_pack` | 3 | none | 0 | valid | - |
| `rumi_model_catalog_pack` | 3 | ai_runtime_signal | 2 | valid | runtime_signal_requires_human_review |
| `rumi_model_evals_pack` | 3 | ai_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_model_registry_pack` | 5 | ai_runtime_signal | 2 | valid | runtime_signal_requires_human_review |
| `rumi_p2p_connector_pack` | 1 | none | 0 | valid | - |
| `rumi_prompt_studio_pack` | 5 | none | 0 | valid | - |
| `rumi_provider_adapters_pack` | 4 | ai_runtime_signal | 2 | valid | runtime_signal_requires_human_review |
| `rumi_provider_registry_pack` | 7 | ai_runtime_signal | 4 | valid | runtime_signal_requires_human_review |
| `rumi_repository_context_pack` | 4 | ai_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_shell_execute_pack` | 2 | none | 2 | valid | - |
| `rumi_slack_connector_pack` | 2 | none | 0 | valid | - |
| `rumi_subagent_placement_pack` | 6 | none | 0 | valid | - |
| `rumi_terminal_session_pack` | 2 | none | 0 | valid | - |
| `rumi_tool_approval_bridge_pack` | 1 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_tool_audit_pack` | 1 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_tool_authoring_pack` | 2 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_tool_broker_pack` | 1 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_tool_capability_executor_pack` | 1 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_tool_executor_selector_pack` | 1 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_tool_guard_pack` | 1 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_tool_local_executor_pack` | 1 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_tool_mcp_executor_pack` | 1 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_tool_policy_pack` | 1 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_tool_registry_pack` | 3 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_tool_remote_executor_pack` | 1 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_tool_result_pack` | 1 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_tool_sandbox_executor_pack` | 1 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_tool_validation_pack` | 1 | tool_runtime_signal | 0 | valid | no_tracked_profile_reachable_operation, runtime_signal_requires_human_review |
| `rumi_turn_runtime_pack` | 3 | none | 0 | valid | - |
| `rumi_workspace_mount_pack` | 2 | none | 0 | valid | - |
| `tobkiri_host_pack_control` | 23 | none | 23 | valid | - |
| `tobkiri_workflow_pack` | 20 | none | 0 | valid | - |

## Diagnostics

- None

## Limits

- Official schema validity is input validity, not runtime admission.
- Profile reachability requires explicit Pack inclusion but is not activation proof.
- AST I/O and `HOST_PROVIDER_FACTORY` observations are advisory only.
- Dynamic imports, reflection, native code, and runtime-added edges may be missed.
