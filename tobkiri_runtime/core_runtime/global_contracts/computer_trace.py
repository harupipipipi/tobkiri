"""Secret-free global trace contract for Computer Use actions.

This module intentionally does not accept arbitrary mappings.  Callers may
only emit the fixed scalar fields below, so payloads, typed text, URLs,
clipboard contents, approval tokens, environment values, and window titles
cannot accidentally enter the trace file.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterator


_TRACE_CONTEXT: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "rumi_computer_use_trace_context", default=None
)
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_BOOL_FIELDS = frozenset(
    {
        "target_app_present",
        "target_bundle_present",
        "target_pid_present",
        "target_window_present",
        "target_pid_stable",
        "focused_element_stable",
        "window_binding_equal",
        "background",
        "foreground",
        "delivered",
        "executed",
        "effect_observed",
        "postcondition_verified",
        "completion_verified",
        "input_dispatched",
        "focus_attempted",
        "focus_succeeded",
        "target_window_stable",
        "semantic_control_resolved",
        "semantic_control_role_allowed",
        "semantic_control_settable",
        "focused_control_matches",
        "selection_verified",
        "value_readback_attempted",
        "value_readback_matched",
        "rebind_attempted",
        "rebind_succeeded",
        "approval_pending",
        "approval_replay",
        "result_ok",
        "driver_registered",
        "driver_available",
        "background_type_capable",
        "pyobjc_ax_import_available",
        "ax_process_trusted",
        "ax_set_value_unsafe_app",
        "ax_attempted",
        "screenshot_supported",
        "target_resolved",
        "capture_attempted",
        "capture_succeeded",
        "artifact_path_present",
        "model_path_present",
        "artifact_file_created",
        "model_file_created",
        "artifact_root_match",
        "screenshot_contract_valid",
        "semantic_counts_truncated",
        "saw_ax_text_field",
        "saw_ax_combo_box",
        "saw_ax_text_area",
        "saw_ax_search_field_subrole",
        "saw_ax_web_area_ancestor",
        "saw_unlisted_text_capable_role",
        "window_frame_match",
        "child_frame_valid",
        "child_center_inside_window",
        "relative_region_evaluable",
        "relative_region_matched",
        "probe_completed",
        "semantic_control_ready",
        "mutation_attempted",
        "semantic_window_scan_complete",
        "semantic_window_scan_truncated",
        "semantic_window_depth_truncated",
        "semantic_app_scan_performed",
        "semantic_app_scan_complete",
        "semantic_app_scan_truncated",
        "semantic_actionable_counts_truncated",
        "semantic_app_diagnostic_counts_truncated",
        "semantic_unlisted_relation_scan_complete",
        "semantic_exposure_probe_performed",
        "semantic_exposure_probe_complete",
        "semantic_exposure_probe_truncated",
        "semantic_exposure_global_node_limit_hit",
        "semantic_exposure_global_read_limit_hit",
        "semantic_exposure_count_saturated",
        "semantic_alt_contents_advertised",
        "semantic_alt_visible_children_advertised",
        "semantic_alt_navigation_order_advertised",
        "semantic_alt_shared_text_advertised",
        "semantic_alt_focused_element_present",
        "semantic_alt_focused_element_exact_owned",
        "semantic_alt_focused_element_non_web",
        "semantic_alt_focused_element_allowed_role",
        "semantic_alt_search_predicate_advertised",
        "semantic_alt_text_marker_relation_advertised",
        "semantic_alt_allowed_role_found",
        "semantic_alt_full_eligibility_found",
        "semantic_navigation_order_count_stable",
        "semantic_navigation_order_complete",
        "saw_unlisted_container_class",
        "saw_unlisted_static_value_class",
        "saw_unlisted_action_control_class",
        "saw_unlisted_web_root_class",
        "saw_unlisted_other_class",
        "semantic_children_failure_on_window_root",
        "semantic_children_failure_under_toolbar",
        "semantic_children_attribute_advertised",
        "semantic_children_count_known",
        "semantic_children_count_nonzero",
        "semantic_children_branch_proven_empty",
        "semantic_actionable_branch_scope_complete",
        "semantic_actionable_candidates_complete",
        "semantic_actionable_scan_complete",
        "semantic_stale_node_self_eligible",
        "semantic_stale_recovery_eligible",
        "semantic_stale_recovery_attempted",
        "semantic_stale_recovery_window_rebound",
        "semantic_stale_recovery_window_stable",
        "semantic_stale_recovery_second_pass_complete",
        "semantic_stale_recovery_succeeded",
        "semantic_stale_parent_refresh_attempted",
        "semantic_stale_parent_refresh_succeeded",
        "semantic_stale_recovery_final_scan_complete",
        "semantic_stale_additional_read_budget_exhausted",
        "semantic_navigation_order_count_stable", "semantic_navigation_order_complete",
        "exact_binding_input_valid", "exact_running_app_present",
        "exact_quartz_query_completed", "exact_quartz_record_present",
        "exact_quartz_owner_matches", "exact_quartz_layer_allowed", "exact_quartz_visible",
        "exact_quartz_frame_matches", "exact_ax_windows_attribute_available",
        "exact_ax_windows_payload_valid", "exact_ax_windows_read_completed",
        "exact_ax_match_present", "exact_ax_match_unique", "exact_window_resolved",
        "exact_resolution_retry_attempted", "exact_resolution_retry_recovered",
        "native_frontmost_check_completed", "native_target_non_frontmost_before",
        "native_target_non_frontmost_after", "native_frontmost_unchanged",
        "selection_matched_app",
        "selection_matched_window",
        "selection_selected",
        "selection_exact_binding_required",
        "selection_exact_binding_present",
        "selection_app_verified",
        "selection_pid_present",
        "selection_window_id_present",
        "selection_geometry_complete",
        "selection_geometry_integral",
        "selection_focus_requested",
        "selection_focus_attempted",
        "selection_window_owner_alias_matched",
        "selection_requested_alias_valid",
        "selection_requested_bundle_alias_available",
        "selection_swift_helper_available",
        "selection_swift_helper_invoked",
        "selection_swift_helper_compile_attempted",
        "selection_swift_helper_compile_succeeded",
        "selection_native_snapshot_atomic",
        "selection_nsworkspace_observation_completed",
        "selection_nsworkspace_target_process_present",
        "selection_nsworkspace_localized_name_match",
        "selection_nsworkspace_bundle_id_match",
        "selection_target_pid_match_available",
        "selection_target_bundle_match_available",
        "selection_selected_identity_contract_valid",
        "selection_selected_identity_available",
        "selection_selected_owner_alias_match",
        "selection_selected_target_process_match",
        "selection_selected_target_bundle_match",
        "selection_primary_source_nonempty",
        "selection_later_sources_suppressed_by_selection_policy",
        "selection_diagnostic_sources_compared",
        "selection_primary_source_target_match_absent",
        "selection_later_source_target_match_present",
        "selection_primary_source_suppressed_target_observation",
        "selection_inventory_instrumentation_consistent",
        "selection_reobservation_eligible",
        "selection_reobservation_attempted",
        "selection_reobservation_recovered",
        "selection_permission_request_api_invoked",
        *(
            f"selection_{source}_permission_check_colocated"
            for source in ("swift", "quartz", "system_events")
        ),
        *(
            f"selection_{source}_{suffix}"
            for source in ("swift", "quartz")
            for suffix in (
                "target_pid_set_constructed_privately", "on_screen_omission_confirmed",
                "all_windows_nonactionable",
            )
        ),
        *(
            f"selection_{source}_{suffix}"
            for source in ("swift", "quartz", "system_events")
            for suffix in (
                "inventory_observed", "inventory_contract_valid", "pid_match_available",
                "bundle_match_available", "on_screen_only_filter_applied",
                "layer_zero_filter_applied",
            )
        ),
    }
)
_SEMANTIC_COUNT_CAPS = {
    "semantic_nodes_visited_count": 255,
    "semantic_role_match_count": 64, "semantic_window_owned_count": 64,
    "semantic_non_web_content_count": 64, "semantic_frame_valid_count": 64,
    "semantic_region_match_count": 64, "semantic_enabled_count": 64,
    "semantic_value_present_count": 64, "semantic_value_readable_count": 64,
    "semantic_value_settable_count": 64, "semantic_selected_text_settable_count": 64,
    "semantic_selected_range_settable_count": 64, "semantic_focus_settable_count": 64,
    "semantic_final_candidate_count": 8,
    "semantic_preinvalidation_candidate_count": 8,
    "semantic_window_nodes_visited_count": 255,
    "semantic_window_duplicate_nodes_skipped_count": 255,
    "semantic_window_max_depth_reached": 20,
    "semantic_app_nodes_visited_count": 255,
    "semantic_forbidden_root_count": 64,
    "semantic_forbidden_subtree_pruned_count": 64,
    "semantic_other_window_pruned_count": 64,
    "semantic_children_read_failure_count": 64,
    "semantic_children_read_success_count": 64,
    "semantic_children_empty_count": 64,
    "semantic_children_unsupported_count": 64,
    "semantic_children_no_value_count": 64,
    "semantic_children_cannot_complete_count": 64,
    "semantic_children_invalid_element_count": 64,
    "semantic_children_global_failure_count": 64,
    "semantic_children_protocol_failure_count": 64,
    "semantic_children_unknown_branch_count": 64,
    "semantic_unresolved_selector_branch_count": 64,
    "semantic_children_proven_empty_after_failure_count": 64,
    "semantic_children_retry_attempted_count": 64,
    "semantic_children_retry_recovered_count": 64,
    "semantic_stale_parent_refresh_count": 1,
    "semantic_stale_parent_refresh_read_count": 2,
    "semantic_stale_additional_ax_read_count": 64,
    "semantic_discovery_pass_count": 3,
    "semantic_stale_recovery_restart_count": 2,
    "semantic_first_pass_stale_count": 64,
    "semantic_second_pass_stale_count": 64,
    "semantic_first_pass_unknown_branch_count": 64,
    "semantic_second_pass_unknown_branch_count": 64,
    "semantic_first_pass_nodes_visited_count": 255,
    "semantic_second_pass_nodes_visited_count": 255,
    "semantic_second_pass_final_candidate_count": 8,
    "semantic_third_pass_stale_count": 64,
    "semantic_third_pass_unknown_branch_count": 64,
    "semantic_third_pass_nodes_visited_count": 255,
    "semantic_third_pass_final_candidate_count": 8,
    "semantic_window_allowed_role_count": 64,
    "semantic_app_owned_allowed_role_count": 64,
    "semantic_allowed_ax_text_field_count": 8,
    "semantic_allowed_ax_combo_box_count": 8,
    "semantic_allowed_ax_text_area_count": 8,
    "semantic_allowed_frame_inside_window_count": 8,
    "semantic_allowed_region_x_match_count": 8,
    "semantic_allowed_region_y_match_count": 8,
    "semantic_unlisted_text_capable_count": 64,
    "semantic_unlisted_window_owned_count": 64,
    "semantic_unlisted_non_web_count": 64,
    "semantic_unlisted_frame_valid_count": 64,
    "semantic_unlisted_region_match_count": 64,
    "semantic_unlisted_enabled_count": 64,
    "semantic_unlisted_value_readable_count": 64,
    "semantic_unlisted_mutation_ready_count": 64,
    "semantic_unlisted_value_settable_count": 64,
    "semantic_unlisted_selected_text_settable_count": 64,
    "semantic_unlisted_selected_range_settable_count": 64,
    "semantic_unlisted_focus_settable_count": 64,
    "semantic_unlisted_attribute_capability_known_count": 64,
    "semantic_unlisted_under_toolbar_count": 64,
    "semantic_unlisted_related_allowed_role_count": 64,
    "exact_resolution_attempt_count": 2, "exact_quartz_record_match_count": 2,
    "exact_ax_window_count": 16, "exact_ax_frame_valid_count": 16,
    "exact_ax_frame_match_count": 8,
    "semantic_exposure_nodes_visited_count": 64,
    "semantic_exposure_edge_reads_count": 128,
    "semantic_exposure_edge_read_failure_count": 16,
    "semantic_exposure_exact_owned_count": 64,
    "semantic_exposure_non_web_count": 64,
    "semantic_exposure_allowed_role_count": 8,
    "semantic_exposure_full_eligibility_count": 8,
    "semantic_exposure_shared_text_relation_count": 8,
    "semantic_exposure_parameterized_capability_count": 8,
    "semantic_exposure_page_control_count": 8,
    "semantic_exposure_incomplete_cause_count": 8,
    "semantic_exposure_edge_fanout_truncated_count": 16,
    "semantic_exposure_depth_limit_new_target_count": 16,
    "semantic_exposure_depth_limit_queued_target_count": 16,
    "semantic_exposure_queue_remainder_count": 64,
    "semantic_exposure_payload_missing_count": 16,
    "semantic_exposure_payload_invalid_count": 16,
    "semantic_exposure_payload_mixed_count": 16,
    "semantic_exposure_attribute_inventory_unknown_count": 16,
    "semantic_exposure_parameterized_inventory_unknown_count": 5,
    "semantic_exposure_edge_incomplete_without_failure_count": 16,
    "semantic_exposure_node_ownership_rejected_count": 64,
    "semantic_exposure_edge_target_ownership_rejected_count": 64,
    "semantic_navigation_order_fallback_attempted_count": 8,
    "semantic_navigation_order_fallback_succeeded_count": 8,
    "semantic_navigation_order_recovered_invalid_count": 8,
    "semantic_navigation_order_page_read_count": 16,
}
_WINDOW_SELECTION_COUNT_CAPS = {
    "selection_nsworkspace_target_process_match_count": 4,
    "selection_inventory_cause_count": 4,
    "selection_observation_index": 2,
    "selection_observation_count": 2,
    **{
        f"selection_{source}_{suffix}": cap
        for source in ("swift", "quartz", "system_events")
        for suffix, cap in (
            ("window_total_count", 64),
            ("usable_window_count", 64),
            ("target_name_match_count", 8),
            ("target_pid_match_count", 8),
            ("target_bundle_match_count", 8),
        )
    },
    **{
        f"selection_{source}_{suffix}": cap
        for source in ("swift", "quartz")
        for suffix, cap in (
            ("owner_name_present_count", 64),
            ("window_name_present_count", 64),
            ("raw_target_pid_match_count", 8),
            ("raw_target_bundle_match_count", 8),
            ("all_windows_target_pid_match_count", 8),
            ("target_rejected_not_on_screen_count", 8),
            ("target_rejected_nonzero_layer_count", 8),
            ("target_rejected_invalid_identity_count", 8),
            ("target_rejected_nonpositive_geometry_count", 8),
            ("rejected_target_pid_mismatch_count", 64),
            ("rejected_target_bundle_mismatch_count", 8),
        )
    },
    "selection_permission_fact_change_count": 4,
    "selection_quartz_cg_all_windows_records_aggregated_count": 256,
}
_FIXED_COUNT_CAPS = {**_SEMANTIC_COUNT_CAPS, **_WINDOW_SELECTION_COUNT_CAPS}
_INT_FIELDS = frozenset({"dispatched_units", "requested_count", "executed_count"}) | frozenset(_FIXED_COUNT_CAPS)
_TOKEN_FIELDS = frozenset(
    {
        "stage",
        "action",
        "requested_delivery_mode",
        "selected_driver",
        "failure_stage",
        "error_code",
        "input_strategy",
        "ax_result_code",
        "capture_driver",
        "target_binding_source",
        "semantic_scan_scope",
        "semantic_discovery_stage",
        "semantic_coordinate_status",
        "semantic_ownership_proof",
        "semantic_app_diagnostic_stage",
        "semantic_app_diagnostic_scope",
        "semantic_app_diagnostic_ownership_proof",
        "semantic_unlisted_relation_kind",
        "semantic_exposure_stage",
        "semantic_exposure_source",
        "semantic_parameterized_capability_class",
        "semantic_exposure_incomplete_cause",
        "semantic_exposure_fanout_source",
        "semantic_exposure_depth_limit_source",
        "semantic_exposure_focus_cardinality",
        "semantic_exposure_count_saturation_class",
        "semantic_allowed_role_class",
        "semantic_allowed_region_miss_axis",
        "semantic_allowed_center_y_band",
        "semantic_allowed_width_band",
        "semantic_allowed_height_band",
        "accessibility_trust_preflight",
        "semantic_children_ax_error_class",
        "semantic_children_structural_empty_proof",
        "semantic_navigation_order_fallback_outcome",
        "semantic_navigation_order_failure_class",
        "semantic_navigation_order_ax_error_class",
        "semantic_navigation_order_cardinality_class",
        "semantic_navigation_order_parent_proof",
        "semantic_stale_branch_scope",
        "semantic_stale_recovery_outcome",
        "semantic_stale_reference_refresh_class",
        "semantic_stale_branch_comparison",
        "semantic_second_third_stale_reference_class",
        "selection_failure_stage",
        "selection_activation_policy",
        "selection_swift_helper_response_contract",
        "selection_swift_helper_binary_class",
        "selection_swift_helper_contract_version_class",
        "selection_inventory_source_used",
        "selection_selected_identity_class",
        "selection_authoritative_permission_source",
        "selection_inventory_diagnostic_stage",
        "selection_inventory_diagnostic_outcome",
        "selection_reobservation_outcome",
        *(
            f"selection_{source}_execution_component"
            for source in ("swift", "quartz", "system_events")
        ),
        "selection_swift_helper_signing_class",
        "selection_swift_helper_persistence_class",
        "selection_swift_helper_path_stability",
        "selection_swift_helper_signature_stability",
        "selection_codex_permission_comparison",
        "selection_swift_ax_trust",
        "selection_quartz_ax_trust",
        "selection_swift_ax_target_probe_outcome",
        "selection_quartz_ax_target_probe_outcome",
        "selection_system_events_automation_preflight",
        "selection_system_events_execution_outcome",
        "selection_swift_screen_capture_preflight",
        "selection_quartz_screen_capture_preflight",
        "selection_swift_cg_on_screen_query_outcome",
        "selection_swift_cg_all_windows_query_outcome",
        "selection_quartz_cg_on_screen_query_outcome",
        "selection_quartz_cg_all_windows_query_outcome",
        "selection_permission_diagnostic_outcome",
        "selection_authoritative_permission_outcome",
        "selection_secondary_permission_outcome",
        "selection_permission_fact_stability",
    }
)
_OUTPUT_FIELDS = frozenset({"timestamp_ms", "duration_ms", "run_id", "action_id"}) | _BOOL_FIELDS | _INT_FIELDS | _TOKEN_FIELDS
_SAFE_AX_RESULT_CODES = frozenset({
    "AX_DRIVER_NOT_REGISTERED", "AX_DRIVER_UNAVAILABLE", "AX_CAPABILITY_UNAVAILABLE",
    "AX_BACKGROUND_TYPE_UNSUPPORTED", "AX_DRIVER_ELIGIBLE", "AX_IMPORT_UNAVAILABLE",
    "AX_NOT_TRUSTED", "AX_SET_VALUE_UNSAFE_APP", "AX_TARGET_MISSING", "AX_ELIGIBLE",
    "AX_DIAGNOSTICS_UNAVAILABLE", "AX_TYPE_VERIFIED", "AX_TYPE_POSTED_UNVERIFIED",
    "AX_TYPE_NOT_EXECUTED", "AX_DRIVER_ERROR",
})
_SAFE_SCREENSHOT_CAPTURE_DRIVERS = frozenset({
    "none", "mac_swift_host", "mac_screencapture_window", "mac_screencapture_rect",
    "mac_screencapture_display", "windows_native", "linux_native",
})
_SAFE_SEMANTIC_ENUMS = {
    "semantic_scan_scope": frozenset({"exact_window_descendants", "application_tree_owned", "none"}),
    "semantic_discovery_stage": frozenset({
        "no_nodes", "scan_incomplete", "role_absent", "window_ownership_unverified", "web_content_excluded",
        "frame_unavailable", "region_excluded", "disabled", "value_unreadable",
        "not_settable", "ambiguous", "ready",
    }),
    "semantic_coordinate_status": frozenset({
        "window_frame_matched", "child_frames_unavailable", "child_frames_outside_window",
        "relative_region_miss", "consistent", "unavailable",
    }),
    "semantic_ownership_proof": frozenset({
        "window_descendant", "ax_window_attribute", "top_level_ui_element", "ancestor_chain", "none",
    }),
    "semantic_app_diagnostic_stage": frozenset({"not_performed", "complete", "scan_incomplete"}),
    "semantic_app_diagnostic_scope": frozenset({"application_tree_owned", "none"}),
    "semantic_app_diagnostic_ownership_proof": frozenset({
        "ax_window_attribute", "top_level_ui_element", "ancestor_chain", "multiple", "none",
    }),
    "semantic_unlisted_relation_kind": frozenset({
        "title_relation", "linked_relation", "parent_child", "none", "multiple",
    }),
    "semantic_allowed_role_class": frozenset({
        "ax_text_field", "ax_combo_box", "ax_text_area", "multiple", "none",
    }),
    "semantic_allowed_region_miss_axis": frozenset({
        "none", "x", "y", "both", "outside_window", "frame_unavailable", "multiple",
    }),
    "semantic_allowed_center_y_band": frozenset({
        "top_0_22", "upper_22_35", "middle_35_65", "lower_65_100",
        "outside_window", "frame_unavailable", "multiple", "none",
    }),
    "semantic_allowed_width_band": frozenset({
        "narrow_lt_40", "wide_40_80", "near_full_80_100",
        "outside_window", "frame_unavailable", "multiple", "none",
    }),
    "semantic_allowed_height_band": frozenset({
        "shallow_0_15", "medium_15_40", "tall_40_100",
        "outside_window", "frame_unavailable", "multiple", "none",
    }),
    "semantic_traversal_order": frozenset({"breadth_first"}),
    "semantic_unlisted_role_class": frozenset({
        "unlisted_container", "unlisted_static_value", "unlisted_action_control",
        "unlisted_web_root", "unlisted_other", "multiple", "none",
    }),
    "semantic_children_failure_class": frozenset({
        "none", "cannot_complete", "stale_element", "global_api", "protocol",
        "generic", "multiple",
    }),
    "semantic_children_incomplete_branch_class": frozenset({
        "window_root", "container", "static_value", "action_control", "other",
        "multiple", "none",
    }),
    "semantic_children_ax_error_class": frozenset({
        "none", "no_value", "attribute_unsupported", "cannot_complete",
        "invalid_element", "api_disabled", "not_implemented", "illegal_argument",
        "payload_type_invalid", "generic", "multiple",
    }),
    "semantic_children_structural_empty_proof": frozenset({
        "none", "count_zero", "attribute_not_advertised", "multiple",
    }),
    "semantic_navigation_order_fallback_outcome": frozenset({
        "not_attempted", "complete_empty", "complete_children", "unavailable",
        "incomplete", "protocol_invalid", "multiple",
    }),
    "semantic_navigation_order_failure_class": frozenset({
        "none", "not_advertised", "count_unavailable", "count_over_limit",
        "page_ax_failure", "payload_invalid", "count_changed", "duplicate",
        "self_cycle", "parent_unavailable", "parent_mismatch", "multiple",
    }),
    "semantic_navigation_order_ax_error_class": frozenset({
        "none", "no_value", "attribute_unsupported", "cannot_complete",
        "invalid_element", "api_disabled", "not_implemented", "illegal_argument",
        "generic", "multiple",
    }),
    "semantic_navigation_order_cardinality_class": frozenset({
        "zero", "one", "two_to_eight", "nine_to_64", "sixty_five_to_255",
        "over_limit", "unknown", "multiple",
    }),
    "semantic_navigation_order_parent_proof": frozenset({
        "not_checked", "empty", "all_direct", "unavailable", "mismatch", "multiple",
    }),
    "semantic_stale_branch_scope": frozenset({
        "none", "structurally_empty", "forbidden_web", "candidate_node",
        "selector_relevant_unknown", "window_root", "multiple", "unknown",
    }),
    "accessibility_trust_preflight": frozenset({"granted", "denied"}),
    "semantic_stale_node_class": frozenset({
        "none", "container", "text_control", "static_value", "action_control",
        "other", "multiple",
    }),
    "semantic_stale_recovery_outcome": frozenset({
        "not_needed", "recovered_clean", "recovery_not_eligible",
        "exact_window_rebind_failed", "exact_window_changed", "frontmost_changed",
        # Older helpers can still emit second-pass values while the host
        # transitions to the bounded final-pass outcome set.
        "second_pass_stale", "second_pass_incomplete",
        "parent_refresh_not_eligible", "parent_refresh_failed",
        "parent_refresh_budget_exhausted", "recovered_after_parent_refresh",
        "final_pass_stale", "final_pass_incomplete",
    }),
    "semantic_stale_reference_refresh_class": frozenset({
        "not_attempted", "same_stale_reference_returned",
        "stale_reference_absent_nonempty", "branch_now_empty", "unknown",
    }),
    "semantic_stale_branch_comparison": frozenset({
        "not_applicable", "same_class_and_depth", "different_class_or_depth",
        "multiple", "unknown",
    }),
    "semantic_second_third_stale_reference_class": frozenset({
        "same_parent_same_reference", "same_parent_new_reference",
        "new_parent_same_reference", "new_parent_new_reference", "not_comparable",
    }),
    "exact_resolution_stage": frozenset({
        "input_validation", "running_application", "quartz_record", "quartz_frame",
        "ax_window_enumeration", "ax_window_match", "background_validation", "ready",
    }),
    "exact_resolution_outcome": frozenset({
        "input_invalid", "application_not_running", "quartz_record_missing",
        "quartz_record_invalid", "quartz_frame_mismatch", "ax_windows_unavailable",
        "ax_match_absent", "ax_match_ambiguous", "frontmost_changed", "ready", "recovered",
    }),
    "ax_windows_outcome": frozenset({
        "success", "no_value", "unsupported", "cannot_complete",
        "invalid_application_element", "global_failure", "protocol_invalid",
    }),
    "semantic_exposure_stage": frozenset({
        "incomplete", "alternate_structural_role_found", "relationship_role_found",
        "focused_page_control", "capability_advertised_only", "only_unlisted_proxy",
        "complete_no_fixed_exposure",
    }),
    "semantic_exposure_source": frozenset({
        "contents", "visible_children", "navigation_order", "shared_text",
        "focused_element", "multiple", "none",
    }),
    "semantic_parameterized_capability_class": frozenset({
        "search_predicate", "text_marker_relation", "multiple", "none",
    }),
    "semantic_exposure_incomplete_cause": frozenset({
        "none", "edge_fanout", "depth_limit", "global_node_limit",
        "global_read_limit", "queue_remainder", "focus_cardinality",
        "payload_invalid", "attribute_inventory_unknown",
        "parameterized_inventory_unknown", "edge_incomplete_without_failure",
        "counter_saturation", "multiple",
    }),
    "semantic_exposure_fanout_source": frozenset({
        "contents", "visible_children", "navigation_order", "shared_text",
        "title_relation", "serves_as_title", "linked", "parent", "multiple", "none",
    }),
    "semantic_exposure_depth_limit_source": frozenset({
        "contents", "visible_children", "navigation_order", "shared_text",
        "title_relation", "serves_as_title", "linked", "parent", "multiple", "none",
    }),
    "semantic_exposure_focus_cardinality": frozenset({
        "none", "one", "multiple", "unknown",
    }),
    "semantic_exposure_count_saturation_class": frozenset({
        "none", "incomplete_cause_count", "edge_fanout",
        "depth_limit_new_target", "depth_limit_queued_target", "queue_remainder",
        "payload_missing", "payload_invalid", "payload_mixed",
        "attribute_inventory_unknown", "parameterized_inventory_unknown",
        "edge_incomplete_without_failure", "node_ownership_rejected",
        "edge_target_ownership_rejected", "nodes_visited", "edge_reads",
        "edge_read_failures", "exact_owned", "non_web", "allowed_role",
        "full_eligibility", "shared_text_relation", "parameterized_capability",
        "page_control", "multiple",
    }),
}
_SAFE_TYPE_TRACE_ERROR_CODES = frozenset({
    "TYPE_ACCESSIBILITY_NOT_TRUSTED", "TYPE_ACCESSIBILITY_API_UNAVAILABLE",
    "TYPE_SEMANTIC_PROTOCOL_INVALID", "TYPE_EXACT_WINDOW_REQUIRED", "TYPE_EXACT_WINDOW_NOT_FOUND",
    "TYPE_BACKGROUND_PRECONDITION_FAILED", "TYPE_SEMANTIC_SELECTOR_INVALID",
    "TYPE_SEMANTIC_CONTROL_NOT_FOUND", "TYPE_SEMANTIC_CONTROL_DISABLED",
    "TYPE_SEMANTIC_VALUE_UNREADABLE", "TYPE_SEMANTIC_CONTROL_NOT_SETTABLE",
    "TYPE_SEMANTIC_CONTROL_AMBIGUOUS", "TYPE_SEMANTIC_WINDOW_OWNERSHIP_UNVERIFIED",
    "TYPE_SEMANTIC_COORDINATE_MISMATCH", "TYPE_COMPLETION_NOT_VERIFIED",
    "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
    "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE",
    # Compatibility-only for diagnostics from an already-installed helper.
    "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE",
    "TYPE_SEMANTIC_ROLE_CLASS_UNRESOLVED",
    "TYPE_SEMANTIC_PROBE_UNAVAILABLE", "TYPE_SEMANTIC_PROBE_FAILED",
    "TYPE_SEMANTIC_PROBE_UNSAFE_RESULT",
    "TYPE_DIAGNOSTICS_INVALID", "TYPE_TARGET_DRIFTED", "TYPE_FOREGROUND_TARGET_NOT_VERIFIED",
    "TYPE_VERIFICATION_UNAVAILABLE", "TYPE_SELECTION_INVALID", "TYPE_SEMANTIC_BACKGROUND_FAILED",
    "TEXT_REQUIRED",
    "TYPE_EXACT_WINDOW_INPUT_INVALID", "TYPE_EXACT_WINDOW_APP_NOT_RUNNING",
    "TYPE_EXACT_WINDOW_QUARTZ_RECORD_NOT_FOUND", "TYPE_EXACT_WINDOW_QUARTZ_RECORD_INVALID",
    "TYPE_EXACT_WINDOW_FRAME_MISMATCH", "TYPE_EXACT_WINDOW_AX_WINDOWS_UNAVAILABLE",
    "TYPE_EXACT_WINDOW_AX_MATCH_NOT_FOUND", "TYPE_EXACT_WINDOW_AX_MATCH_AMBIGUOUS",
})
_SAFE_TYPE_TRACE_FAILURE_STAGES = frozenset({
    "accessibility_permission", "exact_window_binding", "exact_window_resolution",
    "selector_validation", "background_precondition", "semantic_control_resolution",
    "semantic_control_validation", "selection_verification", "same_element_readback",
    "semantic_discovery", "window_ownership", "coordinate_validation", "before_grapheme_dispatch",
    "post_dispatch_verification", "initial_target_verification", "initial_target_rebind",
    "foreground_target_verification",
})
_SAFE_SCREENSHOT_TARGET_BINDING_SOURCES = frozenset({
    "explicit_window", "explicit_identifiers", "enumerated_match", "persisted_selection",
    "active_window", "none",
})
_SAFE_SCREENSHOT_FAILURE_STAGES = frozenset({
    "target_resolution", "native_capture", "fallback_capture", "artifact_validation",
    "model_copy", "helper_contract", "broker_transport", "pack_transport", "harness_validation",
})
_SAFE_SCREENSHOT_ERROR_CODES = frozenset({
    "SCREENSHOT_TARGET_UNAVAILABLE", "SCREENSHOT_PLATFORM_UNSUPPORTED", "SCREENSHOT_CAPTURE_FAILED",
    "SCREENSHOT_ARTIFACT_NOT_CREATED", "SCREENSHOT_MODEL_ARTIFACT_NOT_CREATED",
    "SCREENSHOT_ARTIFACT_OUTSIDE_ROOT", "SCREENSHOT_COMPLETION_NOT_VERIFIED",
    "SCREENSHOT_CONTRACT_INVALID",
})
_SAFE_WINDOW_SELECTION_ERROR_CODES = frozenset({
    "SELECT_WINDOW_APP_NOT_FOUND",
    "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED",
    "SELECT_WINDOW_USABLE_WINDOW_NOT_FOUND",
    "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE",
    "SELECT_WINDOW_RESULT_INVALID",
})
_SAFE_WINDOW_SELECTION_FAILURE_STAGES = frozenset({
    "none", "app_match", "window_match", "exact_binding",
})
_SAFE_WINDOW_SELECTION_ENUMS = {
    "selection_activation_policy": frozenset({"not_requested", "invalid_requested"}),
    "selection_swift_helper_response_contract": frozenset({
        "not_invoked", "valid_success", "valid_error", "timeout",
        "process_failure", "invalid_json", "non_object",
    }),
    "selection_swift_helper_binary_class": frozenset({
        "isolated_reused_current", "isolated_compiled_current",
        "pack_reused_current", "pack_compiled_current", "override_expected",
        "override_mismatch", "stale_fallback", "unavailable", "unknown",
    }),
    "selection_swift_helper_contract_version_class": frozenset({
        "expected", "missing", "mismatch",
    }),
    "selection_inventory_source_used": frozenset({
        "swift_host", "quartz", "system_events", "none",
    }),
    "selection_selected_identity_class": frozenset({
        "bundle_process_match", "process_match", "owner_name_only", "no_match", "unavailable",
    }),
    "selection_authoritative_permission_source": frozenset({
        "swift_host", "quartz", "system_events", "none",
    }),
    "selection_inventory_diagnostic_stage": frozenset({
        "helper_resolution", "native_snapshot", "source_comparison",
        "identity_match", "window_filter", "reobservation", "complete",
    }),
    "selection_inventory_diagnostic_outcome": frozenset({
        "helper_unavailable", "helper_contract_invalid", "process_absent",
        "process_present_no_window", "owner_name_mismatch",
        "primary_source_divergence", "transient_recovered",
        "transient_not_recovered", "multiple", "instrumentation_inconsistent",
        "exact_window_ready", "unknown",
    }),
    "selection_reobservation_outcome": frozenset({
        "not_needed", "not_eligible", "recovered", "not_recovered",
        "instrumentation_inconsistent",
    }),
    **{
        f"selection_{source}_execution_component": frozenset({
            "viewer_app", "isolated_python_runtime", "swift_helper",
            "system_events_child", "other", "unknown",
        })
        for source in ("swift", "quartz", "system_events")
    },
    "selection_swift_helper_signing_class": frozenset({
        "signed_stable", "ad_hoc", "unsigned", "unavailable", "unknown",
    }),
    "selection_swift_helper_persistence_class": frozenset({
        "reused_current", "compiled_current", "override", "stale_fallback",
        "unavailable", "unknown",
    }),
    "selection_swift_helper_path_stability": frozenset({
        "first_observation", "same", "changed", "unavailable", "unknown",
    }),
    "selection_swift_helper_signature_stability": frozenset({
        "first_observation", "same", "changed", "unavailable", "unknown",
    }),
    "selection_codex_permission_comparison": frozenset({"not_observable"}),
    **{
        f"selection_{source}_ax_trust": frozenset({
            "trusted", "not_trusted", "unavailable",
        })
        for source in ("swift", "quartz")
    },
    **{
        f"selection_{source}_ax_target_probe_outcome": frozenset({
            "success", "skipped_not_trusted", "api_disabled", "invalid_ui_element",
            "cannot_complete", "attribute_unsupported", "no_value",
            "illegal_argument", "failure", "unavailable", "unknown",
        })
        for source in ("swift", "quartz")
    },
    "selection_system_events_automation_preflight": frozenset({
        "authorized", "denied", "would_require_consent", "target_unavailable",
        "api_unavailable", "unknown",
    }),
    "selection_system_events_execution_outcome": frozenset({
        "success", "not_authorized", "accessibility_denied", "automation_denied",
        "timeout", "launch_failure", "script_failure", "invalid_output",
        "skipped_non_authoritative", "unknown",
    }),
    **{
        f"selection_{source}_screen_capture_preflight": frozenset({
            "granted", "denied", "unavailable",
        })
        for source in ("swift", "quartz")
    },
    **{
        f"selection_{source}_{query}_query_outcome": frozenset({
            "success_nonempty", "success_nonempty_truncated", "success_empty",
            "nil_or_unavailable", "invalid_payload",
        })
        for source in ("swift", "quartz")
        for query in ("cg_on_screen", "cg_all_windows")
    },
    "selection_permission_diagnostic_outcome": frozenset({
        "accessibility_denied", "screen_capture_denied", "system_events_denied",
        "on_screen_filter_exclusion", "layer_filter_exclusion",
        "geometry_filter_exclusion", "identity_correlation_failure", "multiple",
        "permissions_ok_no_target", "permissions_ok_target_unknown", "instrumentation_inconsistent",
        "forbidden_action_required", "unknown",
    }),
    "selection_authoritative_permission_outcome": frozenset({
        "forbidden_action_required", "instrumentation_inconsistent",
        "accessibility_denied", "screen_capture_denied", "system_events_denied",
        "on_screen_filter_exclusion", "layer_filter_exclusion",
        "geometry_filter_exclusion", "identity_correlation_failure", "multiple",
        "permissions_ok", "permissions_ok_no_target", "permissions_ok_target_unknown",
        "skipped_non_authoritative",
        "not_applicable", "unavailable", "unknown",
    }),
    "selection_secondary_permission_outcome": frozenset({
        "forbidden_action_required", "instrumentation_inconsistent",
        "accessibility_denied", "screen_capture_denied", "system_events_denied",
        "on_screen_filter_exclusion", "layer_filter_exclusion",
        "geometry_filter_exclusion", "identity_correlation_failure", "multiple",
        "permissions_ok", "permissions_ok_no_target", "permissions_ok_target_unknown",
        "skipped_non_authoritative",
        "not_applicable", "unavailable", "unknown",
    }),
    "selection_permission_fact_stability": frozenset({"stable", "changed", "unknown"}),
}


def current_trace_ids() -> tuple[str, str] | None:
    return _TRACE_CONTEXT.get()


@contextlib.contextmanager
def computer_action_trace(
    action: str,
    *,
    run_id: str = "",
    action_id: str = "",
) -> Iterator[tuple[str, str]]:
    """Provide stable correlation IDs for all layers of one action."""
    existing = _TRACE_CONTEXT.get()
    if existing is not None:
        yield existing
        return
    safe_run_id = _safe_identifier(run_id) or _safe_identifier(os.environ.get("RUMI_COMPUTER_TRACE_RUN_ID")) or "local"
    safe_action_id = _safe_identifier(action_id) or f"computer-{uuid.uuid4().hex}"
    token = _TRACE_CONTEXT.set((safe_run_id, safe_action_id))
    try:
        yield safe_run_id, safe_action_id
    finally:
        _TRACE_CONTEXT.reset(token)


def emit_computer_trace(stage: str, action: str, **facts: Any) -> dict[str, Any]:
    """Append one allowlisted JSONL event and return the exact safe event."""
    ids = _TRACE_CONTEXT.get()
    run_id, action_id = ids if ids is not None else ("local", f"computer-{uuid.uuid4().hex}")
    event: dict[str, Any] = {
        "timestamp_ms": int(time.time() * 1000),
        "run_id": _safe_identifier(run_id) or "local",
        "action_id": _safe_identifier(action_id) or "invalid",
        "stage": _safe_token(stage, fallback="unknown"),
        "action": _safe_token(action, fallback="unknown"),
    }
    for key, value in facts.items():
        if key in _BOOL_FIELDS and isinstance(value, bool):
            event[key] = value
        elif key in _INT_FIELDS and isinstance(value, int) and not isinstance(value, bool):
            event[key] = max(0, min(_FIXED_COUNT_CAPS.get(key, value), value))
        elif key == "duration_ms" and isinstance(value, (int, float)) and not isinstance(value, bool):
            event[key] = max(0, round(float(value), 3))
        elif key == "capture_driver":
            if value in _SAFE_SCREENSHOT_CAPTURE_DRIVERS:
                event[key] = value
        elif key == "target_binding_source":
            if value in _SAFE_SCREENSHOT_TARGET_BINDING_SOURCES:
                event[key] = value
        elif key in _SAFE_SEMANTIC_ENUMS:
            if value in _SAFE_SEMANTIC_ENUMS[key]:
                event[key] = value
        elif key == "selection_failure_stage":
            if value in _SAFE_WINDOW_SELECTION_FAILURE_STAGES:
                event[key] = value
        elif key in _SAFE_WINDOW_SELECTION_ENUMS:
            if value in _SAFE_WINDOW_SELECTION_ENUMS[key]:
                event[key] = value
        elif key == "error_code" and action == "computer.select_window":
            if value in _SAFE_WINDOW_SELECTION_ERROR_CODES:
                event[key] = value
        elif key in _TOKEN_FIELDS:
            token_value = _safe_token(value)
            if token_value:
                event[key] = token_value
    if any(
        isinstance(facts.get(key), int)
        and not isinstance(facts.get(key), bool)
        and (facts[key] < 0 or facts[key] > cap)
        for key, cap in _SEMANTIC_COUNT_CAPS.items()
    ):
        event["semantic_counts_truncated"] = True
    event = {key: value for key, value in event.items() if key in _OUTPUT_FIELDS}
    path = _trace_path()
    if path is not None:
        _append_jsonl(path, event)
    return event


def result_trace_facts(result: Any) -> dict[str, Any]:
    """Extract only fixed, non-content facts from a controller/broker result."""
    if not isinstance(result, dict):
        return {"result_ok": False}
    sources = [result]
    seen_source_ids = {id(result)}
    index = 0
    while index < len(sources) and index < 8:
        source = sources[index]
        index += 1
        for key in ("data", "diagnostics", "result", "target", "ax_candidate"):
            nested = source.get(key)
            if isinstance(nested, dict) and id(nested) not in seen_source_ids:
                seen_source_ids.add(id(nested))
                sources.append(nested)

    def first(*keys: str) -> Any:
        for key in keys:
            for source in sources:
                if key in source:
                    return source.get(key)
        return None

    executed = first("executed")
    is_error = bool(
        result.get("is_error")
        or result.get("error")
        or result.get("ok") is False
        or result.get("success") is False
    )
    approval_pending = bool(result.get("approval_required") or result.get("requires_approval"))
    selection_source: dict[str, Any] | None = None
    if result.get("action") == "computer.select_window":
        selection_source = result
    elif isinstance(result.get("result"), dict) and result["result"].get("action") == "computer.select_window":
        selection_source = result["result"]
    ax_candidate: dict[str, Any] = {}
    for source in sources:
        candidate = source.get("ax_candidate")
        if isinstance(candidate, dict):
            ax_candidate = candidate
            break
    facts: dict[str, Any] = {
        "selected_driver": first("driver", "selected_driver"),
        "background": bool(first("background")),
        "foreground": bool(first("foreground", "requires_foreground")),
        "delivered": bool(first("delivered", "input_dispatched", "executed")),
        "executed": bool(executed),
        "effect_observed": bool(first("effect_observed", "mutation_observed")),
        "postcondition_verified": bool(first("postcondition_verified")),
        "completion_verified": bool(first("completion_verified")),
        "input_dispatched": first("input_dispatched") is True,
        "probe_completed": first("probe_completed") is True,
        "semantic_control_ready": first("semantic_control_ready") is True,
        "mutation_attempted": first("mutation_attempted") is True,
        "target_pid_stable": bool(first("target_pid_stable")),
        "focused_element_stable": bool(first("focused_element_stable")),
        "focus_attempted": bool(first("focus_attempted")),
        "focus_succeeded": bool(first("focus_succeeded")),
        "target_window_stable": bool(first("target_window_stable")),
        "semantic_control_resolved": bool(first("semantic_control_resolved")),
        "semantic_control_role_allowed": bool(first("semantic_control_role_allowed")),
        "semantic_control_settable": bool(first("semantic_control_settable")),
        "focused_control_matches": bool(first("focused_control_matches")),
        "selection_verified": bool(first("selection_verified")),
        "value_readback_attempted": bool(first("value_readback_attempted")),
        "value_readback_matched": bool(first("value_readback_matched")),
        "rebind_attempted": bool(first("rebind_attempted", "target_rebind_attempted")),
        "rebind_succeeded": bool(first("rebind_succeeded", "target_rebind_succeeded")),
        "approval_pending": approval_pending,
        "result_ok": not is_error and not approval_pending and bool(executed if executed is not None else True),
        "failure_stage": first("failure_stage"),
        "error_code": first("error_code"),
        "input_strategy": first("input_strategy"),
    }
    if selection_source is not None:
        for key in (
            "selection_matched_app", "selection_matched_window", "selection_selected",
            "selection_exact_binding_required", "selection_exact_binding_present",
            "selection_app_verified", "selection_pid_present", "selection_window_id_present",
            "selection_geometry_complete", "selection_geometry_integral",
            "selection_focus_requested", "selection_focus_attempted",
            "selection_window_owner_alias_matched", "selection_requested_alias_valid",
            "selection_requested_bundle_alias_available", "selection_swift_helper_available",
            "selection_swift_helper_invoked", "selection_swift_helper_compile_attempted",
            "selection_swift_helper_compile_succeeded", "selection_native_snapshot_atomic",
            "selection_nsworkspace_observation_completed",
            "selection_nsworkspace_target_process_present",
            "selection_nsworkspace_localized_name_match",
            "selection_nsworkspace_bundle_id_match", "selection_target_pid_match_available",
            "selection_target_bundle_match_available",
            "selection_selected_identity_contract_valid",
            "selection_selected_identity_available",
            "selection_selected_owner_alias_match",
            "selection_selected_target_process_match",
            "selection_selected_target_bundle_match",
            "selection_primary_source_nonempty",
            "selection_later_sources_suppressed_by_selection_policy",
            "selection_diagnostic_sources_compared",
            "selection_primary_source_target_match_absent",
            "selection_later_source_target_match_present",
            "selection_primary_source_suppressed_target_observation",
            "selection_inventory_instrumentation_consistent",
            "selection_reobservation_eligible", "selection_reobservation_attempted",
            "selection_reobservation_recovered",
            "selection_permission_request_api_invoked",
            *(
                f"selection_{source}_permission_check_colocated"
                for source in ("swift", "quartz", "system_events")
            ),
            *(
                f"selection_{source}_{suffix}"
                for source in ("swift", "quartz")
                for suffix in (
                    "target_pid_set_constructed_privately", "on_screen_omission_confirmed",
                    "all_windows_nonactionable",
                )
            ),
            *(
                f"selection_{source}_{suffix}"
                for source in ("swift", "quartz", "system_events")
                for suffix in (
                    "inventory_observed", "inventory_contract_valid", "pid_match_available",
                    "bundle_match_available", "on_screen_only_filter_applied",
                    "layer_zero_filter_applied",
                )
            ),
        ):
            value = selection_source.get(key)
            if isinstance(value, bool):
                facts[key] = value
        if isinstance(selection_source.get("selected"), bool):
            facts["selection_selected"] = selection_source["selected"]
        exact_required = selection_source.get("selection_exact_binding_required") is True
        selected_ok = selection_source.get("selected") is True
        exact_ok = not exact_required or selection_source.get("selection_exact_binding_present") is True
        facts["result_ok"] = bool(not is_error and not approval_pending and selected_ok and exact_ok)
        failure_stage = str(selection_source.get("selection_failure_stage") or "")
        error_code = str(selection_source.get("error_code") or result.get("error_code") or "")
        facts["selection_failure_stage"] = (
            failure_stage if failure_stage in _SAFE_WINDOW_SELECTION_FAILURE_STAGES else None
        )
        facts["error_code"] = error_code if error_code in _SAFE_WINDOW_SELECTION_ERROR_CODES else None
        for key, cap in _WINDOW_SELECTION_COUNT_CAPS.items():
            value = selection_source.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                facts[key] = max(0, min(cap, value))
        for key, allowed in _SAFE_WINDOW_SELECTION_ENUMS.items():
            value = selection_source.get(key)
            if value in allowed:
                facts[key] = value
    for key in (
        "driver_registered",
        "driver_available",
        "background_type_capable",
        "pyobjc_ax_import_available",
        "ax_process_trusted",
        "ax_set_value_unsafe_app",
        "target_app_present",
        "target_bundle_present",
        "target_pid_present",
        "target_window_present",
    ):
        if isinstance(ax_candidate.get(key), bool):
            facts[key] = ax_candidate[key]
    if isinstance(ax_candidate.get("attempted"), bool):
        facts["ax_attempted"] = ax_candidate["attempted"]
    ax_result_code = str(ax_candidate.get("result_code") or "")
    if ax_result_code in _SAFE_AX_RESULT_CODES:
        facts["ax_result_code"] = ax_result_code
    for key in (
        "screenshot_supported",
        "target_resolved",
        "capture_attempted",
        "capture_succeeded",
        "artifact_path_present",
        "model_path_present",
        "artifact_file_created",
        "model_file_created",
        "artifact_root_match",
        "screenshot_contract_valid",
    ):
        value = first(key)
        if isinstance(value, bool):
            facts[key] = value
    capture_driver = str(first("capture_driver") or "")
    if capture_driver in _SAFE_SCREENSHOT_CAPTURE_DRIVERS:
        facts["capture_driver"] = capture_driver
    target_binding_source = str(first("target_binding_source") or "")
    if target_binding_source in _SAFE_SCREENSHOT_TARGET_BINDING_SOURCES:
        facts["target_binding_source"] = target_binding_source
    if first("action") == "computer.screenshot":
        failure_stage = str(first("failure_stage") or "")
        error_code = str(first("error_code") or "")
        facts["failure_stage"] = failure_stage if failure_stage in _SAFE_SCREENSHOT_FAILURE_STAGES else None
        facts["error_code"] = error_code if error_code in _SAFE_SCREENSHOT_ERROR_CODES else None
    if first("action") in {"computer.type", "computer.probe_text_control"}:
        failure_stage = str(first("failure_stage") or "")
        error_code = str(first("error_code") or "")
        if error_code == "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE":
            # Old helpers are readable at the trace boundary, but new traces
            # use only the bounded repeated-branch taxonomy.
            error_code = "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE"
        facts["failure_stage"] = failure_stage if failure_stage in _SAFE_TYPE_TRACE_FAILURE_STAGES else None
        facts["error_code"] = error_code if error_code in _SAFE_TYPE_TRACE_ERROR_CODES else None
    for key in (
        "semantic_counts_truncated", "saw_ax_text_field", "saw_ax_combo_box", "saw_ax_text_area",
        "saw_ax_search_field_subrole", "saw_ax_web_area_ancestor", "saw_unlisted_text_capable_role",
        "window_frame_match", "child_frame_valid", "child_center_inside_window",
        "relative_region_evaluable", "relative_region_matched",
        "probe_completed", "semantic_control_ready", "mutation_attempted",
        "semantic_window_scan_complete", "semantic_window_scan_truncated",
        "semantic_window_depth_truncated", "semantic_app_scan_performed",
        "semantic_app_scan_complete", "semantic_app_scan_truncated",
        "semantic_actionable_counts_truncated", "semantic_app_diagnostic_counts_truncated",
        "semantic_unlisted_relation_scan_complete",
        "semantic_exposure_probe_performed", "semantic_exposure_probe_complete",
        "semantic_exposure_probe_truncated", "semantic_exposure_global_node_limit_hit",
        "semantic_exposure_global_read_limit_hit", "semantic_exposure_count_saturated",
        "semantic_alt_contents_advertised",
        "semantic_alt_visible_children_advertised", "semantic_alt_navigation_order_advertised",
        "semantic_alt_shared_text_advertised", "semantic_alt_focused_element_present",
        "semantic_alt_focused_element_exact_owned", "semantic_alt_focused_element_non_web",
        "semantic_alt_focused_element_allowed_role", "semantic_alt_search_predicate_advertised",
        "semantic_alt_text_marker_relation_advertised", "semantic_alt_allowed_role_found",
        "semantic_alt_full_eligibility_found",
        "saw_unlisted_container_class", "saw_unlisted_static_value_class",
        "saw_unlisted_action_control_class", "saw_unlisted_web_root_class",
        "saw_unlisted_other_class",
        "semantic_children_failure_on_window_root", "semantic_children_failure_under_toolbar",
        "semantic_children_attribute_advertised", "semantic_children_count_known",
        "semantic_children_count_nonzero", "semantic_children_branch_proven_empty",
        "semantic_actionable_branch_scope_complete", "semantic_actionable_candidates_complete",
        "semantic_actionable_scan_complete", "semantic_stale_node_self_eligible",
        "semantic_stale_recovery_eligible", "semantic_stale_recovery_attempted",
        "semantic_stale_recovery_window_rebound", "semantic_stale_recovery_window_stable",
        "semantic_stale_recovery_second_pass_complete", "semantic_stale_recovery_succeeded",
        "semantic_stale_parent_refresh_attempted", "semantic_stale_parent_refresh_succeeded",
        "semantic_stale_recovery_final_scan_complete",
        "semantic_stale_additional_read_budget_exhausted",
        "semantic_navigation_order_count_stable", "semantic_navigation_order_complete",
        "exact_binding_input_valid", "exact_running_app_present",
        "exact_quartz_query_completed", "exact_quartz_record_present",
        "exact_quartz_owner_matches", "exact_quartz_layer_allowed", "exact_quartz_visible",
        "exact_quartz_frame_matches", "exact_ax_windows_attribute_available",
        "exact_ax_windows_payload_valid", "exact_ax_windows_read_completed",
        "exact_ax_match_present", "exact_ax_match_unique", "exact_window_resolved",
        "exact_resolution_retry_attempted", "exact_resolution_retry_recovered",
        "native_frontmost_check_completed", "native_target_non_frontmost_before",
        "native_target_non_frontmost_after", "native_frontmost_unchanged",
    ):
        value = first(key)
        if isinstance(value, bool):
            facts[key] = value
    for key, cap in _SEMANTIC_COUNT_CAPS.items():
        value = first(key)
        if isinstance(value, int) and not isinstance(value, bool):
            facts[key] = max(0, min(cap, value))
            if value < 0 or value > cap:
                facts["semantic_counts_truncated"] = True
    for key, allowed in _SAFE_SEMANTIC_ENUMS.items():
        value = str(first(key) or "")
        if value in allowed:
            facts[key] = value
    for count_key in ("dispatched_units", "requested_count", "executed_count"):
        value = first(count_key)
        if isinstance(value, int) and not isinstance(value, bool):
            facts[count_key] = value
    return facts


def target_trace_facts(target: Any) -> dict[str, bool]:
    value = target if isinstance(target, dict) else {}
    return {
        "target_app_present": bool(value.get("app") or value.get("application") or value.get("target_app")),
        "target_bundle_present": bool(value.get("bundle_id")),
        "target_pid_present": value.get("pid") not in (None, ""),
        "target_window_present": bool(
            value.get("window_id") not in (None, "")
            or value.get("hwnd") not in (None, "")
            or value.get("window_title")
            or value.get("title")
        ),
    }


def requested_delivery_mode(payload: Any) -> str:
    value = payload if isinstance(payload, dict) else {}
    fallback = str(value.get("fallback") or "").strip().lower()
    if value.get("background") is True or fallback == "background" or value.get("focus") is False:
        return "background"
    if value.get("physical") is True or fallback == "foreground" or value.get("focus") is True:
        return "foreground"
    return "auto"


def _trace_path() -> Path | None:
    configured = str(os.environ.get("RUMI_COMPUTER_USE_TRACE_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser()
    user_data = str(os.environ.get("RUMI_USER_DATA") or "").strip()
    return Path(user_data).expanduser() / "logs" / "computer_use_trace.jsonl" if user_data else None


def _append_jsonl(path: Path, event: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, (json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
    except OSError:
        # Trace diagnostics must never alter Computer Use behavior.
        return


def _safe_identifier(value: Any) -> str:
    return _safe_token(value)


def _safe_token(value: Any, *, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text if _SAFE_TOKEN.fullmatch(text) else fallback
