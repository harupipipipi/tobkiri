"""Fixed, content-free Computer Use delivery facts.

The broker/helper boundary may need to report that input was posted without
claiming that its effect was verified.  This extractor deliberately ignores
all user content and target identifiers.
"""

from __future__ import annotations

from typing import Any
import re


_SAFE_DRIVERS = frozenset(
    {
        "browser_cdp",
        "browser_companion",
        "computer_seat",
        "foreground_input",
        "linux_x11_virtual",
        "mac_accessibility",
        "mac_apple_events",
        "mac_cgevent_pid",
        "mac_foreground",
        "mac_swift_host",
        "none",
        "windows_postmessage",
        "windows_uia",
    }
)
_SAFE_VERIFICATION_REQUIREMENTS = frozenset({"screenshot", "observe", "focus_state"})
_SAFE_AX_RESULT_CODES = frozenset(
    {
        "AX_DRIVER_NOT_REGISTERED",
        "AX_DRIVER_UNAVAILABLE",
        "AX_CAPABILITY_UNAVAILABLE",
        "AX_BACKGROUND_TYPE_UNSUPPORTED",
        "AX_DRIVER_ELIGIBLE",
        "AX_IMPORT_UNAVAILABLE",
        "AX_NOT_TRUSTED",
        "AX_SET_VALUE_UNSAFE_APP",
        "AX_TARGET_MISSING",
        "AX_ELIGIBLE",
        "AX_DIAGNOSTICS_UNAVAILABLE",
        "AX_TYPE_VERIFIED",
        "AX_TYPE_POSTED_UNVERIFIED",
        "AX_TYPE_NOT_EXECUTED",
        "AX_DRIVER_ERROR",
    }
)
_SAFE_AX_BOOL_FIELDS = frozenset(
    {
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
        "attempted",
    }
)
_SAFE_SCREENSHOT_BOOL_FIELDS = frozenset({
    "screenshot_supported", "target_resolved", "capture_attempted", "capture_succeeded",
    "artifact_path_present", "model_path_present", "artifact_file_created",
    "model_file_created", "artifact_root_match", "screenshot_contract_valid",
})
_SAFE_SCREENSHOT_CAPTURE_DRIVERS = frozenset({
    "none", "mac_swift_host", "mac_screencapture_window", "mac_screencapture_rect",
    "mac_screencapture_display", "windows_native", "linux_native",
})
_SAFE_SCREENSHOT_TARGET_SOURCES = frozenset({
    "explicit_window", "explicit_identifiers", "enumerated_match", "persisted_selection",
    "active_window", "none",
})
_SAFE_SCREENSHOT_FAILURE_STAGES = frozenset({
    "target_resolution", "native_capture", "fallback_capture", "artifact_validation",
    "model_copy", "helper_contract", "broker_transport", "pack_transport", "harness_validation",
})
SAFE_TYPE_PREDISPATCH_ERROR_CODES = frozenset({
    "TYPE_ACCESSIBILITY_NOT_TRUSTED", "TYPE_ACCESSIBILITY_API_UNAVAILABLE",
    "TYPE_SEMANTIC_PROTOCOL_INVALID", "TYPE_EXACT_WINDOW_REQUIRED",
    "TYPE_EXACT_WINDOW_NOT_FOUND", "TYPE_BACKGROUND_PRECONDITION_FAILED",
    "TYPE_SEMANTIC_SELECTOR_INVALID", "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
    "TYPE_SEMANTIC_CONTROL_DISABLED", "TYPE_SEMANTIC_VALUE_UNREADABLE",
    "TYPE_SEMANTIC_CONTROL_NOT_SETTABLE", "TYPE_SEMANTIC_CONTROL_AMBIGUOUS",
    "TYPE_SEMANTIC_WINDOW_OWNERSHIP_UNVERIFIED", "TYPE_SEMANTIC_COORDINATE_MISMATCH",
    "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
    # New native output is deliberately narrow: only a repeatedly stale
    # branch that met the bounded same-branch gate gets this code. Keep the
    # old subtree code readable solely for already-installed helpers.
    "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE",
    "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE",
    "TYPE_SEMANTIC_ROLE_CLASS_UNRESOLVED",
    "TYPE_SEMANTIC_PROBE_UNAVAILABLE", "TYPE_SEMANTIC_PROBE_FAILED",
    "TYPE_SEMANTIC_PROBE_UNSAFE_RESULT",
    "TYPE_EXACT_WINDOW_INPUT_INVALID", "TYPE_EXACT_WINDOW_APP_NOT_RUNNING",
    "TYPE_EXACT_WINDOW_QUARTZ_RECORD_NOT_FOUND", "TYPE_EXACT_WINDOW_QUARTZ_RECORD_INVALID",
    "TYPE_EXACT_WINDOW_FRAME_MISMATCH", "TYPE_EXACT_WINDOW_AX_WINDOWS_UNAVAILABLE",
    "TYPE_EXACT_WINDOW_AX_MATCH_NOT_FOUND", "TYPE_EXACT_WINDOW_AX_MATCH_AMBIGUOUS",
})
_SAFE_TYPE_ERROR_CODES = SAFE_TYPE_PREDISPATCH_ERROR_CODES | frozenset({
    "TYPE_COMPLETION_NOT_VERIFIED", "TYPE_DIAGNOSTICS_INVALID", "TYPE_TARGET_DRIFTED",
    "TYPE_FOREGROUND_TARGET_NOT_VERIFIED", "TYPE_VERIFICATION_UNAVAILABLE",
    "TYPE_SELECTION_INVALID", "TYPE_SEMANTIC_BACKGROUND_FAILED", "TEXT_REQUIRED",
})
_LEGACY_REPEATEDLY_STALE_CODE = "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE"
_REPEATEDLY_STALE_BRANCH_CODE = "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE"
_SAFE_TYPE_BOOL_FIELDS = frozenset({
    "completion_verified", "input_dispatched", "target_pid_stable", "focused_element_stable",
    "direct_ax_attempted", "mutation_observed", "direct_no_mutation_fallback",
    "target_window_stable", "semantic_control_resolved", "semantic_control_role_allowed",
    "semantic_control_settable", "focus_attempted", "focus_succeeded",
    "focused_control_matches", "selection_verified", "value_readback_attempted",
    "value_readback_matched", "semantic_counts_truncated", "saw_ax_text_field",
    "saw_ax_combo_box", "saw_ax_text_area", "saw_ax_search_field_subrole",
    "saw_ax_web_area_ancestor", "saw_unlisted_text_capable_role", "window_frame_match",
    "child_frame_valid", "child_center_inside_window", "relative_region_evaluable",
    "relative_region_matched",
    "probe_completed", "semantic_control_ready", "mutation_attempted",
    "semantic_window_scan_complete", "semantic_window_scan_truncated",
    "semantic_window_depth_truncated", "semantic_app_scan_performed",
    "semantic_app_scan_complete", "semantic_app_scan_truncated",
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
    "exact_binding_input_valid", "exact_running_app_present",
    "exact_quartz_query_completed", "exact_quartz_record_present",
    "exact_quartz_owner_matches", "exact_quartz_layer_allowed", "exact_quartz_visible",
    "exact_quartz_frame_matches", "exact_ax_windows_attribute_available",
    "exact_ax_windows_payload_valid", "exact_ax_windows_read_completed",
    "exact_ax_match_present", "exact_ax_match_unique", "exact_window_resolved",
    "exact_resolution_retry_attempted", "exact_resolution_retry_recovered",
    "native_frontmost_check_completed", "native_target_non_frontmost_before",
    "native_target_non_frontmost_after", "native_frontmost_unchanged",
    "semantic_actionable_counts_truncated", "semantic_app_diagnostic_counts_truncated",
    "semantic_unlisted_relation_scan_complete",
    "semantic_exposure_probe_performed", "semantic_exposure_probe_complete",
    "semantic_exposure_probe_truncated", "semantic_alt_contents_advertised",
    "semantic_exposure_global_node_limit_hit", "semantic_exposure_global_read_limit_hit",
    "semantic_exposure_count_saturated",
    "semantic_alt_visible_children_advertised", "semantic_alt_navigation_order_advertised",
    "semantic_alt_shared_text_advertised", "semantic_alt_focused_element_present",
    "semantic_alt_focused_element_exact_owned", "semantic_alt_focused_element_non_web",
    "semantic_alt_focused_element_allowed_role", "semantic_alt_search_predicate_advertised",
    "semantic_alt_text_marker_relation_advertised", "semantic_alt_allowed_role_found",
    "semantic_alt_full_eligibility_found",
    "semantic_navigation_order_count_stable", "semantic_navigation_order_complete",
})
_SAFE_SEMANTIC_COUNT_CAPS = {
    "semantic_nodes_visited_count": 255,
    "semantic_role_match_count": 64,
    "semantic_window_owned_count": 64,
    "semantic_non_web_content_count": 64,
    "semantic_frame_valid_count": 64,
    "semantic_region_match_count": 64,
    "semantic_enabled_count": 64,
    "semantic_value_present_count": 64,
    "semantic_value_readable_count": 64,
    "semantic_value_settable_count": 64,
    "semantic_selected_text_settable_count": 64,
    "semantic_selected_range_settable_count": 64,
    "semantic_focus_settable_count": 64,
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
    "exact_resolution_attempt_count": 2,
    "exact_quartz_record_match_count": 2,
    "exact_ax_window_count": 16,
    "exact_ax_frame_valid_count": 16,
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
_SAFE_TYPE_ENUMS = {
    "input_strategy": frozenset({"none", "semantic_ax_selected_text", "semantic_ax_value"}),
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
    "semantic_traversal_order": frozenset({"breadth_first"}),
    "semantic_unlisted_role_class": frozenset({
        "unlisted_container", "unlisted_static_value", "unlisted_action_control",
        "unlisted_web_root", "unlisted_other", "multiple", "none",
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
        # Keep legacy second-pass values readable for old helpers while the
        # native host rolls out the bounded final-pass contract.
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

SAFE_WINDOW_SELECTION_ERROR_CODES = frozenset({
    "SELECT_WINDOW_APP_NOT_FOUND",
    "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED",
    "SELECT_WINDOW_USABLE_WINDOW_NOT_FOUND",
    "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE",
    "SELECT_WINDOW_RESULT_INVALID",
})
_SAFE_WINDOW_SELECTION_BOOL_FIELDS = frozenset({
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
})
_SAFE_WINDOW_SELECTION_COUNT_CAPS = {
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
_SAFE_WINDOW_SELECTION_FAILURE_STAGES = frozenset({
    "none", "app_match", "window_match", "exact_binding",
})


def safe_computer_delivery_facts(value: Any) -> dict[str, Any]:
    """Return only scalar delivery facts; never copy arbitrary result fields."""
    if not isinstance(value, dict):
        return {"executed": False, "delivered": False, "completion_verified": False, "outcome": "failed"}

    sources = _safe_sources(value)

    def first(key: str) -> Any:
        for source in sources:
            if key in source:
                return source.get(key)
        return None

    executed_value = first("executed")
    input_dispatched_value = first("input_dispatched")
    delivered_value = first("delivered")
    completion_value = first("completion_verified")
    effect_value = first("effect_observed")
    if effect_value is None:
        effect_value = first("mutation_observed")

    executed = executed_value is True
    input_dispatched = input_dispatched_value is True
    delivered = delivered_value is True or input_dispatched or executed
    completion_verified = completion_value is True
    effect_observed = effect_value is True

    if completion_verified:
        raw_outcome = "verified"
    elif delivered:
        raw_outcome = "posted_unverified"
    elif value.get("is_error") or value.get("error"):
        raw_outcome = "failed"
    else:
        raw_outcome = "not_delivered"

    facts: dict[str, Any] = {
        "executed": executed,
        "delivered": delivered,
        "input_dispatched": input_dispatched,
        "completion_verified": completion_verified,
        "effect_observed": effect_observed,
        "background": first("background") is True,
        "foreground": first("foreground") is True,
        "uses_physical_input": first("uses_physical_input") is True,
        "requires_foreground": first("requires_foreground") is True,
        "can_parallel_user_work": first("can_parallel_user_work") is True,
        "postcondition_verified": first("postcondition_verified") is True,
        "outcome": raw_outcome,
    }
    if raw_outcome == "posted_unverified":
        requirement = str(first("verification_required") or "").strip().lower()
        facts["verification_required"] = (
            requirement if requirement in _SAFE_VERIFICATION_REQUIREMENTS else "screenshot"
        )
    else:
        requirement = str(first("verification_required") or "").strip().lower()
        if requirement in _SAFE_VERIFICATION_REQUIREMENTS:
            facts["verification_required"] = requirement

    driver = str(first("driver") or "").strip()
    if driver in _SAFE_DRIVERS:
        facts["driver"] = driver
    dispatched_units = first("dispatched_units")
    if isinstance(dispatched_units, int) and not isinstance(dispatched_units, bool):
        facts["dispatched_units"] = max(0, dispatched_units)
    return facts


def safe_window_selection_facts(value: Any, *, requested_app: str = "") -> dict[str, Any]:
    """Extract the action-owned exact-window contract without copying target values."""
    if not isinstance(value, dict) or value.get("action") != "computer.select_window":
        return {}

    safe: dict[str, Any] = {
        key: value[key]
        for key in _SAFE_WINDOW_SELECTION_BOOL_FIELDS
        if isinstance(value.get(key), bool)
    }
    for key, cap in _SAFE_WINDOW_SELECTION_COUNT_CAPS.items():
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool):
            safe[key] = max(0, min(cap, item))
    for key, allowed in _SAFE_WINDOW_SELECTION_ENUMS.items():
        item = str(value.get(key) or "")
        if item in allowed:
            safe[key] = item
    target = value.get("target_window") if isinstance(value.get("target_window"), dict) else None
    if target is not None and value.get("selected") is True:
        geometry = [target.get(key) for key in ("x", "y", "width", "height")]
        geometry_complete = all(item is not None for item in geometry)
        geometry_integral = geometry_complete and all(
            _selection_integral_number(item, positive=key in {"width", "height"}) is not None
            for key, item in zip(("x", "y", "width", "height"), geometry)
        )
        app_verified = _selection_app_matches(requested_app, target.get("app"))
        pid_present = _selection_integral_number(target.get("pid"), positive=True) is not None
        window_id_present = _selection_integral_number(target.get("window_id"), positive=True) is not None
        safe.update({
            "selection_app_verified": app_verified,
            "selection_pid_present": pid_present,
            "selection_window_id_present": window_id_present,
            "selection_geometry_complete": geometry_complete,
            "selection_geometry_integral": geometry_integral,
            "selection_exact_binding_present": bool(
                app_verified
                and pid_present
                and window_id_present
                and geometry_complete
                and geometry_integral
            ),
        })
    if isinstance(value.get("selected"), bool):
        safe["selection_selected"] = value["selected"]
    error_code = str(value.get("error_code") or "")
    if error_code in SAFE_WINDOW_SELECTION_ERROR_CODES:
        safe["error_code"] = error_code
    failure_stage = str(value.get("selection_failure_stage") or "")
    if failure_stage in _SAFE_WINDOW_SELECTION_FAILURE_STAGES:
        safe["selection_failure_stage"] = failure_stage
    return safe


def _selection_integral_number(value: Any, *, positive: bool) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not number.is_integer() or (positive and number <= 0):
        return None
    return int(number)


def _selection_app_matches(requested: Any, actual: Any) -> bool:
    requested_tokens = _selection_app_tokens(requested)
    actual_tokens = _selection_app_tokens(actual)
    return bool(requested_tokens and actual_tokens and requested_tokens & actual_tokens)


def _selection_app_tokens(value: Any) -> set[str]:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return set()
    collapsed = re.sub(r"[^a-z0-9]+", "", normalized)
    tokens = {normalized, collapsed}
    alias_groups = (
        {"chatgpt atlas", "chatgptatlas", "atlas", "openai atlas", "openaiatlas"},
        {"google chrome", "googlechrome", "chrome", "chrome.exe"},
        {"microsoft edge", "microsoftedge", "ms edge", "edge", "msedge", "msedge.exe"},
        {"mozilla firefox", "mozillafirefox", "firefox", "firefox.exe"},
    )
    for group in alias_groups:
        if tokens & group:
            tokens.update(group)
    return {item for item in tokens if item}


def safe_ax_candidate_facts(value: Any) -> dict[str, Any]:
    """Extract only fixed AX readiness booleans and an enumerated code."""
    if not isinstance(value, dict):
        return {}
    candidate: dict[str, Any] | None = None
    for source in _safe_sources(value):
        nested = source.get("ax_candidate")
        if isinstance(nested, dict):
            candidate = nested
            break
    if candidate is None:
        return {}
    safe = {
        key: candidate[key]
        for key in _SAFE_AX_BOOL_FIELDS
        if isinstance(candidate.get(key), bool)
    }
    result_code = str(candidate.get("result_code") or "")
    if result_code in _SAFE_AX_RESULT_CODES:
        safe["result_code"] = result_code
    return safe


def safe_screenshot_facts(value: Any) -> dict[str, Any]:
    """Extract only the fixed screenshot completion contract, never artifact paths."""
    if not isinstance(value, dict):
        return {}
    sources = _safe_sources(value)

    def first(key: str) -> Any:
        for source in sources:
            if key in source:
                return source.get(key)
        return None

    safe = {
        key: first(key)
        for key in _SAFE_SCREENSHOT_BOOL_FIELDS
        if isinstance(first(key), bool)
    }
    capture_driver = str(first("capture_driver") or "")
    if capture_driver in _SAFE_SCREENSHOT_CAPTURE_DRIVERS:
        safe["capture_driver"] = capture_driver
    target_source = str(first("target_binding_source") or "")
    if target_source in _SAFE_SCREENSHOT_TARGET_SOURCES:
        safe["target_binding_source"] = target_source
    failure_stage = str(first("failure_stage") or "")
    if failure_stage in _SAFE_SCREENSHOT_FAILURE_STAGES:
        safe["failure_stage"] = failure_stage
    return safe


def safe_type_diagnostic_facts(value: Any) -> dict[str, Any]:
    """Return bounded, closed-set type diagnostics without target or text content."""
    if not isinstance(value, dict):
        return {}
    sources = _safe_sources(value)

    def first(key: str) -> Any:
        for source in sources:
            if key in source:
                return source.get(key)
        return None

    safe: dict[str, Any] = {}
    for key in _SAFE_TYPE_BOOL_FIELDS:
        item = first(key)
        if isinstance(item, bool):
            safe[key] = item
    truncated = safe.get("semantic_counts_truncated") is True
    for key, cap in _SAFE_SEMANTIC_COUNT_CAPS.items():
        item = first(key)
        if isinstance(item, int) and not isinstance(item, bool):
            bounded = max(0, min(cap, item))
            safe[key] = bounded
            truncated = truncated or item < 0 or item > cap
    if any(key in safe for key in _SAFE_SEMANTIC_COUNT_CAPS):
        safe["semantic_counts_truncated"] = truncated
    dispatched_units = first("dispatched_units")
    if isinstance(dispatched_units, int) and not isinstance(dispatched_units, bool):
        safe["dispatched_units"] = max(0, min(1_000_000, dispatched_units))
    error_code = str(first("error_code") or "")
    if error_code == _LEGACY_REPEATEDLY_STALE_CODE:
        # Legacy is accepted at this inbound boundary, but every new envelope
        # exposes the narrower branch-scoped taxonomy.
        error_code = _REPEATEDLY_STALE_BRANCH_CODE
    if error_code in _SAFE_TYPE_ERROR_CODES:
        safe["error_code"] = error_code
    for key, allowed in _SAFE_TYPE_ENUMS.items():
        item = str(first(key) or "")
        if item in allowed:
            safe[key] = item
    failure_stage = str(first("failure_stage") or "")
    if failure_stage in {
        "accessibility_permission", "exact_window_binding", "exact_window_resolution",
        "selector_validation", "background_precondition", "semantic_control_resolution",
        "semantic_control_validation", "selection_verification", "same_element_readback",
        "semantic_discovery", "window_ownership", "coordinate_validation",
        "before_grapheme_dispatch", "post_dispatch_verification",
        "initial_target_verification", "initial_target_rebind", "foreground_target_verification",
    }:
        safe["failure_stage"] = failure_stage
    return safe


def _safe_sources(value: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [value]
    seen = {id(value)}
    index = 0
    while index < len(sources) and index < 8:
        source = sources[index]
        index += 1
        for key in ("data", "diagnostics", "result", "ax_candidate"):
            nested = source.get(key)
            if isinstance(nested, dict) and id(nested) not in seen:
                seen.add(id(nested))
                sources.append(nested)
    return sources
