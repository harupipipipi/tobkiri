from __future__ import annotations

from core_runtime.host_broker.computer_delivery import safe_computer_delivery_facts


def test_delivery_facts_are_fixed_and_cannot_spoof_verified_outcome_or_leak_content():
    facts = safe_computer_delivery_facts(
        {
            "executed": True,
            "delivered": True,
            "input_dispatched": True,
            "background": True,
            "foreground": False,
            "driver": "mac_accessibility",
            "uses_physical_input": False,
            "requires_foreground": False,
            "can_parallel_user_work": True,
            "effect_observed": False,
            "postcondition_verified": False,
            "completion_verified": False,
            "outcome": "verified",
            "verification_required": "private-method",
            "dispatched_units": 7,
            "text": "private typed text",
            "url": "https://example.invalid/?secret=query",
            "window_title": "private title",
            "pid": 123,
            "window_id": 456,
            "approval_token": "private token",
            "error": "private raw error",
        }
    )

    assert facts == {
        "executed": True,
        "delivered": True,
        "input_dispatched": True,
        "completion_verified": False,
        "effect_observed": False,
        "background": True,
        "foreground": False,
        "uses_physical_input": False,
        "requires_foreground": False,
        "can_parallel_user_work": True,
        "postcondition_verified": False,
        "outcome": "posted_unverified",
        "verification_required": "screenshot",
        "driver": "mac_accessibility",
        "dispatched_units": 7,
    }
    serialized = str(facts)
    for private in (
        "private typed text",
        "secret=query",
        "private title",
        "123",
        "456",
        "private token",
        "private raw error",
        "private-method",
    ):
        assert private not in serialized


def test_delivery_facts_drop_unknown_driver_and_non_boolean_claims():
    facts = safe_computer_delivery_facts(
        {
            "executed": "true",
            "completion_verified": "true",
            "driver": "private-driver-name",
            "dispatched_units": "99",
        }
    )

    assert facts["executed"] is False
    assert facts["completion_verified"] is False
    assert facts["outcome"] == "not_delivered"
    assert "driver" not in facts
    assert "dispatched_units" not in facts


def test_delivery_type_facts_preserve_only_bounded_final_stale_recovery_diagnostics():
    from core_runtime.host_broker.computer_delivery import safe_type_diagnostic_facts

    facts = safe_type_diagnostic_facts(
        {
            "diagnostics": {
                "semantic_stale_parent_refresh_attempted": True,
                "semantic_stale_parent_refresh_succeeded": True,
                "semantic_stale_recovery_final_scan_complete": False,
                "semantic_stale_additional_read_budget_exhausted": True,
                "semantic_stale_parent_refresh_count": 99,
                "semantic_stale_parent_refresh_read_count": 99,
                "semantic_stale_additional_ax_read_count": 99,
                "semantic_discovery_pass_count": 99,
                "semantic_stale_recovery_restart_count": 99,
                "semantic_third_pass_stale_count": 99,
                "semantic_third_pass_unknown_branch_count": 99,
                "semantic_third_pass_nodes_visited_count": 999,
                "semantic_third_pass_final_candidate_count": 99,
                "semantic_stale_reference_refresh_class": "same_stale_reference_returned",
                "semantic_stale_branch_comparison": "same_class_and_depth",
                "semantic_second_third_stale_reference_class": "new_parent_same_reference",
                "semantic_stale_recovery_outcome": "final_pass_stale",
                "raw_ax_reference": "CANARY_REF",
                "private_ax_path": "/private/CANARY_PATH",
                "semantic_stale_branch_comparison_raw": "CANARY_BRANCH",
            }
        }
    )

    assert facts == {
        "semantic_stale_parent_refresh_attempted": True,
        "semantic_stale_parent_refresh_succeeded": True,
        "semantic_stale_recovery_final_scan_complete": False,
        "semantic_stale_additional_read_budget_exhausted": True,
        "semantic_stale_parent_refresh_count": 1,
        "semantic_stale_parent_refresh_read_count": 2,
        "semantic_stale_additional_ax_read_count": 64,
        "semantic_discovery_pass_count": 3,
        "semantic_stale_recovery_restart_count": 2,
        "semantic_third_pass_stale_count": 64,
        "semantic_third_pass_unknown_branch_count": 64,
        "semantic_third_pass_nodes_visited_count": 255,
        "semantic_third_pass_final_candidate_count": 8,
        "semantic_stale_reference_refresh_class": "same_stale_reference_returned",
        "semantic_stale_branch_comparison": "same_class_and_depth",
        "semantic_second_third_stale_reference_class": "new_parent_same_reference",
        "semantic_stale_recovery_outcome": "final_pass_stale",
        "semantic_counts_truncated": True,
    }
    assert "CANARY" not in str(facts)


def test_delivery_type_facts_preserve_closed_actionable_branch_gate_contract():
    from core_runtime.host_broker.computer_delivery import safe_type_diagnostic_facts

    facts = safe_type_diagnostic_facts(
        {
            "diagnostics": {
                "semantic_actionable_scan_complete": False,
                "semantic_stale_node_self_eligible": True,
                "semantic_unresolved_selector_branch_count": 999,
                "semantic_preinvalidation_candidate_count": 99,
                "semantic_stale_branch_scope": "selector_relevant_unknown",
                "semantic_stale_node_class": "text_control",
                "semantic_children_ax_error_class": "invalid_element",
                "semantic_children_structural_empty_proof": "count_zero",
                "accessibility_trust_preflight": "granted",
                "semantic_private_branch_path": "CANARY_PRIVATE_PATH",
            }
        }
    )

    assert facts == {
        "semantic_actionable_scan_complete": False,
        "semantic_stale_node_self_eligible": True,
        "semantic_unresolved_selector_branch_count": 64,
        "semantic_preinvalidation_candidate_count": 8,
        "semantic_stale_branch_scope": "selector_relevant_unknown",
        "semantic_stale_node_class": "text_control",
        "semantic_children_ax_error_class": "invalid_element",
        "semantic_children_structural_empty_proof": "count_zero",
        "accessibility_trust_preflight": "granted",
        "semantic_counts_truncated": True,
    }
    assert "CANARY" not in str(facts)


def test_delivery_type_facts_preserve_only_closed_navigation_order_fallback_contract():
    from core_runtime.host_broker.computer_delivery import safe_type_diagnostic_facts

    facts = safe_type_diagnostic_facts(
        {
            "diagnostics": {
                "semantic_navigation_order_fallback_attempted_count": 99,
                "semantic_navigation_order_fallback_succeeded_count": 99,
                "semantic_navigation_order_recovered_invalid_count": 99,
                "semantic_navigation_order_page_read_count": 99,
                "semantic_navigation_order_fallback_outcome": "complete_children",
                "semantic_navigation_order_failure_class": "none",
                "semantic_navigation_order_ax_error_class": "invalid_element",
                "semantic_navigation_order_cardinality_class": "nine_to_64",
                "semantic_navigation_order_parent_proof": "all_direct",
                "semantic_navigation_order_count_stable": True,
                "semantic_navigation_order_complete": True,
                "semantic_navigation_order_raw_children": ["CANARY_AX_REFERENCE"],
                "semantic_navigation_order_private_path": "CANARY_PRIVATE_PATH",
            }
        }
    )

    assert facts == {
        "semantic_navigation_order_count_stable": True,
        "semantic_navigation_order_complete": True,
        "semantic_navigation_order_fallback_attempted_count": 8,
        "semantic_navigation_order_fallback_succeeded_count": 8,
        "semantic_navigation_order_recovered_invalid_count": 8,
        "semantic_navigation_order_page_read_count": 16,
        "semantic_navigation_order_fallback_outcome": "complete_children",
        "semantic_navigation_order_failure_class": "none",
        "semantic_navigation_order_ax_error_class": "invalid_element",
        "semantic_navigation_order_cardinality_class": "nine_to_64",
        "semantic_navigation_order_parent_proof": "all_direct",
        "semantic_counts_truncated": True,
    }
    assert "CANARY" not in str(facts)


def test_window_selection_facts_keep_truncated_quartz_collection_aggregate_only():
    from core_runtime.host_broker.computer_delivery import safe_window_selection_facts

    facts = safe_window_selection_facts(
        {
            "action": "computer.select_window",
            "selection_quartz_cg_all_windows_query_outcome": "success_nonempty_truncated",
            "selection_quartz_cg_all_windows_records_aggregated_count": 999,
            "selection_authoritative_permission_outcome": "permissions_ok_target_unknown",
            "selection_permission_diagnostic_outcome": "permissions_ok_target_unknown",
            "quartz_records": [{"owner": "CANARY_OWNER", "window_id": 17}],
            "raw_pid": 123,
        }
    )

    assert facts == {
        "selection_quartz_cg_all_windows_query_outcome": "success_nonempty_truncated",
        "selection_quartz_cg_all_windows_records_aggregated_count": 256,
        "selection_authoritative_permission_outcome": "permissions_ok_target_unknown",
        "selection_permission_diagnostic_outcome": "permissions_ok_target_unknown",
    }
    assert "CANARY" not in str(facts)
