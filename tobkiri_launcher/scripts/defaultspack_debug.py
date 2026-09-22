#!/usr/bin/env python3
"""Launch and inspect Defaultspack with the Tobkiri Launcher host broker wired in.

This is intentionally a local debugging harness for agents and developers. It
prints redacted status only; reusable local tokens stay in process env/files.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import secrets
import signal
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Mapping, TextIO

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from debug_secret_environment import (  # noqa: E402
    copy_process_environment,
    process_environment,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUMI_AI_ROOT = REPO_ROOT / "tobkiri_runtime"
DEFAULTSPACK_ROOT = RUMI_AI_ROOT / "ecosystem" / "defaultspack"
VIEWER_ROOT = REPO_ROOT / "tobkiri_launcher"
ECOSYSTEM_JSON = DEFAULTSPACK_ROOT / "ecosystem.json"
RUN_ROOT = REPO_ROOT / ".tmp" / "rumi-viewer-defaultspack-debug"
LATEST_JSON = RUN_ROOT / "latest.json"
SMOKE_MODEL = "cerebras/gemma-4-31b"
SMOKE_TOOL = "browser_computer"
# The viewer smoke is deliberately not a general provider launcher.  Keeping
# this mapping fixed prevents a command-line value, a debug fixture, or a
# custom base URL from turning the harness into a credential-forwarding proxy.
_TRUSTED_SMOKE_PROVIDER_ID = "cerebras"
_TRUSTED_SMOKE_PROVIDER_ENV = "CEREBRAS_API_KEY"
_TRUSTED_SMOKE_CREDENTIAL_SOURCE = "inherited_env"
MIMO_CHAT_PROFILE = "mimo-chat"
CEREBRAS_COMPUTER_PROFILE = "cerebras-computer-use"
_SMOKE_PROVIDER_PROFILES: dict[str, dict[str, Any]] = {
    CEREBRAS_COMPUTER_PROFILE: {
        "provider_id": "cerebras",
        "model": "cerebras/gemma-4-31b",
        "credential_env": "CEREBRAS_API_KEY",
        "env_prefix": "CEREBRAS_",
    },
    MIMO_CHAT_PROFILE: {
        "provider_id": "opencode-zen",
        "model": "opencode-zen/mimo-v2.5-free",
        "credential_env": "OPENCODE_ZEN_API_KEY",
        "env_prefix": "OPENCODE_ZEN_",
        # Authority approval for this debug-only profile is intentionally tied
        # to the bundled provider endpoint.  These values must never be
        # learned from an Authority request or caller-supplied configuration.
        "api_id": "legacy",
        "endpoint_url": "https://opencode.ai/zen/v1/chat/completions",
        "endpoint_path": "/v1/chat/completions",
        "origin": "https://opencode.ai",
        "domain": "opencode.ai",
        "port": 443,
        "transport": "https",
    },
}
# Provider credentials and endpoint overrides are data-plane authority.  A
# smoke child starts from a detached environment snapshot, so the allowlist must remove
# every provider namespace supported by Defaultspack before restoring the one
# selected code-owned key.  Runtime necessities such as PATH remain intact.
_SMOKE_PROVIDER_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AVIAN_",
    "AZURE_OPENAI_",
    "CEREBRAS_",
    "CF_API_",
    "CLOUDFLARE_",
    "DEEPINFRA_",
    "DEEPSEEK_",
    "FIREWORKS_",
    "FRIENDLI_",
    "GEMINI_",
    "GENSPARK_",
    "GITLAWB_OPENGATEWAY_",
    "GLM_",
    "GOOGLE_",
    "GROQ_",
    "HYPERBOLIC_",
    "INFERENCENET_",
    "INFERENCE_NET_",
    "LLAMACPP_",
    "LMSTUDIO_",
    "LONGCAT_",
    "MIMO_",
    "MISTRAL_",
    "MOONSHOT_",
    "NEBIUS_",
    "NGC_",
    "NVIDIA_",
    "NOVITA_",
    "OLLAMA_",
    "OPENAI_",
    "OPENAI_COMPATIBLE_",
    "OPENCODE_GO_",
    "OPENCODE_ZEN_",
    "OPENROUTER_",
    "PERPLEXITY_",
    "RUMIOAUTH_CLOUDFLARE_",
    "RUMIOAUTH_GOOGLE_",
    "SAMBANOVA_",
    "TOGETHER_",
    "UPSTAGE_",
    "VLLM_",
    "XAI_",
    "XIAOMI_MIMO_",
)
_SMOKE_PROVIDER_ENV_KEYS = frozenset(
    {
        "RUMI_CLOUDFLARE_OAUTH_ACCESS_TOKEN",
        "RUMI_CLOUDFLARE_OAUTH_REFRESH_TOKEN",
        "RUMI_CLOUDFLARE_SANDBOX_API_KEY",
    }
)
DEFAULT_CHAT_STREAM_INACTIVITY_SECONDS = 60.0
_SYSTEM_POPEN = subprocess.Popen
DEFAULT_SMOKE_MIN_STREAM_INTERVAL_SECONDS = 35.0
DEFAULT_MAX_TRANSIENT_RESUMES = 2
DEFAULT_VIEWER_BROKER_PORT = 8770
DEFAULT_DEFAULTSPACK_HTTP_PORT = 8766
DEFAULT_KERNEL_PORT = 8765
HOST_BROKER_PERMISSION_SUBJECTS = frozenset({"Tobkiri Launcher", "Rumi Viewer"})
VIEWER_DEV_COMMAND = ("cargo", "tauri", "dev")
DEFAULT_VIEWER_MIN_FREE_MB = 4096
WRY_DETACHED_PANIC = "wkwebview/mod.rs:1349"
VIEWER_DEBUG_INSTANCE_ID_ENV = "RUMI_VIEWER_DEBUG_INSTANCE_ID"
VIEWER_DEBUG_USER_DATA_ROOT_ENV = "RUMI_VIEWER_DEBUG_USER_DATA_ROOT"
VIEWER_TRUSTED_CHAT_STORE_ENV = "RUMI_VIEWER_TRUSTED_DEFAULTSPACK_CHAT_STORE_PATH"
VIEWER_BROKER_CONNECTION_ENV = "RUMI_VIEWER_HOST_BROKER_CONNECTION"
VIEWER_BROKER_INSTANCE_NONCE_ENV = "RUMI_VIEWER_BROKER_INSTANCE_NONCE"
DEBUG_PYTHON_ENV = "TOBKIRI_DEBUG_PYTHON"
DEFAULTSPACK_DEBUG_ISOLATION_ENV = "RUMI_DEFAULTSPACK_DEBUG_ISOLATION"
DEFAULTSPACK_DEBUG_RUN_ID_ENV = "RUMI_DEFAULTSPACK_RUN_ID"
DEFAULTSPACK_DEBUG_LAUNCH_NONCE_ENV = "RUMI_DEFAULTSPACK_LAUNCH_NONCE"
DEFAULTSPACK_DEBUG_STATE_ROOT_ENV = "RUMI_DEFAULTSPACK_DEBUG_STATE_ROOT"
DEFAULTSPACK_DEBUG_HTTP_PORT_ENV = "RUMI_DEFAULTSPACK_DEBUG_HTTP_PORT"
DEFAULTSPACK_DEBUG_KERNEL_PORT_ENV = "RUMI_DEFAULTSPACK_DEBUG_KERNEL_PORT"
DEFAULTSPACK_REQUIRE_OWN_BIND_ENV = "RUMI_DEFAULTSPACK_REQUIRE_OWN_BIND"
_DEBUG_INSTANCE_ID_RE = re.compile(r"debug-[A-Za-z0-9_-]{3,58}\Z")
DEFAULT_SMOKE_PROMPT = (
    "Use computer control in ChatGPT Atlas to complete this task visibly: open Google, "
    "type the literal text youtube into Google's search box, navigate to youtube.com, "
    "choose a video, and start playback. Verify that playback is running."
)
DIRECT_ATLAS_APP = "ChatGPT Atlas"
_DIRECT_BACKGROUND_ACTIONS = {
    "computer.key",
    "computer.type",
}
_DIRECT_SAFE_BACKGROUND_DRIVERS = {
    "browser_cdp",
    "browser_companion",
    "mac_accessibility",
    "mac_cgevent_pid",
    "mac_swift_host",
}
_SMOKE_RUNTIME_APPROVAL_TOOLS = {
    "browser_computer",
    "browser_companion",
    "browser_use",
    "computer_use",
    "browser_open_url",
    "open_browser",
    "job_resume",
}
_SMOKE_PROVIDER_PERMISSIONS = {"model.invoke", "api_key.use", "network.egress"}
_SMOKE_HOST_PERMISSIONS = {
    "host.screen.capture",
    "host.accessibility.read",
    "host.accessibility.mutate",
    "host.input.pointer",
    "host.input.keyboard",
    "host.process.open_url",
    "host.process.launch_app",
}
_SECRET_VALUE_RE = re.compile(
    r"\b(?:sk|csk|gh[pousr]|AIza)[-_A-Za-z0-9]{16,}\b"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)((?:\"?(?:authorization|bearer|api[_-]?(?:key|token)|password|secret|token)\"?)\s*(?::|=|\s)\s*)[^\s,}]+"
)


class DebugApiError(RuntimeError):
    """A deliberately sanitized local debug API failure."""


class SmokeRunnerError(RuntimeError):
    pass


_DIRECT_ARTIFACT_REPORT_BOOL_FIELDS = frozenset(
    {
        "source_regular",
        "source_nonempty",
        "source_symlink",
        "source_type_allowed",
        "source_size_allowed",
        "source_fresh",
        "trusted_root_match",
        "copy_attempted",
        "copy_succeeded",
    }
)


class DirectArtifactCopyError(SmokeRunnerError):
    """Content-free artifact failure safe for direct-run JSONL/stdout."""

    failure_stage = "artifact_copy"

    def __init__(self, error_code: str, *, artifact_count: int, **facts: bool):
        self.error_code = error_code
        self.artifact_count = max(0, int(artifact_count))
        self.facts = {
            key: value
            for key, value in facts.items()
            if key in _DIRECT_ARTIFACT_REPORT_BOOL_FIELDS and isinstance(value, bool)
        }
        super().__init__(error_code)


class DirectTypeClassificationError(SmokeRunnerError):
    """Fixed type classification safe for direct supervisor reporting."""

    def __init__(self, error_code: str):
        if error_code not in _SAFE_TYPE_PREDISPATCH_CODES:
            raise SmokeRunnerError("TYPE_HARD_FAILURE")
        self.error_code = error_code
        self.classification = "PRECONDITION_FAILED"
        self.input_dispatched = False
        self.completion_verified = False
        super().__init__(error_code)


_DIRECT_SELECT_CONTROLLER_ERROR_CODES = frozenset(
    {
        "SELECT_WINDOW_APP_NOT_FOUND",
        "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED",
        "SELECT_WINDOW_USABLE_WINDOW_NOT_FOUND",
        "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE",
        "SELECT_WINDOW_RESULT_INVALID",
    }
)
_DIRECT_SELECT_HARNESS_ERROR_CODES = frozenset(
    {
        "SELECT_WINDOW_TRANSPORT_CONTRACT_INVALID",
        "SELECT_WINDOW_RESULT_SCOPE_INVALID",
        "SELECT_WINDOW_BACKGROUND_INVARIANT_FAILED",
        "SELECT_WINDOW_PERMISSION_REQUEST_FORBIDDEN",
        "SELECT_WINDOW_AUTHORITATIVE_PERMISSION_DENIED",
        "SELECT_WINDOW_AUTHORITATIVE_PERMISSION_UNAVAILABLE",
    }
)
_DIRECT_SELECT_ERROR_CODES = (
    _DIRECT_SELECT_CONTROLLER_ERROR_CODES | _DIRECT_SELECT_HARNESS_ERROR_CODES
)
_DIRECT_SELECT_BOOL_FIELDS = frozenset(
    {
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
        "selection_primary_source_nonempty",
        "selection_later_sources_suppressed_by_selection_policy",
        "selection_diagnostic_sources_compared",
        "selection_primary_source_target_match_absent",
        "selection_later_source_target_match_present",
        "selection_primary_source_suppressed_target_observation",
        "selection_inventory_instrumentation_consistent",
        "selection_swift_inventory_observed",
        "selection_swift_inventory_contract_valid",
        "selection_swift_pid_match_available",
        "selection_swift_bundle_match_available",
        "selection_swift_on_screen_only_filter_applied",
        "selection_swift_layer_zero_filter_applied",
        "selection_quartz_inventory_observed",
        "selection_quartz_inventory_contract_valid",
        "selection_quartz_pid_match_available",
        "selection_quartz_bundle_match_available",
        "selection_quartz_on_screen_only_filter_applied",
        "selection_quartz_layer_zero_filter_applied",
        "selection_system_events_inventory_observed",
        "selection_system_events_inventory_contract_valid",
        "selection_system_events_pid_match_available",
        "selection_system_events_bundle_match_available",
        "selection_system_events_on_screen_only_filter_applied",
        "selection_system_events_layer_zero_filter_applied",
        "selection_reobservation_eligible",
        "selection_reobservation_attempted",
        "selection_reobservation_recovered",
        "selection_permission_request_api_invoked",
        "selection_swift_permission_check_colocated",
        "selection_quartz_permission_check_colocated",
        "selection_system_events_permission_check_colocated",
        "selection_swift_target_pid_set_constructed_privately",
        "selection_swift_on_screen_omission_confirmed",
        "selection_swift_all_windows_nonactionable",
        "selection_swift_visibility_probe_performed",
        "selection_swift_visibility_probe_complete",
        "selection_swift_visibility_probe_truncated",
        "selection_swift_target_hidden_present",
        "selection_swift_target_unhidden_present",
        "selection_swift_target_ax_windows_read_complete",
        "selection_selected_identity_contract_valid",
        "selection_selected_identity_available",
        "selection_selected_owner_alias_match",
        "selection_selected_target_process_match",
        "selection_selected_target_bundle_match",
        "selection_quartz_target_pid_set_constructed_privately",
        "selection_quartz_on_screen_omission_confirmed",
        "selection_quartz_all_windows_nonactionable",
    }
)
_DIRECT_SELECT_COUNT_CAPS = {
    "selection_nsworkspace_target_process_match_count": 4,
    "selection_swift_window_total_count": 64,
    "selection_swift_usable_window_count": 64,
    "selection_swift_target_name_match_count": 8,
    "selection_swift_target_pid_match_count": 8,
    "selection_swift_target_bundle_match_count": 8,
    "selection_quartz_window_total_count": 64,
    "selection_quartz_usable_window_count": 64,
    "selection_quartz_target_name_match_count": 8,
    "selection_quartz_target_pid_match_count": 8,
    "selection_quartz_target_bundle_match_count": 8,
    "selection_system_events_window_total_count": 64,
    "selection_system_events_usable_window_count": 64,
    "selection_system_events_target_name_match_count": 8,
    "selection_system_events_target_pid_match_count": 8,
    "selection_system_events_target_bundle_match_count": 8,
    "selection_inventory_cause_count": 4,
    "selection_observation_index": 2,
    "selection_observation_count": 2,
    "selection_swift_owner_name_present_count": 64,
    "selection_swift_window_name_present_count": 64,
    "selection_swift_raw_target_pid_match_count": 8,
    "selection_swift_raw_target_bundle_match_count": 8,
    "selection_swift_all_windows_target_pid_match_count": 8,
    "selection_swift_target_rejected_not_on_screen_count": 8,
    "selection_swift_target_rejected_nonzero_layer_count": 8,
    "selection_swift_target_rejected_invalid_identity_count": 8,
    "selection_swift_target_rejected_nonpositive_geometry_count": 8,
    "selection_swift_rejected_target_pid_mismatch_count": 64,
    "selection_swift_rejected_target_bundle_mismatch_count": 8,
    "selection_quartz_owner_name_present_count": 64,
    "selection_quartz_window_name_present_count": 64,
    "selection_quartz_raw_target_pid_match_count": 8,
    "selection_quartz_raw_target_bundle_match_count": 8,
    "selection_quartz_all_windows_target_pid_match_count": 8,
    "selection_quartz_target_rejected_not_on_screen_count": 8,
    "selection_quartz_target_rejected_nonzero_layer_count": 8,
    "selection_quartz_target_rejected_invalid_identity_count": 8,
    "selection_quartz_target_rejected_nonpositive_geometry_count": 8,
    "selection_quartz_rejected_target_pid_mismatch_count": 64,
    "selection_quartz_rejected_target_bundle_mismatch_count": 8,
    "selection_quartz_cg_all_windows_records_aggregated_count": 256,
    "selection_permission_fact_change_count": 4,
    "selection_swift_visibility_target_process_count": 4,
    "selection_swift_visibility_candidate_process_count": 4,
    "selection_swift_target_ax_window_count": 16,
    "selection_swift_ax_minimized_count": 16,
    "selection_swift_ax_nonminimized_count": 16,
    "selection_swift_ax_frame_valid_count": 16,
    "selection_swift_ax_display_intersection_count": 16,
    "selection_swift_ax_same_pid_cg_frame_match_count": 16,
    "selection_swift_ax_cross_pid_cg_frame_match_count": 16,
    "selection_swift_target_cg_offscreen_layer_zero_geometry_count": 16,
    "selection_visibility_fact_change_count": 8,
}
_DIRECT_SELECT_FAILURE_STAGES = frozenset(
    {
        "result_scope",
        "contract_validation",
        "frontmost_validation",
        "safety_policy_validation",
        "authoritative_permission_validation",
        "authoritative_diagnostic_validation",
    }
)
_DIRECT_SELECT_CONTROLLER_STAGES = frozenset(
    {"none", "app_match", "window_match", "exact_binding"}
)
_DIRECT_SELECT_RECORD_SOURCES = frozenset(
    {"swift_host", "quartz", "system_events", "explicit", "persisted", "active", "none"}
)
_DIRECT_SELECT_ENUM_FIELDS = {
    "selection_failure_stage": _DIRECT_SELECT_CONTROLLER_STAGES,
    "selection_record_source": _DIRECT_SELECT_RECORD_SOURCES,
    "selection_activation_policy": frozenset({"not_requested", "invalid_requested"}),
    "selection_swift_helper_response_contract": frozenset(
        {
            "not_invoked", "valid_success", "valid_error", "timeout",
            "process_failure", "invalid_json", "non_object",
        }
    ),
    "selection_swift_helper_binary_class": frozenset(
        {
            "isolated_reused_current", "isolated_compiled_current",
            "pack_reused_current", "pack_compiled_current", "override_expected",
            "override_mismatch", "stale_fallback", "unavailable", "unknown",
        }
    ),
    "selection_swift_helper_contract_version_class": frozenset(
        {"expected", "missing", "mismatch"}
    ),
    "selection_inventory_source_used": frozenset(
        {"swift_host", "quartz", "system_events", "none"}
    ),
    "selection_authoritative_permission_source": frozenset(
        {"swift_host", "quartz", "system_events", "none"}
    ),
    "selection_inventory_diagnostic_stage": frozenset(
        {
            "helper_resolution", "native_snapshot", "source_comparison",
            "identity_match", "window_filter", "reobservation", "complete",
        }
    ),
    "selection_inventory_diagnostic_outcome": frozenset(
        {
            "helper_unavailable", "helper_contract_invalid", "process_absent",
            "process_present_no_window", "owner_name_mismatch",
            "primary_source_divergence", "transient_recovered",
            "transient_not_recovered", "multiple", "instrumentation_inconsistent",
            "exact_window_ready", "unknown",
        }
    ),
    "selection_reobservation_outcome": frozenset(
        {
            "not_needed", "not_eligible", "recovered", "not_recovered",
            "instrumentation_inconsistent",
        }
    ),
    "selection_swift_execution_component": frozenset(
        {"viewer_app", "isolated_python_runtime", "swift_helper", "system_events_child", "other", "unknown"}
    ),
    "selection_quartz_execution_component": frozenset(
        {"viewer_app", "isolated_python_runtime", "swift_helper", "system_events_child", "other", "unknown"}
    ),
    "selection_system_events_execution_component": frozenset(
        {"viewer_app", "isolated_python_runtime", "swift_helper", "system_events_child", "other", "unknown"}
    ),
    "selection_swift_helper_signing_class": frozenset(
        {"signed_stable", "ad_hoc", "unsigned", "unavailable", "unknown"}
    ),
    "selection_swift_helper_persistence_class": frozenset(
        {"reused_current", "compiled_current", "override", "stale_fallback", "unavailable", "unknown"}
    ),
    "selection_swift_helper_path_stability": frozenset(
        {"first_observation", "same", "changed", "unavailable", "unknown"}
    ),
    "selection_swift_helper_signature_stability": frozenset(
        {"first_observation", "same", "changed", "unavailable", "unknown"}
    ),
    "selection_codex_permission_comparison": frozenset({"not_observable"}),
    "selection_swift_ax_trust": frozenset({"trusted", "not_trusted", "unavailable"}),
    "selection_quartz_ax_trust": frozenset({"trusted", "not_trusted", "unavailable"}),
    "selection_swift_ax_target_probe_outcome": frozenset(
        {"success", "skipped_not_trusted", "api_disabled", "invalid_ui_element", "cannot_complete", "attribute_unsupported", "no_value", "illegal_argument", "failure", "unavailable", "unknown"}
    ),
    "selection_quartz_ax_target_probe_outcome": frozenset(
        {"success", "skipped_not_trusted", "api_disabled", "invalid_ui_element", "cannot_complete", "attribute_unsupported", "no_value", "illegal_argument", "failure", "unavailable", "unknown"}
    ),
    "selection_system_events_automation_preflight": frozenset(
        {"authorized", "denied", "would_require_consent", "target_unavailable", "api_unavailable", "unknown"}
    ),
    "selection_system_events_execution_outcome": frozenset(
        {"success", "not_authorized", "accessibility_denied", "automation_denied", "timeout", "launch_failure", "script_failure", "invalid_output", "skipped_non_authoritative", "unknown"}
    ),
    "selection_swift_screen_capture_preflight": frozenset({"granted", "denied", "unavailable"}),
    "selection_quartz_screen_capture_preflight": frozenset({"granted", "denied", "unavailable"}),
    "selection_swift_cg_on_screen_query_outcome": frozenset(
        {"success_nonempty", "success_nonempty_truncated", "success_empty", "nil_or_unavailable", "invalid_payload"}
    ),
    "selection_swift_cg_all_windows_query_outcome": frozenset(
        {"success_nonempty", "success_nonempty_truncated", "success_empty", "nil_or_unavailable", "invalid_payload"}
    ),
    "selection_quartz_cg_on_screen_query_outcome": frozenset(
        {"success_nonempty", "success_nonempty_truncated", "success_empty", "nil_or_unavailable", "invalid_payload"}
    ),
    "selection_quartz_cg_all_windows_query_outcome": frozenset(
        {"success_nonempty", "success_nonempty_truncated", "success_empty", "nil_or_unavailable", "invalid_payload"}
    ),
    "selection_permission_diagnostic_outcome": frozenset(
        {"accessibility_denied", "screen_capture_denied", "system_events_denied", "on_screen_filter_exclusion", "layer_filter_exclusion", "geometry_filter_exclusion", "identity_correlation_failure", "multiple", "permissions_ok_no_target", "permissions_ok_target_unknown", "instrumentation_inconsistent", "forbidden_action_required", "unknown"}
    ),
    "selection_authoritative_permission_outcome": frozenset(
        {
            "forbidden_action_required", "instrumentation_inconsistent",
            "accessibility_denied", "screen_capture_denied", "system_events_denied",
            "on_screen_filter_exclusion", "layer_filter_exclusion",
            "geometry_filter_exclusion", "identity_correlation_failure", "multiple",
            "permissions_ok", "permissions_ok_no_target", "permissions_ok_target_unknown", "skipped_non_authoritative",
            "not_applicable", "unavailable", "unknown",
        }
    ),
    "selection_secondary_permission_outcome": frozenset(
        {
            "forbidden_action_required", "instrumentation_inconsistent",
            "accessibility_denied", "screen_capture_denied", "system_events_denied",
            "on_screen_filter_exclusion", "layer_filter_exclusion",
            "geometry_filter_exclusion", "identity_correlation_failure", "multiple",
            "permissions_ok", "permissions_ok_no_target", "permissions_ok_target_unknown", "skipped_non_authoritative",
            "not_applicable", "unavailable", "unknown",
        }
    ),
    "selection_permission_fact_stability": frozenset({"stable", "changed", "unknown"}),
    "selection_visibility_fact_stability": frozenset({"stable", "changed", "unknown"}),
    "selection_swift_visibility_class": frozenset(
        {
            "on_screen_nonfrontmost", "on_screen_frontmost", "app_hidden",
            "all_ax_windows_minimized", "offscreen_same_pid_frame_correlated",
            "offscreen_cross_pid_frame_correlated", "off_display_geometry",
            "multiple_process_ambiguous", "ax_windows_unavailable", "mixed",
            "indeterminate",
        }
    ),
    "selection_swift_visibility_incomplete_cause": frozenset(
        {
            "none", "target_process_cap", "ax_window_cap", "cg_record_cap",
            "ax_read_failure", "protocol_invalid", "multiple_candidates",
        }
    ),
    "selection_selected_identity_class": frozenset(
        {"bundle_process_match", "process_match", "owner_name_only", "no_match", "unavailable"}
    ),
}
_DIRECT_SELECT_HARNESS_FINAL_FIELDS = frozenset(
    {
        "selection_observation_index",
        "selection_observation_count",
        "selection_reobservation_eligible",
        "selection_reobservation_attempted",
        "selection_reobservation_recovered",
        "selection_reobservation_outcome",
        "selection_permission_fact_stability",
        "selection_permission_fact_change_count",
        "selection_visibility_fact_stability",
        "selection_visibility_fact_change_count",
    }
)
_DIRECT_SELECT_PERMISSION_STABILITY_FIELDS = frozenset(
    {
        "selection_permission_request_api_invoked",
        "selection_authoritative_permission_source",
        "selection_authoritative_permission_outcome",
    }
)
_DIRECT_SELECT_VISIBILITY_STABILITY_FIELDS = frozenset(
    {
        "selection_swift_visibility_class",
        "selection_swift_visibility_probe_complete",
        "selection_swift_visibility_probe_truncated",
        "selection_swift_visibility_target_process_count",
        "selection_swift_visibility_candidate_process_count",
        "selection_swift_target_ax_window_count",
        "selection_swift_ax_minimized_count",
        "selection_swift_ax_nonminimized_count",
        "selection_swift_ax_frame_valid_count",
        "selection_swift_ax_display_intersection_count",
        "selection_swift_ax_same_pid_cg_frame_match_count",
        "selection_swift_ax_cross_pid_cg_frame_match_count",
        "selection_swift_target_cg_offscreen_layer_zero_geometry_count",
    }
)


class DirectSelectionContractError(SmokeRunnerError):
    """Content-free exact-binding failure safe for direct supervisor output."""

    def __init__(
        self,
        error_code: str,
        *,
        failure_stage: str,
        facts: Mapping[str, Any] | None = None,
    ):
        if error_code not in _DIRECT_SELECT_ERROR_CODES:
            error_code = "SELECT_WINDOW_RESULT_INVALID"
        if failure_stage not in _DIRECT_SELECT_FAILURE_STAGES:
            failure_stage = "contract_validation"
        self.error_code = error_code
        self.failure_stage = failure_stage
        safe_facts: dict[str, Any] = {}
        for key, value in dict(facts or {}).items():
            if key in _DIRECT_SELECT_BOOL_FIELDS and isinstance(value, bool):
                safe_facts[key] = value
            elif key in _DIRECT_SELECT_COUNT_CAPS and isinstance(value, int) and not isinstance(value, bool):
                safe_facts[key] = max(0, min(_DIRECT_SELECT_COUNT_CAPS[key], value))
            elif key in _DIRECT_SELECT_ENUM_FIELDS and value in _DIRECT_SELECT_ENUM_FIELDS[key]:
                safe_facts[key] = value
        self.facts = safe_facts
        super().__init__(error_code)


_DIRECT_PROBE_ERROR_CODES = frozenset(
    {
        "TYPE_ACCESSIBILITY_NOT_TRUSTED",
        "TYPE_ACCESSIBILITY_API_UNAVAILABLE",
        "TYPE_SEMANTIC_PROTOCOL_INVALID",
        "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
        "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE",
        # Compatibility-only for older native helpers.
        "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE",
        "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
        "TYPE_SEMANTIC_ROLE_CLASS_UNRESOLVED",
        "TYPE_SEMANTIC_CONTROL_DISABLED",
        "TYPE_SEMANTIC_VALUE_UNREADABLE",
        "TYPE_SEMANTIC_CONTROL_NOT_SETTABLE",
        "TYPE_SEMANTIC_CONTROL_AMBIGUOUS",
        "TYPE_SEMANTIC_WINDOW_OWNERSHIP_UNVERIFIED",
        "TYPE_SEMANTIC_COORDINATE_MISMATCH",
        "PROBE_RESULT_SCOPE_INVALID",
        "PROBE_TRANSPORT_CONTRACT_INVALID",
        "PROBE_BACKGROUND_INVARIANT_FAILED",
        "PROBE_FRONTMOST_SENTINEL_UNSTABLE",
        "TYPE_EXACT_WINDOW_NOT_FOUND",
        "TYPE_EXACT_WINDOW_INPUT_INVALID",
        "TYPE_EXACT_WINDOW_APP_NOT_RUNNING",
        "TYPE_EXACT_WINDOW_QUARTZ_RECORD_NOT_FOUND",
        "TYPE_EXACT_WINDOW_QUARTZ_RECORD_INVALID",
        "TYPE_EXACT_WINDOW_FRAME_MISMATCH",
        "TYPE_EXACT_WINDOW_AX_WINDOWS_UNAVAILABLE",
        "TYPE_EXACT_WINDOW_AX_MATCH_NOT_FOUND",
        "TYPE_EXACT_WINDOW_AX_MATCH_AMBIGUOUS",
        "TYPE_TARGET_DRIFTED",
    }
)
_DIRECT_PROBE_BOOL_FIELDS = frozenset(
    {
        "probe_completed",
        "semantic_control_ready",
        "semantic_control_resolved",
        "semantic_control_role_allowed",
        "semantic_control_settable",
        "semantic_counts_truncated",
        "semantic_actionable_counts_truncated",
        "semantic_window_scan_complete",
        "semantic_window_scan_truncated",
        "semantic_window_depth_truncated",
        "semantic_app_scan_performed",
        "semantic_app_scan_complete",
        "semantic_app_scan_truncated",
        "semantic_app_diagnostic_counts_truncated",
        "semantic_unlisted_relation_scan_complete",
        "semantic_navigation_order_count_stable",
        "semantic_navigation_order_complete",
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
        "semantic_exposure_probe_performed",
        "semantic_exposure_probe_complete",
        "semantic_exposure_probe_truncated",
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
        "semantic_exposure_global_node_limit_hit",
        "semantic_exposure_global_read_limit_hit",
        "semantic_exposure_count_saturated",
        "native_frontmost_check_completed",
        "native_target_non_frontmost_before",
        "native_target_non_frontmost_after",
        "native_frontmost_unchanged",
        "context_frontmost_check_completed",
        "context_target_non_frontmost_before",
        "context_target_non_frontmost_after",
        "context_frontmost_unchanged",
        "semantic_actionable_counts_truncated",
        "semantic_app_diagnostic_counts_truncated",
        "semantic_unlisted_relation_scan_complete",
    }
)
_DIRECT_PROBE_COUNT_CAPS = {
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
    "semantic_final_candidate_count": 8,
    "semantic_preinvalidation_candidate_count": 8,
    "semantic_navigation_order_fallback_attempted_count": 8,
    "semantic_navigation_order_fallback_succeeded_count": 8,
    "semantic_navigation_order_recovered_invalid_count": 8,
    "semantic_navigation_order_page_read_count": 16,
}
_DIRECT_PROBE_ENUM_FIELDS = {
    "semantic_traversal_order": frozenset({"breadth_first"}),
    "semantic_discovery_stage": frozenset(
        {
            "no_nodes",
            "scan_incomplete",
            "role_absent",
            "window_ownership_unverified",
            "web_content_excluded",
            "frame_unavailable",
            "region_excluded",
            "disabled",
            "value_unreadable",
            "not_settable",
            "ambiguous",
            "ready",
        }
    ),
    "semantic_scan_scope": frozenset(
        {"exact_window_descendants", "application_tree_owned", "none"}
    ),
    "semantic_app_diagnostic_stage": frozenset(
        {"not_performed", "complete", "scan_incomplete"}
    ),
    "semantic_app_diagnostic_scope": frozenset(
        {"application_tree_owned", "none"}
    ),
    "semantic_app_diagnostic_ownership_proof": frozenset(
        {
            "ax_window_attribute",
            "top_level_ui_element",
            "ancestor_chain",
            "multiple",
            "none",
        }
    ),
    "semantic_unlisted_relation_kind": frozenset(
        {"title_relation", "linked_relation", "parent_child", "none", "multiple"}
    ),
    "semantic_ownership_proof": frozenset(
        {
            "window_descendant",
            "ax_window_attribute",
            "top_level_ui_element",
            "ancestor_chain",
            "none",
        }
    ),
    "semantic_unlisted_role_class": frozenset(
        {
            "unlisted_container",
            "unlisted_static_value",
            "unlisted_action_control",
            "unlisted_web_root",
            "unlisted_other",
            "none",
        }
    ),
    "semantic_allowed_role_class": frozenset(
        {"ax_text_field", "ax_combo_box", "ax_text_area", "multiple", "none"}
    ),
    "semantic_allowed_region_miss_axis": frozenset(
        {"none", "x", "y", "both", "outside_window", "frame_unavailable", "multiple"}
    ),
    "semantic_allowed_center_y_band": frozenset(
        {
            "top_0_22", "upper_22_35", "middle_35_65", "lower_65_100",
            "outside_window", "frame_unavailable", "multiple", "none",
        }
    ),
    "semantic_allowed_width_band": frozenset(
        {
            "narrow_lt_40", "wide_40_80", "near_full_80_100",
            "outside_window", "frame_unavailable", "multiple", "none",
        }
    ),
    "semantic_allowed_height_band": frozenset(
        {
            "shallow_0_15", "medium_15_40", "tall_40_100",
            "outside_window", "frame_unavailable", "multiple", "none",
        }
    ),
    "semantic_children_failure_class": frozenset(
        {
            "none",
            "cannot_complete",
            "stale_element",
            "global_api",
            "protocol",
            "generic",
            "multiple",
        }
    ),
    "semantic_children_incomplete_branch_class": frozenset(
        {
            "window_root",
            "container",
            "static_value",
            "action_control",
            "other",
            "multiple",
            "none",
        }
    ),
    "semantic_children_ax_error_class": frozenset(
        {
            "none", "no_value", "attribute_unsupported", "cannot_complete",
            "invalid_element", "api_disabled", "not_implemented", "illegal_argument",
            "payload_type_invalid", "generic", "multiple",
        }
    ),
    "semantic_children_structural_empty_proof": frozenset(
        {"none", "count_zero", "attribute_not_advertised", "multiple"}
    ),
    "semantic_navigation_order_fallback_outcome": frozenset(
        {"not_attempted", "complete_empty", "complete_children", "unavailable",
         "incomplete", "protocol_invalid", "multiple"}
    ),
    "semantic_navigation_order_failure_class": frozenset(
        {"none", "not_advertised", "count_unavailable", "count_over_limit",
         "page_ax_failure", "payload_invalid", "count_changed", "duplicate",
         "self_cycle", "parent_unavailable", "parent_mismatch", "multiple"}
    ),
    "semantic_navigation_order_ax_error_class": frozenset(
        {"none", "no_value", "attribute_unsupported", "cannot_complete",
         "invalid_element", "api_disabled", "not_implemented", "illegal_argument",
         "generic", "multiple"}
    ),
    "semantic_navigation_order_cardinality_class": frozenset(
        {"zero", "one", "two_to_eight", "nine_to_64", "sixty_five_to_255",
         "over_limit", "unknown", "multiple"}
    ),
    "semantic_navigation_order_parent_proof": frozenset(
        {"not_checked", "empty", "all_direct", "unavailable", "mismatch", "multiple"}
    ),
    "semantic_stale_branch_scope": frozenset(
        {
            "none", "structurally_empty", "forbidden_web", "candidate_node",
            "selector_relevant_unknown", "window_root", "multiple", "unknown",
        }
    ),
    "accessibility_trust_preflight": frozenset({"granted", "denied"}),
    "semantic_stale_node_class": frozenset(
        {
            "none", "container", "text_control", "static_value", "action_control",
            "other", "multiple",
        }
    ),
    "semantic_stale_recovery_outcome": frozenset(
        {
            "not_needed",
            "recovered_clean",
            "recovery_not_eligible",
            "exact_window_rebind_failed",
            "exact_window_changed",
            "frontmost_changed",
            "second_pass_stale",
            "second_pass_incomplete",
            "parent_refresh_not_eligible",
            "parent_refresh_failed",
            "parent_refresh_budget_exhausted",
            "recovered_after_parent_refresh",
            "final_pass_stale",
            "final_pass_incomplete",
        }
    ),
    "semantic_stale_reference_refresh_class": frozenset(
        {
            "not_attempted",
            "same_stale_reference_returned",
            "stale_reference_absent_nonempty",
            "branch_now_empty",
            "unknown",
        }
    ),
    "semantic_stale_branch_comparison": frozenset(
        {
            "not_applicable",
            "same_class_and_depth",
            "different_class_or_depth",
            "multiple",
            "unknown",
        }
    ),
    "semantic_second_third_stale_reference_class": frozenset(
        {
            "same_parent_same_reference",
            "same_parent_new_reference",
            "new_parent_same_reference",
            "new_parent_new_reference",
            "not_comparable",
        }
    ),
    "semantic_exposure_stage": frozenset(
        {
            "incomplete",
            "alternate_structural_role_found",
            "relationship_role_found",
            "focused_page_control",
            "capability_advertised_only",
            "only_unlisted_proxy",
            "complete_no_fixed_exposure",
        }
    ),
    "semantic_exposure_source": frozenset(
        {
            "contents",
            "visible_children",
            "navigation_order",
            "shared_text",
            "focused_element",
            "multiple",
            "none",
        }
    ),
    "semantic_parameterized_capability_class": frozenset(
        {"search_predicate", "text_marker_relation", "multiple", "none"}
    ),
    "semantic_exposure_incomplete_cause": frozenset(
        {
            "none",
            "edge_fanout",
            "depth_limit",
            "global_node_limit",
            "global_read_limit",
            "queue_remainder",
            "focus_cardinality",
            "payload_invalid",
            "attribute_inventory_unknown",
            "parameterized_inventory_unknown",
            "edge_incomplete_without_failure",
            "counter_saturation",
            "multiple",
        }
    ),
    "semantic_exposure_fanout_source": frozenset(
        {
            "contents",
            "visible_children",
            "navigation_order",
            "shared_text",
            "title_relation",
            "serves_as_title",
            "linked",
            "parent",
            "multiple",
            "none",
        }
    ),
    "semantic_exposure_depth_limit_source": frozenset(
        {
            "contents",
            "visible_children",
            "navigation_order",
            "shared_text",
            "title_relation",
            "serves_as_title",
            "linked",
            "parent",
            "multiple",
            "none",
        }
    ),
    "semantic_exposure_focus_cardinality": frozenset(
        {"none", "one", "multiple", "unknown"}
    ),
    "semantic_exposure_count_saturation_class": frozenset(
        {
            "none",
            "incomplete_cause_count",
            "edge_fanout",
            "depth_limit_new_target",
            "depth_limit_queued_target",
            "queue_remainder",
            "payload_missing",
            "payload_invalid",
            "payload_mixed",
            "attribute_inventory_unknown",
            "parameterized_inventory_unknown",
            "edge_incomplete_without_failure",
            "node_ownership_rejected",
            "edge_target_ownership_rejected",
            "nodes_visited",
            "edge_reads",
            "edge_read_failures",
            "exact_owned",
            "non_web",
            "allowed_role",
            "full_eligibility",
            "shared_text_relation",
            "parameterized_capability",
            "page_control",
            "multiple",
        }
    ),
}


class DirectProbeContractError(SmokeRunnerError):
    """Content-free probe failure that always precedes write approval."""

    def __init__(
        self,
        error_code: str,
        *,
        failure_stage: str = "contract_validation",
        facts: Mapping[str, Any] | None = None,
    ):
        if error_code not in _DIRECT_PROBE_ERROR_CODES:
            error_code = "PROBE_TRANSPORT_CONTRACT_INVALID"
        if failure_stage not in {
            "result_scope",
            "contract_validation",
            "exact_window_resolution",
            "frontmost_validation",
        }:
            failure_stage = "contract_validation"
        self.error_code = error_code
        self.failure_stage = failure_stage
        self.facts = _direct_probe_facts(dict(facts or {}), require_action=False)
        self.facts.pop("error_code", None)
        super().__init__(error_code)


class LivePtyRequiredError(SmokeRunnerError):
    pass


def _direct_failure_code(error: BaseException) -> str:
    """Return a fixed enum for direct-run stdout/JSONL failure reporting."""

    if isinstance(error, DirectArtifactCopyError):
        return error.error_code
    if isinstance(error, DirectTypeClassificationError):
        return error.error_code
    if isinstance(error, DirectSelectionContractError):
        return error.error_code
    if isinstance(error, DirectProbeContractError):
        return error.error_code
    raw = str(error or "")
    for code in (
        "DIAGNOSTICS_MISSING",
        "KEY_EFFECT_NOT_VERIFIED",
        "SCREENSHOT_TARGET_UNAVAILABLE",
        "TYPE_DELIVERY_POLICY_VIOLATION",
        "TYPE_HARD_FAILURE",
        "PROVIDER_ENV_NOT_PRESENT",
    ):
        if code in raw:
            return code
    if isinstance(error, LivePtyRequiredError):
        return "LIVE_PTY_REQUIRED"
    if isinstance(error, DebugApiError):
        return "DIRECT_API_FAILED"
    return "DIRECT_COMPUTER_USE_FAILED"


def _direct_failure_report(error: BaseException) -> dict[str, Any]:
    """Return the only failure fields the direct supervisor may persist."""

    if isinstance(error, DirectTypeClassificationError):
        return {
            "error_code": error.error_code,
            "classification": error.classification,
            "input_dispatched": error.input_dispatched,
            "completion_verified": error.completion_verified,
        }
    if isinstance(error, DirectSelectionContractError):
        return {
            "error": error.error_code,
            "error_code": error.error_code,
            "failure_stage": error.failure_stage,
            **error.facts,
        }
    if isinstance(error, DirectProbeContractError):
        return _direct_probe_failure_payload(error)
    error_code = _direct_failure_code(error)
    report: dict[str, Any] = {"error": error_code, "error_code": error_code}
    if isinstance(error, DirectArtifactCopyError):
        report["failure_stage"] = error.failure_stage
        report["artifact_count"] = error.artifact_count
        report.update(error.facts)
    return report


def _smoke_provider_profile(profile_name: str) -> dict[str, Any]:
    """Return a code-owned provider profile; caller supplied profiles are forbidden."""

    try:
        return _SMOKE_PROVIDER_PROFILES[profile_name]
    except KeyError:
        raise SmokeRunnerError("SMOKE_PROVIDER_PROFILE_NOT_ALLOWED") from None


def isolated_smoke_provider_preflight(
    parent_env: Mapping[str, str] | None = None,
    *,
    profile_name: str = CEREBRAS_COMPUTER_PROFILE,
) -> dict[str, Any]:
    """Describe the fixed smoke credential without retaining or logging it.

    This check intentionally accepts neither a provider ID nor an environment
    variable name from a caller.  It is safe to persist in the supervisor log:
    no credential value, length, prefix, suffix, or hash is returned.
    """

    profile = _smoke_provider_profile(profile_name)
    source = process_environment() if parent_env is None else parent_env
    credential_present = bool(str(source.get(profile["credential_env"]) or "").strip())
    return {
        "provider_id": profile["provider_id"],
        "model": profile["model"],
        "credential_present": credential_present,
        "credential_source": _TRUSTED_SMOKE_CREDENTIAL_SOURCE,
        "credential_persisted": False,
        "allow_custom_base_url": False,
    }


def apply_isolated_smoke_provider_env(
    child_env: dict[str, str],
    *,
    parent_env: Mapping[str, str] | None,
    require_credential: bool,
    profile_name: str = CEREBRAS_COMPUTER_PROFILE,
) -> None:
    """Forward only the fixed Cerebras credential to an owned debug child.

    The isolated secrets directory remains empty.  In particular, do not
    inherit ``CEREBRAS_BASE_URL`` (or another ``CEREBRAS_*`` override): the
    bundled, trusted provider manifest supplies the endpoint and model
    catalogue.  This is debug-isolation-only; ordinary launches retain their
    existing environment behaviour.
    """

    profile = _smoke_provider_profile(profile_name)
    # Remove every allowlisted provider namespace first. The inherited snapshot
    # must not silently select another credential or a custom endpoint.
    for key in tuple(child_env):
        if key in _SMOKE_PROVIDER_ENV_KEYS or key.startswith(
            _SMOKE_PROVIDER_ENV_PREFIXES
        ):
            child_env.pop(key, None)
    child_env["RUMI_DEFAULTSPACK_ENABLE_CLOUD_PROVIDERS"] = "1"
    if parent_env is None:
        if require_credential:
            raise SmokeRunnerError("PROVIDER_ENV_NOT_PRESENT")
        return
    credential = str(parent_env.get(profile["credential_env"]) or "").strip()
    if not credential:
        if require_credential:
            raise SmokeRunnerError("PROVIDER_ENV_NOT_PRESENT")
        return
    child_env[profile["credential_env"]] = credential


def isolated_smoke_provider_secret_values(
    parent_env: Mapping[str, str] | None,
    *,
    profile_name: str = CEREBRAS_COMPUTER_PROFILE,
) -> tuple[str, ...]:
    """Return an in-memory redaction value for an already trusted child only."""

    if parent_env is None:
        return ()
    profile = _smoke_provider_profile(profile_name)
    value = str(parent_env.get(profile["credential_env"]) or "").strip()
    return (value,) if value else ()


def seed_isolated_smoke_model_selection(
    state_root: Path,
    *,
    profile_name: str = CEREBRAS_COMPUTER_PROFILE,
) -> None:
    """Write the secret-free default model fixture for an isolated smoke run."""

    profile = _smoke_provider_profile(profile_name)
    settings_path = state_root / "frontend_settings.json"
    payload = {
        "models": {
            "preferred_model": profile["model"],
            "thinking_level": "medium",
            "deepthink_enabled": False,
        }
    }
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        fd = os.open(settings_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            mode = settings_path.stat().st_mode & 0o777
            existing = settings_path.read_text(encoding="utf-8")
        except OSError as error:
            raise SmokeRunnerError("failed to validate isolated model settings") from error
        if mode != 0o600 or existing != rendered:
            raise SmokeRunnerError("isolated model settings are not the trusted smoke fixture")
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(rendered)
    except Exception:
        settings_path.unlink(missing_ok=True)
        raise


class ReservedLoopbackPort:
    """A harness-owned loopback reservation released immediately before launch.

    Holding the socket prevents another local process from claiming an auto
    selected port while the run context is assembled.  The kernel and HTTP
    servers must own their sockets themselves, so release is necessarily just
    before their respective launch; callers re-check ownership/readiness and
    never treat an arbitrary listener as this run's server.
    """

    def __init__(self, socket_: socket.socket, port: int) -> None:
        self._socket = socket_
        self.port = port

    def release(self) -> None:
        if self._socket.fileno() != -1:
            self._socket.close()


def _strict_loopback_port(value: int | str, *, name: str) -> int:
    text = str(value)
    if not text or not text.isascii() or not text.isdecimal():
        raise SmokeRunnerError(f"{name} must be an ASCII decimal localhost port")
    port = int(text)
    if not 1 <= port <= 65535:
        raise SmokeRunnerError(f"{name} must be between 1 and 65535")
    return port


def reserve_loopback_port(*, requested: int | None, excluded: set[int], name: str) -> ReservedLoopbackPort:
    """Reserve one exact/ephemeral 127.0.0.1 TCP port without touching listeners."""

    if requested is not None:
        port = _strict_loopback_port(requested, name=name)
        if port in excluded:
            raise SmokeRunnerError(f"{name} must not reuse another isolated run port")
    else:
        port = 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        selected = int(sock.getsockname()[1])
        if selected in excluded:
            sock.close()
            # An ephemeral collision is extremely unlikely, but retry rather
            # than accepting a shared run port.
            if requested is None:
                return reserve_loopback_port(requested=None, excluded=excluded, name=name)
            raise SmokeRunnerError(f"{name} must not reuse another isolated run port")
        return ReservedLoopbackPort(sock, selected)
    except OSError as error:
        sock.close()
        raise SmokeRunnerError(f"{name} is unavailable on 127.0.0.1:{port}: {error}") from error


def apply_defaultspack_debug_isolation(
    env: dict[str, str],
    *,
    run_id: str,
    nonce: str,
    state_root: Path,
    http_port: int,
    kernel_port: int,
    provider_profile: str = CEREBRAS_COMPUTER_PROFILE,
) -> None:
    """Attach the complete, non-authorizing debug run identity to an owned child.

    The port values are set after desktop metadata by the launch path.  State
    paths are deliberately fresh per run; provider credentials remain inherited
    from the caller's process and are never copied into or printed from this
    directory.
    """

    run_id = validate_debug_instance_id(run_id)
    if not instance_nonce_is_safe(nonce):
        raise SmokeRunnerError("Defaultspack debug launch nonce is invalid")
    root = state_root.resolve()
    if not root.is_absolute() or root.name != "defaultspack_state":
        raise SmokeRunnerError("Defaultspack debug state root must be an absolute per-run state directory")
    http_port = _strict_loopback_port(http_port, name="Defaultspack HTTP port")
    kernel_port = _strict_loopback_port(kernel_port, name="kernel port")
    if (
        http_port == DEFAULT_DEFAULTSPACK_HTTP_PORT
        or kernel_port == DEFAULT_KERNEL_PORT
        or http_port == kernel_port
    ):
        raise SmokeRunnerError("Defaultspack debug isolation requires distinct non-default HTTP and kernel ports")
    root.mkdir(parents=True, exist_ok=True)
    for directory in (root, root / "approval", root / "artifacts", root / "secrets", root / "scheduler"):
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError as error:
            raise SmokeRunnerError(f"failed to secure Defaultspack debug state: {error}") from error
    # This is intentionally the only provider configuration written to the
    # run root.  Credentials stay exclusively in the inherited child env.
    seed_isolated_smoke_model_selection(root, profile_name=provider_profile)
    env.update(
        {
            DEFAULTSPACK_DEBUG_ISOLATION_ENV: "1",
            DEFAULTSPACK_REQUIRE_OWN_BIND_ENV: "1",
            DEFAULTSPACK_DEBUG_RUN_ID_ENV: run_id,
            DEFAULTSPACK_DEBUG_LAUNCH_NONCE_ENV: nonce,
            DEFAULTSPACK_DEBUG_STATE_ROOT_ENV: str(root),
            DEFAULTSPACK_DEBUG_HTTP_PORT_ENV: str(http_port),
            DEFAULTSPACK_DEBUG_KERNEL_PORT_ENV: str(kernel_port),
            "DEFAULTS_HTTP_HOST": "127.0.0.1",
            # These intentionally follow desktop metadata application.  They
            # are both required because the desktop entry accepts either name.
            "DEFAULTS_HTTP_PORT": str(http_port),
            "RUMI_DEFAULTSPACK_PORT": str(http_port),
            "RUMI_PORT": str(kernel_port),
            "RUMI_DEFAULTSPACK_APPROVAL_DB_PATH": str(root / "approval" / "approvals.sqlite3"),
            "RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH": str(root / "approval" / "approval_runtime_secret"),
            "RUMI_DEFAULTSPACK_AUDIT_PATH": str(root / "audit.jsonl"),
            "RUMI_DEFAULTSPACK_BROWSER_ARTIFACTS_PATH": str(root / "artifacts" / "browser.jsonl"),
            "RUMI_DEFAULTSPACK_RUNTIME_CONFIG_PATH": str(root / "runtime_config.json"),
            "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH": str(root / "frontend_settings.json"),
            "RUMI_DEFAULTSPACK_SECRETS_DIR": str(root / "secrets"),
            "RUMI_DEFAULTSPACK_SCHEDULER_DIR": str(root / "scheduler"),
        }
    )


def prepare_defaultspack_approval_secret(state_root: Path) -> Path:
    """Create the run's approval signing secret before either process starts.

    The secret is file-backed (never injected into output or child command
    arguments) and the same path is supplied to both the Viewer broker and
    Defaultspack.  ``O_EXCL`` prevents a competing process from replacing a
    run secret while the harness is assembling its context.
    """

    root = state_root.resolve()
    secret_path = root / "approval" / "approval_runtime_secret"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        secret_path.parent.chmod(0o700)
    except OSError as error:
        raise SmokeRunnerError(f"failed to secure approval secret directory: {error}") from error
    try:
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # The state root is harness-owned.  An existing owner-only file can be
        # reused only for a resume of this exact run, never from a shared root.
        try:
            mode = secret_path.stat().st_mode & 0o777
            if mode != 0o600 or not secret_path.read_text(encoding="utf-8").strip():
                raise SmokeRunnerError("isolated approval secret is invalid")
            return secret_path
        except OSError as error:
            raise SmokeRunnerError(f"failed to validate isolated approval secret: {error}") from error
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(secrets.token_urlsafe(48))
        return secret_path
    except Exception:
        secret_path.unlink(missing_ok=True)
        raise


def instance_nonce_is_safe(value: str) -> bool:
    return 16 <= len(value) <= 128 and all(character.isascii() and (character.isalnum() or character in "_-") for character in value)


def _safe_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"http", "https"}:
            return value
        query = []
        for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            lowered = key.lower()
            if any(marker in lowered for marker in ("token", "secret", "key", "authorization", "password")):
                query.append((key, "[redacted]"))
            else:
                query.append((key, item))
        fragment = "" if any(marker in parsed.fragment.lower() for marker in ("token", "secret", "auth")) else parsed.fragment
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), fragment)
        )
    except Exception:
        return value


def _redact_string(value: str, *, secrets_to_hide: tuple[str, ...] = ()) -> str:
    redacted = value
    for secret_value in secrets_to_hide:
        if secret_value:
            redacted = redacted.replace(secret_value, "[redacted]")
    redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1[redacted]", redacted)
    return _SECRET_VALUE_RE.sub("[redacted]", redacted)


def default_connection_path() -> Path:
    env_path = process_environment().get("RUMI_VIEWER_HOST_BROKER_CONNECTION", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "dev.tobkiri.launcher"
        / "user_data"
        / "host_broker"
        / "connection.json"
    )


def defaultspack_python_executable() -> Path:
    configured = process_environment().get(DEBUG_PYTHON_ENV, "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.absolute()
        raise SmokeRunnerError(f"{DEBUG_PYTHON_ENV} is not an executable file")

    app_data_dir = default_connection_path().parent.parent.parent
    launcher_python = (
        app_data_dir / "venv" / "Scripts" / "python.exe"
        if os.name == "nt"
        else app_data_dir / "venv" / "bin" / "python3"
    )
    if launcher_python.is_file() and os.access(launcher_python, os.X_OK):
        # Keep the venv entrypoint path intact. Resolving its symlink to the
        # base interpreter discards pyvenv.cfg discovery and its site-packages.
        return launcher_python.absolute()
    return Path(sys.executable).absolute()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def redact_connection(connection: dict[str, Any], path: Path) -> dict[str, Any]:
    pid = _optional_int(connection.get("pid"))
    port = _optional_int(connection.get("port"))
    return {
        "path": str(path),
        "exists": path.exists(),
        "url": _safe_url(str(connection.get("url") or "")),
        "port": port if port is not None else connection.get("port"),
        "port_open": port_is_open(port) if port is not None else False,
        "pid": pid if pid is not None else connection.get("pid"),
        "pid_running": pid_is_running(pid) if pid is not None else False,
        "token_present": bool(connection.get("token")),
        "created_at": connection.get("created_at"),
    }


def _optional_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except Exception:
        return None


def configured_viewer_broker_port(explicit: int | None = None) -> int:
    raw: Any = (
        explicit
        if explicit is not None
        else process_environment().get("RUMI_VIEWER_BROKER_PORT")
    )
    if raw is None:
        return DEFAULT_VIEWER_BROKER_PORT
    text = str(raw)
    if not text or not text.isascii() or not text.isdecimal():
        raise SmokeRunnerError("RUMI_VIEWER_BROKER_PORT must be an ASCII decimal localhost port")
    port = int(text)
    if not 1 <= port <= 65535:
        raise SmokeRunnerError("RUMI_VIEWER_BROKER_PORT must be between 1 and 65535")
    return port


def generate_debug_instance_id() -> str:
    """Return a public, per-run debug-only Tauri instance identity.

    This value is deliberately not a credential.  It is only accepted by the
    debug-build single-instance isolation gate together with the independent
    broker nonce, exact connection path, isolated data root, and non-default
    broker port.
    """

    value = f"debug-{os.getpid()}-{time.time_ns()}-{secrets.token_hex(4)}"
    validate_debug_instance_id(value)
    return value


def create_unique_run_dir(prefix: str = "run") -> tuple[str, Path]:
    """Atomically reserve a collision-safe, public run identity and directory."""

    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    for _ in range(8):
        run_id = (
            f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-"
            f"{time.time_ns()}-{secrets.token_hex(4)}"
        )
        path = RUN_ROOT / run_id
        try:
            path.mkdir(mode=0o700)
            return run_id, path
        except FileExistsError:
            continue
    raise SmokeRunnerError("could not allocate a unique debug run directory")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Publish a JSON manifest without exposing a partially written file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def validate_debug_instance_id(value: str) -> str:
    if not isinstance(value, str) or not _DEBUG_INSTANCE_ID_RE.fullmatch(value):
        raise SmokeRunnerError(
            "debug Viewer instance ID must match debug-[A-Za-z0-9_-]{3,58}"
        )
    return value


def prepare_owned_viewer_debug_root(
    supervisor_dir: Path, connection_path: Path
) -> tuple[Path, Path]:
    """Create the harness-owned Viewer data root and exact connection path.

    The debug-only native lifecycle gate intentionally accepts no arbitrary
    connection path.  Keeping both under this run directory prevents a smoke
    run from attaching an already-running Viewer or its credentials.
    """

    root = (supervisor_dir / "viewer_user_data").resolve()
    expected_connection = root / "host_broker" / "connection.json"
    resolved_connection = connection_path.expanduser().resolve()
    if resolved_connection != expected_connection:
        raise SmokeRunnerError(
            "viewer-smoke-computer-use requires its owned connection path under "
            "the per-run Viewer user-data root"
        )
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError as error:
        raise SmokeRunnerError(f"failed to secure Viewer debug user-data root: {error}") from error
    return root, expected_connection


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def process_start_marker(pid: int) -> str:
    """Return the OS process birth marker used to reject PID reuse."""

    try:
        process = _SYSTEM_POPEN(
            ["ps", "-p", str(pid), "-o", "lstart="],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, _ = process.communicate(timeout=2)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except (NameError, OSError):
            pass
        return ""
    return " ".join(stdout.split()) if process.returncode == 0 else ""


def process_group_id(pid: int) -> int | None:
    """Return the current process group for a live process when available."""

    try:
        value = os.getpgid(pid)
    except OSError:
        return None
    return value if value > 0 else None


def request_json(url: str, *, token: str | None = None, timeout: float = 2.0) -> dict[str, Any]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


def http_status(url: str, *, token: str | None = None) -> dict[str, Any]:
    try:
        return {"ok": True, "response": request_json(url, token=token)}
    except urllib.error.HTTPError as error:
        return {"ok": False, "status": error.code, "error": str(error)}
    except Exception as error:
        return {"ok": False, "error": str(error)}


def port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def lsof_listener(port: int) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-F", "pc"],
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    current: dict[str, Any] = {}
    for line in completed.stdout.splitlines():
        if line.startswith("p"):
            current["pid"] = line[1:]
        elif line.startswith("c"):
            current["command"] = line[1:]
    return current or None


def load_desktop_app() -> dict[str, Any]:
    data = read_json(ECOSYSTEM_JSON)
    desktop_app = data.get("desktop_app")
    if not isinstance(desktop_app, dict):
        raise SystemExit(f"{ECOSYSTEM_JSON} has no desktop_app object")
    return desktop_app


def desktop_port(desktop_app: dict[str, Any], override: int | None) -> int:
    if override:
        return override
    env = desktop_app.get("env") if isinstance(desktop_app.get("env"), dict) else {}
    for key in ("RUMI_DEFAULTSPACK_PORT", "DEFAULTS_HTTP_PORT"):
        value = str(env.get(key) or "").strip()
        if value:
            return int(value)
    return 8766


def load_connection(
    path: Path, *, expected_port: int | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not path.exists():
        return {}, {
            "ok": False,
            "connection": {"path": str(path), "exists": False, "token_present": False},
            "health": {"ok": False, "error": "connection file not found"},
        }
    try:
        connection = read_json(path)
    except Exception as error:
        return {}, {
            "ok": False,
            "connection": {"path": str(path), "exists": True, "token_present": False},
            "health": {"ok": False, "error": f"invalid connection file: {error}"},
        }
    if not isinstance(connection, dict):
        return {}, {
            "ok": False,
            "connection": {"path": str(path), "exists": True, "token_present": False},
            "health": {"ok": False, "error": "invalid connection file: expected an object"},
        }
    port = _optional_int(connection.get("port"))
    pid = _optional_int(connection.get("pid"))
    created_at = _optional_int(connection.get("created_at"))
    host = str(connection.get("host") or "").strip()
    url = str(connection.get("url") or "").rstrip("/")
    expected_url = f"http://127.0.0.1:{port}" if port is not None else ""
    if (
        connection.get("version") != 1
        or host != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or url != expected_url
        or (expected_port is not None and port != expected_port)
        or str(connection.get("permission_subject") or "")
        not in HOST_BROKER_PERMISSION_SUBJECTS
        or pid is None
        or pid <= 0
        or created_at is None
        or created_at <= 0
    ):
        return connection, {
            "ok": False,
            "connection": redact_connection(connection, path),
            "health": {"ok": False, "error": "invalid connection file: required identity fields mismatch"},
        }
    redacted = redact_connection(connection, path)
    token = str(connection.get("token") or "")
    health = http_status(f"{url}/api/host/health") if url else {"ok": False, "error": "missing url"}
    permissions = (
        http_status(f"{url}/api/host/permissions", token=token)
        if url and token
        else {"ok": False, "error": "missing token"}
    )
    return connection, {
        "ok": bool(health.get("ok") and permissions.get("ok") and pid_is_running(pid)),
        "connection": redacted,
        "health": health,
        "permissions": permissions,
    }


def stale_connection_status(connection_path: Path, broker: dict[str, Any]) -> dict[str, Any]:
    connection = broker.get("connection") if isinstance(broker.get("connection"), dict) else {}
    if not connection_path.exists():
        return {"stale": False, "reason": "connection file not present"}
    health = broker.get("health") if isinstance(broker.get("health"), dict) else {}
    if str(health.get("error") or "").startswith("invalid connection file"):
        return {"stale": True, "reason": "connection file is invalid"}
    if connection and connection.get("pid") not in (None, "") and connection.get("pid_running") is False:
        return {"stale": True, "reason": "connection file PID is no longer running"}
    if connection and connection.get("port") not in (None, "") and connection.get("port_open") is False:
        return {"stale": True, "reason": "connection file port is not listening"}
    if connection and health.get("ok") is False:
        return {"stale": True, "reason": "connection file health check failed"}
    return {"stale": False, "reason": "connection is current"}


def latest_run() -> dict[str, Any]:
    if not LATEST_JSON.exists():
        return {"exists": False, "path": str(LATEST_JSON)}
    try:
        data = read_json(LATEST_JSON)
    except Exception as error:
        return {"exists": True, "path": str(LATEST_JSON), "error": str(error)}
    data["exists"] = True
    data["path"] = str(LATEST_JSON)
    return data


def _edge_haze_lease_summary(lease_path: Path) -> dict[str, Any]:
    item: dict[str, Any] = {"lease_path": str(lease_path), "lease_exists": lease_path.exists()}
    if not lease_path.exists():
        return item
    try:
        lease = read_json(lease_path)
        now = time.time()
        item["lease"] = {
            "schema": lease.get("schema"),
            "sequence_id": lease.get("sequence_id"),
            "action": lease.get("action"),
            "active": lease.get("active"),
            "deadline_epoch": lease.get("deadline_epoch"),
            "expired": float(lease.get("deadline_epoch") or 0) < now,
            "status_text": lease.get("status_text"),
            "target_window_present": bool(lease.get("target_window")),
        }
    except Exception as error:
        item["error"] = str(error)
    return item


def edge_haze_status(
    user_data: Path | None,
    *,
    broker_connection_path: Path | None = None,
) -> dict[str, Any]:
    if user_data is None:
        latest = latest_run()
        raw = latest.get("user_data")
        user_data = Path(raw) if isinstance(raw, str) and raw else None
    candidates: list[tuple[str, Path]] = []
    if user_data is not None:
        candidates.append(
            (
                "defaultspack_debug_user_data",
                user_data / "shared" / "helpers" / "edge_haze" / "edge_haze.lease.json",
            )
        )
    if broker_connection_path is not None:
        try:
            viewer_user_data = broker_connection_path.expanduser().parent.parent
            candidates.append(
                (
                    "viewer_broker_user_data",
                    viewer_user_data / "shared" / "helpers" / "edge_haze" / "edge_haze.lease.json",
                )
            )
        except Exception:
            pass
    if not candidates:
        return {"known": False}
    seen: set[str] = set()
    leases: list[dict[str, Any]] = []
    for source, lease_path in candidates:
        key = str(lease_path)
        if key in seen:
            continue
        seen.add(key)
        item = _edge_haze_lease_summary(lease_path)
        item["source"] = source
        leases.append(item)
    active = next((item for item in leases if item.get("lease_exists")), leases[0])
    status: dict[str, Any] = {
        "known": True,
        "lease_path": active.get("lease_path"),
        "lease_exists": any(bool(item.get("lease_exists")) for item in leases),
        "leases": leases,
    }
    if active.get("lease"):
        status["lease"] = active["lease"]
    if active.get("error"):
        status["error"] = active["error"]
    return status


def _conversation_items(chat_store: Path | None) -> list[dict[str, Any]]:
    if chat_store is None:
        return []
    items: list[dict[str, Any]] = []
    if chat_store.exists():
        try:
            data = read_json(chat_store)
            conversations = data.get("conversations") if isinstance(data, dict) else None
            if isinstance(conversations, dict):
                items.extend(item for item in conversations.values() if isinstance(item, dict))
            elif isinstance(conversations, list):
                items.extend(item for item in conversations if isinstance(item, dict))
        except Exception:
            pass
    history_root = chat_store.parent / "conversations"
    if history_root.exists():
        for history_path in sorted(history_root.glob("*/history.json")):
            try:
                data = read_json(history_path)
            except Exception:
                continue
            conversation = data.get("conversation") if isinstance(data, dict) else None
            if isinstance(conversation, dict):
                items.append(conversation)
    return items


def _redact_debug_value(
    value: Any,
    *,
    key: str = "",
    depth: int = 0,
    secrets_to_hide: tuple[str, ...] = (),
) -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in ("token", "secret", "api_key", "authorization", "password")):
        return "[redacted]" if value not in (None, "") else value
    if lowered in {
        "text",
        "value",
        "content",
        "clipboard",
        "query",
        "prompt",
        "match_text",
        "text_query",
        "input_text",
        "typed_text",
    } or lowered.endswith("_typed_text"):
        return "[redacted]" if value not in (None, "") else value
    if depth >= 4:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(k): _redact_debug_value(
                v,
                key=str(k),
                depth=depth + 1,
                secrets_to_hide=secrets_to_hide,
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _redact_debug_value(item, depth=depth + 1, secrets_to_hide=secrets_to_hide)
            for item in value[:12]
        ]
    if isinstance(value, str):
        output = _redact_string(value, secrets_to_hide=secrets_to_hide)
        if lowered in {"url", "href"} or lowered.endswith("_url"):
            output = _safe_url(output)
        if len(output) > 240:
            return output[:237] + "..."
        return output
    return value


def _pending_from_message(message: dict[str, Any]) -> dict[str, Any] | None:
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    for key in ("pending_approval", "pendingApproval", "pending_tool_approval"):
        pending = metadata.get(key)
        if isinstance(pending, dict):
            return pending
    for event in reversed(message.get("events") or []):
        if not isinstance(event, dict):
            continue
        if event.get("type") in {"approval_requested", "approval_required"} or event.get("phase") == "approval_requested":
            return event
    return None


def pending_approval_status(chat_store: Path | None) -> dict[str, Any]:
    latest: tuple[float, dict[str, Any], dict[str, Any], dict[str, Any]] | None = None
    for conversation in _conversation_items(chat_store):
        for message in conversation.get("messages") or []:
            if not isinstance(message, dict):
                continue
            pending = _pending_from_message(message)
            if not isinstance(pending, dict):
                continue
            created_at = float(message.get("created_at") or pending.get("created_at") or 0)
            if latest is None or created_at >= latest[0]:
                latest = (created_at, conversation, message, pending)
    if latest is None:
        return {"found": False, "chat_store": str(chat_store) if chat_store else None}
    _, conversation, message, pending = latest
    payload = pending.get("payload")
    if payload is None and isinstance(pending.get("details"), dict):
        payload = pending["details"].get("arguments")
    return {
        "found": True,
        "chat_store": str(chat_store) if chat_store else None,
        "conversation_id": conversation.get("id") or pending.get("conversation_id"),
        "message_id": message.get("id"),
        "request_id": pending.get("request_id") or pending.get("approval_request_id"),
        "tool": pending.get("toolName") or pending.get("tool_name") or pending.get("tool"),
        "operation": pending.get("operation") or pending.get("action"),
        "payload": _redact_debug_value(payload if isinstance(payload, dict) else pending),
    }


class DebugHttpClient:
    def __init__(
        self,
        base_url: str,
        api_token: str,
        browser_approval_token: str,
        *,
        timeout: float = 30.0,
        stream_timeout: float = DEFAULT_CHAT_STREAM_INACTIVITY_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.browser_approval_token = browser_approval_token
        self.timeout = timeout
        self.stream_timeout = stream_timeout
        self.csrf_token = "debug-smoke-" + secrets.token_urlsafe(18)
        self._extra_secrets: set[str] = set()

    @property
    def secrets_to_hide(self) -> tuple[str, ...]:
        return (self.api_token, self.browser_approval_token, *sorted(self._extra_secrets))

    def hide_secrets(self, *values: Any) -> None:
        self._extra_secrets.update(str(value) for value in values if str(value or ""))

    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = self.base_url + "/" + path.lstrip("/")
        if query:
            encoded = urllib.parse.urlencode(
                {key: value for key, value in query.items() if value is not None}
            )
            if encoded:
                url += "?" + encoded
        return url

    def _headers(self, method: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers["X-Rumi-CSRF"] = self.csrf_token
        headers.update(extra or {})
        return headers

    def _error_text(self, value: Any) -> str:
        if isinstance(value, dict):
            error_value = value.get("error")
            if isinstance(error_value, dict):
                code = str(error_value.get("code") or "API_ERROR")
                message = str(error_value.get("message") or "request failed")
                value = f"{code}: {message}"
            else:
                value = error_value or value.get("message") or "request failed"
        return _redact_string(str(value or "request failed"), secrets_to_hide=self.secrets_to_hide)

    def _open(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url(path, query),
            data=body,
            method=method.upper(),
            headers=self._headers(method, headers),
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw) if raw else {}
            except Exception:
                parsed = {}
            raise DebugApiError(
                f"{method.upper()} {path} failed with HTTP {exc.code}: {self._error_text(parsed)}"
            ) from None
        except urllib.error.URLError as exc:
            reason = self._error_text(getattr(exc, "reason", "connection failed"))
            raise DebugApiError(f"{method.upper()} {path} failed: {reason}") from None
        except Exception as exc:
            raise DebugApiError(
                f"{method.upper()} {path} failed: {self._error_text(exc)}"
            ) from None

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self._open(method, path, payload=payload, query=query, headers=headers) as response:
            raw = response.read()
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            raise DebugApiError(f"{method.upper()} {path} returned invalid JSON") from None
        if not isinstance(decoded, dict):
            raise DebugApiError(f"{method.upper()} {path} returned an invalid response")
        if decoded.get("status") == "error":
            raise DebugApiError(
                f"{method.upper()} {path} failed: {self._error_text(decoded)}"
            )
        data = decoded.get("data") if decoded.get("status") == "ok" else decoded
        if not isinstance(data, dict):
            raise DebugApiError(f"{method.upper()} {path} returned invalid data")
        return data

    def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", path, query=query)

    def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self.request("POST", path, payload=payload, query=query, headers=headers)

    def stream(self, path: str, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        if self.stream_timeout <= 0:
            raise DebugApiError("stream inactivity timeout must be positive")
        request = {
            "url": self._url(path),
            "headers": self._headers("POST"),
            "payload": payload,
            # This is a connect safeguard only. The parent watchdog is the
            # authoritative inactivity deadline and can terminate a worker
            # even when urllib is stuck inside buffered ``readline``.
            "connect_timeout": max(self.timeout, self.stream_timeout),
        }
        process = _start_debug_stream_worker(request)
        messages: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=64)
        stop_reader = threading.Event()

        def publish(record: dict[str, Any]) -> None:
            while not stop_reader.is_set():
                try:
                    messages.put(record, timeout=0.05)
                    return
                except queue.Full:
                    continue

        def read_worker_output() -> None:
            assert process.stdout is not None
            try:
                for raw_line in process.stdout:
                    try:
                        record = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        publish({"kind": "protocol_error"})
                        return
                    if not isinstance(record, dict):
                        publish({"kind": "protocol_error"})
                        return
                    publish(record)
            finally:
                publish({"kind": "worker_eof"})

        reader = threading.Thread(
            target=read_worker_output,
            name="defaultspack-debug-stream-reader",
            daemon=True,
        )
        try:
            reader.start()
        except BaseException:
            _stop_debug_stream_worker(process)
            raise
        data_lines: list[str] = []

        def consume() -> dict[str, Any] | None:
            if not data_lines:
                return None
            raw = "\n".join(data_lines)
            data_lines.clear()
            if not raw or raw == "[DONE]":
                return None
            try:
                event = json.loads(raw)
            except Exception:
                raise DebugApiError(
                    f"POST {path} returned a malformed stream event"
                ) from None
            if not isinstance(event, dict):
                raise DebugApiError(f"POST {path} returned an invalid stream event")
            return event

        deadline = time.monotonic() + self.stream_timeout
        try:
            terminal_seen = False
            worker_eof = False
            while not terminal_seen and not worker_eof:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DebugApiError(
                        f"POST {path} stream was inactive for "
                        f"{self.stream_timeout:g} seconds"
                    )
                try:
                    record = messages.get(timeout=remaining)
                except queue.Empty:
                    raise DebugApiError(
                        f"POST {path} stream was inactive for "
                        f"{self.stream_timeout:g} seconds"
                    ) from None
                kind = record.get("kind")
                if kind == "line":
                    deadline = time.monotonic() + self.stream_timeout
                    line = str(record.get("line") or "").rstrip("\r\n")
                    if not line:
                        event = consume()
                        if event is not None:
                            yield event
                            terminal_seen = event.get("type") in {"done", "error"}
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    # SSE comments and other fields are heartbeats: they reset
                    # inactivity but are not surfaced as application events.
                    continue
                if kind == "http_error":
                    raise DebugApiError(
                        f"POST {path} failed with HTTP {record.get('status')}: "
                        f"{self._error_text(record.get('body'))}"
                    )
                if kind == "transport_error":
                    raise DebugApiError(
                        f"POST {path} failed: "
                        f"{self._error_text(record.get('message'))}"
                    )
                if kind == "worker_eof":
                    worker_eof = True
                    continue
                raise DebugApiError(f"POST {path} stream worker protocol failed")
            if not terminal_seen:
                event = consume()
                if event is not None:
                    yield event
        finally:
            stop_reader.set()
            _stop_debug_stream_worker(process)
            reader.join(timeout=2.0)


def _stream_worker_environment() -> dict[str, str]:
    """Return the minimal non-provider environment for the stream subprocess."""

    allowed = {
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
    environment = {
        key: value for key, value in process_environment().items() if key in allowed
    }
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return environment


def _start_debug_stream_worker(request: Mapping[str, Any]) -> subprocess.Popen[bytes]:
    """Start a killable SSE reader without placing credentials in argv or env."""

    process = _SYSTEM_POPEN(
        [sys.executable, str(Path(__file__).resolve()), "--internal-stream-worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_stream_worker_environment(),
        bufsize=0,
    )
    assert process.stdin is not None
    try:
        encoded = json.dumps(dict(request), ensure_ascii=False).encode("utf-8")
        process.stdin.write(encoded)
        process.stdin.close()
    except BaseException:
        _stop_debug_stream_worker(process)
        raise
    return process


def _stop_debug_stream_worker(process: subprocess.Popen[bytes]) -> None:
    """Reap a stream worker, escalating only when graceful exit is impossible."""

    try:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
    finally:
        for pipe in (process.stdin, process.stdout):
            if pipe is not None and not pipe.closed:
                pipe.close()


def _emit_stream_worker_record(record: Mapping[str, Any]) -> None:
    """Write one private framed record to the parent process."""

    encoded = json.dumps(dict(record), ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


def _debug_stream_worker_main() -> int:
    """Own a potentially uninterruptible urllib response in a killable process."""

    response = None
    try:
        raw_request = sys.stdin.buffer.read()
        request = json.loads(raw_request.decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("invalid request")
        url = str(request["url"])
        headers = request["headers"]
        payload = request["payload"]
        timeout = float(request["connect_timeout"])
        if not isinstance(headers, dict) or not isinstance(payload, dict):
            raise ValueError("invalid request")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        http_request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={str(key): str(value) for key, value in headers.items()},
        )
        response = urllib.request.urlopen(http_request, timeout=timeout)
        for raw_line in response:
            _emit_stream_worker_record(
                {"kind": "line", "line": raw_line.decode("utf-8", errors="replace")}
            )
        return 0
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        _emit_stream_worker_record(
            {"kind": "http_error", "status": exc.code, "body": body}
        )
        return 1
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        _emit_stream_worker_record(
            {"kind": "transport_error", "message": str(reason or "connection failed")}
        )
        return 1
    except Exception:
        _emit_stream_worker_record(
            {"kind": "transport_error", "message": "stream worker failed"}
        )
        return 1
    finally:
        if response is not None:
            response.close()


class SmokeReporter:
    def __init__(self, stream: TextIO, *, secrets_to_hide: tuple[str, ...] = ()) -> None:
        self.stream = stream
        self.secrets_to_hide = tuple(value for value in secrets_to_hide if value)

    def hide_secrets(self, *values: Any) -> None:
        next_values = [str(value) for value in values if str(value or "")]
        self.secrets_to_hide = tuple(dict.fromkeys((*self.secrets_to_hide, *next_values)))

    def emit(self, event: str, **values: Any) -> None:
        payload = _redact_debug_value(
            {"event": event, **values},
            secrets_to_hide=self.secrets_to_hide,
        )
        self.stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.stream.flush()


def _compact_action(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    match = re.search(r"\b(?:browser|computer|job|tool)\.[A-Za-z0-9_.-]+\b", raw)
    if match:
        return match.group(0)
    if re.fullmatch(r"[A-Za-z0-9_.:-]+", raw):
        return raw
    return "[redacted]"


def _compact_window(value: Any) -> Any:
    if not isinstance(value, dict):
        return "[present]" if value not in (None, "") else value
    return {
        key: value[key]
        for key in (
            "id",
            "window_id",
            "app",
            "application",
            "bundle_id",
            "pid",
            "index",
            "selected",
            "focused",
        )
        if value.get(key) not in (None, "")
    }


_SAFE_TYPE_DIAGNOSTIC_KEYS = frozenset({
    "error_code",
    "input_strategy",
    "completion_verified",
    "input_dispatched",
    "dispatched_units",
    "target_pid_stable",
    "focused_element_stable",
    "failure_stage",
    "direct_ax_attempted",
    "direct_strategy",
    "mutation_observed",
    "direct_no_mutation_fallback",
})
_TYPE_DIAGNOSTIC_SIGNAL_KEYS = _SAFE_TYPE_DIAGNOSTIC_KEYS - {"error_code"}
_SAFE_TYPE_PREDISPATCH_CODES = frozenset({
    "TYPE_ACCESSIBILITY_NOT_TRUSTED",
    "TYPE_ACCESSIBILITY_API_UNAVAILABLE",
    "TYPE_SEMANTIC_PROTOCOL_INVALID",
    "TYPE_EXACT_WINDOW_REQUIRED",
    "TYPE_EXACT_WINDOW_NOT_FOUND",
    "TYPE_BACKGROUND_PRECONDITION_FAILED",
    "TYPE_SEMANTIC_SELECTOR_INVALID",
    "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
    "TYPE_SEMANTIC_CONTROL_DISABLED",
    "TYPE_SEMANTIC_VALUE_UNREADABLE",
    "TYPE_SEMANTIC_CONTROL_NOT_SETTABLE",
    "TYPE_SEMANTIC_CONTROL_AMBIGUOUS",
    "TYPE_SEMANTIC_WINDOW_OWNERSHIP_UNVERIFIED",
    "TYPE_SEMANTIC_COORDINATE_MISMATCH",
    "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE",
    # Compatibility-only for older native helpers.
    "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE",
})
_SAFE_TYPE_ERROR_CODES = frozenset({
    "TYPE_COMPLETION_NOT_VERIFIED",
    "TYPE_EVENT_UNAVAILABLE",
    "TYPE_FOREGROUND_TARGET_NOT_VERIFIED",
    "TYPE_SELECTION_INVALID",
    "TYPE_TARGET_DRIFTED",
    "TYPE_VERIFICATION_UNAVAILABLE",
}) | _SAFE_TYPE_PREDISPATCH_CODES


def _compact_type_diagnostics(value: Any) -> dict[str, Any]:
    """Extract only safe native typing diagnostics for supervised-run output."""
    diagnostics: dict[str, Any] = {}
    for record in _walk_records(value):
        candidates: list[dict[str, Any]] = []
        nested = record.get("diagnostics")
        if isinstance(nested, dict):
            candidates.append(nested)
        if "error_code" in record or any(key in record for key in _TYPE_DIAGNOSTIC_SIGNAL_KEYS):
            candidates.append(record)
        nested_error = record.get("error")
        if isinstance(nested_error, dict) and nested_error.get("code"):
            candidates.append({"error_code": nested_error.get("code")})
        for candidate in candidates:
            for key in _SAFE_TYPE_DIAGNOSTIC_KEYS:
                item = candidate.get(key)
                if key == "error_code" and item not in (None, ""):
                    item = str(item)
                    if item == "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE":
                        item = "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE"
                    if item not in _SAFE_TYPE_ERROR_CODES:
                        item = "TYPE_ERROR"
                if (
                    key not in diagnostics
                    and isinstance(item, (str, bool, int, float))
                    and not isinstance(item, bytes)
                ):
                    diagnostics[key] = item
    return diagnostics


def _compact_result_evidence(value: Any) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    allowed = {
        "action",
        "operation",
        "url",
        "current_url",
        "final_url",
        "app",
        "target_app",
        "foreground_app",
        "window",
        "target_window",
        "foreground_window",
        "driver",
        "seat",
        "background",
        "result_ok",
        "ok",
        "success",
        "screenshot_path",
        "model_image_path",
        "timestamp",
        "playing",
        "paused",
        "playback_state",
        "current_time",
        "duration",
    }
    for record in _walk_records(value):
        for key in allowed:
            if key in evidence or record.get(key) in (None, "", [], {}):
                continue
            item = record[key]
            if key in {"action", "operation"}:
                item = _compact_action(item)
            elif key in {"window", "target_window", "foreground_window"}:
                item = _compact_window(item)
            evidence[key] = item
    return evidence


def _compact_stream_event(event: dict[str, Any], turn: int) -> dict[str, Any] | None:
    event_type = str(event.get("type") or "").strip()
    if event_type in {"delta", "thinking_delta", "tool_call_delta"}:
        return None
    compact: dict[str, Any] = {"turn": turn, "type": event_type or "unknown"}
    for key in (
        "phase",
        "request_id",
        "approval_request_id",
        "permission_id",
        "tool_name",
        "tool_call_id",
        "action",
        "operation",
        "status",
        "risk_level",
        "recovery_kind",
        "is_error",
        "result_ok",
        "approval_replay",
        "app",
        "target_app",
        "foreground_app",
        "window",
        "target_window",
        "foreground_window",
        "url",
        "timestamp",
        "artifact_paths",
    ):
        if event.get(key) not in (None, "", [], {}):
            item = event[key]
            if key in {"action", "operation"}:
                item = _compact_action(item)
            elif key in {"window", "target_window", "foreground_window"}:
                item = _compact_window(item)
            compact[key] = item
    if isinstance(event.get("artifacts"), list):
        compact["artifacts"] = [
            {
                key: artifact.get(key)
                for key in ("type", "kind", "path", "url", "name")
                if artifact.get(key) not in (None, "")
            }
            for artifact in event["artifacts"][:8]
            if isinstance(artifact, dict)
        ]
    if event_type in {"tool_call_started", "approval_requested"}:
        for key in ("arguments", "payload"):
            if isinstance(event.get(key), dict):
                compact[key] = event[key]
    if event_type == "tool_call_completed" and isinstance(event.get("result"), dict):
        evidence = _compact_result_evidence(event["result"])
        if evidence:
            compact["result_evidence"] = evidence
        type_diagnostics = _compact_type_diagnostics(event["result"])
        if type_diagnostics:
            compact["type_diagnostics"] = type_diagnostics
    if event_type in {"message", "done", "user_message"}:
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        for key in ("id", "finish_reason", "model"):
            if message.get(key) not in (None, ""):
                compact[f"message_{key}"] = message[key]
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        for metadata_key in ("pending_approval", "pending_authority_approval", "pendingAuthorityApproval"):
            pending = metadata.get(metadata_key)
            if isinstance(pending, dict):
                compact[metadata_key] = {
                    key: pending.get(key)
                    for key in (
                        "request_id",
                        "approval_request_id",
                        "permission_id",
                        "tool_name",
                        "action",
                        "operation",
                        "risk_level",
                    )
                    if pending.get(key) not in (None, "")
                }
    if event_type == "error":
        compact["error"] = event.get("error") or event.get("message") or "stream failed"
    return compact


def _resolve_artifact_path(value: Any) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        path = LATEST_JSON.parent / path
    return path


def _read_debug_token(artifact: dict[str, Any], keys: tuple[str, ...], label: str) -> str:
    raw_path = next((artifact.get(key) for key in keys if artifact.get(key)), None)
    if not raw_path:
        raise SmokeRunnerError(f"latest run has no {label} token-file path")
    path = _resolve_artifact_path(raw_path)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SmokeRunnerError(f"could not read {label} token file: {exc.strerror or 'unavailable'}") from None
    if not token:
        raise SmokeRunnerError(f"{label} token file is empty")
    return token


def load_smoke_configuration(port_override: int | None = None) -> dict[str, Any]:
    """Load tokens only for the currently validated harness-owned listener."""

    if not LATEST_JSON.exists():
        raise SmokeRunnerError(f"launch artifact not found: {LATEST_JSON}")
    try:
        artifact = read_json(LATEST_JSON)
    except Exception as exc:
        raise SmokeRunnerError(f"could not read launch artifact: {exc}") from None
    details = _validated_owned_launch_details(artifact)
    if details is None:
        raise SmokeRunnerError("latest run is not a validated owned launch")
    pid, port, run_dir, manifest = details
    if port_override is not None and port_override != port:
        raise SmokeRunnerError("smoke port does not match the owned launch manifest")
    listener = lsof_listener(port)
    listener_pid = _optional_int(listener.get("pid")) if listener else None
    if listener_pid != pid or not pid_is_running(pid):
        raise SmokeRunnerError("validated launch is not the active owned listener")
    api_token = _read_owned_debug_token(
        manifest, run_dir, "token_file", ".desktop_api_token", "local API"
    )
    return {
        "artifact": manifest,
        "base_url": f"http://127.0.0.1:{port}",
        "port": port,
        "api_token": api_token,
        "browser_approval_token": "",
    }


def _read_owned_debug_token(
    artifact: Mapping[str, Any],
    run_dir: Path,
    key: str,
    filename: str,
    label: str,
) -> str:
    """Read one canonical, owner-only, non-symlink token file."""

    expected = run_dir / filename
    raw = str(artifact.get(key) or "")
    if not raw or Path(raw).resolve() != expected:
        raise SmokeRunnerError(f"owned launch has an invalid {label} token-file path")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(expected, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise SmokeRunnerError(f"owned launch has an insecure {label} token file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            descriptor = -1
            token = source.read().strip()
    except SmokeRunnerError:
        raise
    except OSError as exc:
        raise SmokeRunnerError(
            f"could not read {label} token file: {exc.strerror or 'unavailable'}"
        ) from None
    finally:
        if "descriptor" in locals() and descriptor >= 0:
            os.close(descriptor)
    if not token:
        raise SmokeRunnerError(f"{label} token file is empty")
    return token


def _message_request(
    text: str,
    *,
    metadata: dict[str, Any] | None = None,
    tools: list[str] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "user", "content": text}
    if metadata is not None:
        message["metadata"] = metadata
    return {
        "message": message,
        **({"tools": tools} if tools is not None else {}),
        "params": dict(params or {}),
    }


def _smoke_tool_params(*, required: bool = False, tool_name: str = SMOKE_TOOL) -> dict[str, Any]:
    return {
        "tool_choice": "required" if required else "auto",
        "parallel_tool_calls": False,
        "tool_policy": {
            "action_approval_mode": "ask",
            "selected_tools": [tool_name],
        },
    }


def _walk_records(value: Any, *, depth: int = 0) -> Iterator[dict[str, Any]]:
    if depth > 8:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_records(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_records(child, depth=depth + 1)


def direct_background_action_plan(run_nonce: str) -> list[dict[str, Any]]:
    """Return the fixed, provider-free Atlas background-input acceptance plan."""

    safe_nonce = re.sub(r"[^A-Za-z0-9_-]+", "-", str(run_nonce or "")).strip("-")
    if not safe_nonce:
        raise SmokeRunnerError("direct computer-use run nonce is invalid")
    unique_text = f"rumi-background-delivery-{safe_nonce}"

    def semantic_address_type(label: str, text: str) -> dict[str, Any]:
        return {
            "label": label,
            "action": "computer.type",
            "payload": {
                "app": DIRECT_ATLAS_APP,
                "target_control": "browser_address",
                "background": True,
                "focus": False,
                "include_screenshot": False,
                "text": text,
            },
            "wait_after": 0.0,
        }

    def screenshot(label: str) -> dict[str, Any]:
        return {
            "label": label,
            "action": "computer.screenshot",
            "payload": {"app": DIRECT_ATLAS_APP},
            "wait_after": 0.0,
        }

    return [
        semantic_address_type("unique_address_set", unique_text),
        screenshot("unique_text_evidence"),
    ]


def _validate_direct_action(action: str, payload: dict[str, Any]) -> None:
    if str(payload.get("app") or "") != DIRECT_ATLAS_APP:
        raise SmokeRunnerError("direct computer-use action must target ChatGPT Atlas by canonical app name")
    if "approved" in payload:
        raise SmokeRunnerError("direct computer-use action must not trust a client approved flag")
    if action in _DIRECT_BACKGROUND_ACTIONS:
        if payload.get("background") is not True:
            raise SmokeRunnerError("direct mutation action must explicitly request background=true")
        if payload.get("focus") is not False or payload.get("include_screenshot") is not False:
            raise SmokeRunnerError(
                "direct mutation action must explicitly use focus=false and include_screenshot=false"
            )
        forbidden = {
            key
            for key in ("fallback", "foreground", "physical", "mode", "method", "driver")
            if key in payload
        }
        if forbidden:
            raise SmokeRunnerError(
                "direct mutation action forbids foreground/fallback controls: "
                + ", ".join(sorted(forbidden))
            )
        if action == "computer.type" and "target_control" in payload:
            if set(payload) != {
                "app",
                "window",
                "target_control",
                "text",
                "background",
                "focus",
                "include_screenshot",
            }:
                raise SmokeRunnerError("TYPE_DELIVERY_POLICY_VIOLATION")
            if payload.get("target_control") != "browser_address":
                raise SmokeRunnerError("TYPE_DELIVERY_POLICY_VIOLATION")
            _direct_atlas_window_binding(payload.get("window"))
    elif action == "computer.screenshot":
        if set(payload) != {"app", "window"}:
            raise SmokeRunnerError("SCREENSHOT_TARGET_UNAVAILABLE")
        _direct_atlas_window_binding(payload.get("window"))
    else:
        raise SmokeRunnerError(f"direct computer-use plan contains unsupported action {action}")


def _direct_positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not number.is_integer() or number <= 0:
        return None
    return int(number)


def _direct_geometry_int(value: Any, *, positive: bool) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not number.is_integer() or (positive and number <= 0):
        return None
    return int(number)


def _direct_atlas_window_binding(value: Any) -> dict[str, Any]:
    """Return a title-free exact Atlas binding, or fail with a fixed enum."""

    if not isinstance(value, dict) or str(value.get("app") or "") != DIRECT_ATLAS_APP:
        raise SmokeRunnerError("SCREENSHOT_TARGET_UNAVAILABLE")
    pid = _direct_positive_int(value.get("pid"))
    window_id = _direct_positive_int(value.get("window_id"))
    x = _direct_geometry_int(value.get("x"), positive=False)
    y = _direct_geometry_int(value.get("y"), positive=False)
    width = _direct_geometry_int(value.get("width"), positive=True)
    height = _direct_geometry_int(value.get("height"), positive=True)
    if None in {pid, window_id, x, y, width, height}:
        raise SmokeRunnerError("SCREENSHOT_TARGET_UNAVAILABLE")
    return {
        "app": DIRECT_ATLAS_APP,
        "pid": pid,
        "window_id": window_id,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
    }


def _direct_selection_facts(widget: Any) -> dict[str, Any]:
    """Extract only fixed selection facts from the action-owned root result."""

    if not isinstance(widget, dict):
        return {}
    facts: dict[str, Any] = {}
    for key in _DIRECT_SELECT_BOOL_FIELDS:
        value = widget.get(key)
        if isinstance(value, bool):
            facts[key] = value
    for key, cap in _DIRECT_SELECT_COUNT_CAPS.items():
        value = widget.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            facts[key] = max(0, min(cap, value))
    for key, allowed in _DIRECT_SELECT_ENUM_FIELDS.items():
        value = widget.get(key)
        if value in allowed:
            facts[key] = value
    return facts


def _direct_selection_observation_facts(
    widget: Any,
    *,
    observation_index: int,
    final_facts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one attempt's safe source facts plus optional final retry summary."""

    facts = _direct_selection_facts(widget)
    for key in _DIRECT_SELECT_HARNESS_FINAL_FIELDS:
        facts.pop(key, None)
    facts["selection_observation_index"] = max(1, min(2, observation_index))
    for key, value in dict(final_facts or {}).items():
        if key in _DIRECT_SELECT_BOOL_FIELDS and isinstance(value, bool):
            facts[key] = value
        elif key in _DIRECT_SELECT_COUNT_CAPS and isinstance(value, int) and not isinstance(value, bool):
            facts[key] = max(0, min(_DIRECT_SELECT_COUNT_CAPS[key], value))
        elif key in _DIRECT_SELECT_ENUM_FIELDS and value in _DIRECT_SELECT_ENUM_FIELDS[key]:
            facts[key] = value
    return facts


def _selection_reobservation_eligible(
    error: DirectSelectionContractError,
    facts: Mapping[str, Any],
    *,
    background_unchanged: bool,
) -> bool:
    """Apply the fixed fail-closed predicate for the sole second observation."""

    return bool(
        error.error_code == "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED"
        and facts.get("selection_swift_helper_response_contract") == "valid_success"
        and facts.get("selection_swift_helper_contract_version_class") == "expected"
        and facts.get("selection_activation_policy") == "not_requested"
        and facts.get("selection_focus_requested") is False
        and facts.get("selection_inventory_instrumentation_consistent") is True
        and facts.get("selection_permission_request_api_invoked") is not True
        and facts.get("selection_permission_diagnostic_outcome")
        not in {"instrumentation_inconsistent", "forbidden_action_required"}
        and background_unchanged
        and (
            facts.get("selection_nsworkspace_target_process_present") is True
            or facts.get("selection_later_source_target_match_present") is True
        )
    )


def _emit_direct_selection_observation(
    reporter: SmokeReporter,
    widget: Any,
    *,
    observation_index: int,
    final_facts: Mapping[str, Any] | None = None,
) -> None:
    reporter.emit(
        "viewer_direct_selection_observation",
        **_direct_selection_observation_facts(
            widget,
            observation_index=observation_index,
            final_facts=final_facts,
        ),
    )


def _direct_permission_fact_stability(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare only closed permission/trust facts across the two observations."""

    compared = 0
    changed = 0
    for key in _DIRECT_SELECT_PERMISSION_STABILITY_FIELDS:
        first_present = key in first
        second_present = key in second
        if not first_present and not second_present:
            continue
        if not first_present or not second_present:
            continue
        compared += 1
        if first.get(key) != second.get(key):
            changed += 1
    return {
        "selection_permission_fact_stability": (
            "unknown" if compared == 0 else "changed" if changed else "stable"
        ),
        "selection_permission_fact_change_count": min(4, changed),
    }


def _direct_visibility_fact_stability(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare only bounded, diagnostic-only topology facts across retries."""

    compared = 0
    changed = 0
    for key in _DIRECT_SELECT_VISIBILITY_STABILITY_FIELDS:
        if key not in first or key not in second:
            continue
        compared += 1
        if first.get(key) != second.get(key):
            changed += 1
    return {
        "selection_visibility_fact_stability": (
            "unknown" if compared == 0 else "changed" if changed else "stable"
        ),
        "selection_visibility_fact_change_count": min(8, changed),
    }


def _direct_permission_diagnostic_contract(
    facts: Mapping[str, Any],
    *,
    selected: bool,
) -> tuple[dict[str, Any], tuple[str, str] | None]:
    """Return the fixed authoritative-permission gate, if one takes precedence."""

    normalized = dict(facts)
    inconsistent = normalized.get("selection_permission_request_api_invoked") is True
    authoritative_source = normalized.get("selection_authoritative_permission_source")
    source_prefix = {
        "swift_host": "swift",
        "quartz": "quartz",
        "system_events": "system_events",
    }.get(authoritative_source)
    if source_prefix in {"swift", "quartz"}:
        source = source_prefix
        query_key = f"selection_{source}_cg_all_windows_query_outcome"
        nonactionable_key = f"selection_{source}_all_windows_nonactionable"
        if query_key in normalized and normalized.get(nonactionable_key) is not True:
            inconsistent = True

    stability = normalized.get("selection_permission_fact_stability")
    change_count = normalized.get("selection_permission_fact_change_count")
    if stability == "changed":
        inconsistent = True
    elif stability == "stable" and change_count not in {None, 0}:
        inconsistent = True
    elif isinstance(change_count, int) and not isinstance(change_count, bool) and change_count > 0:
        inconsistent = True

    if inconsistent:
        normalized["selection_authoritative_permission_outcome"] = (
            "forbidden_action_required"
            if normalized.get("selection_permission_request_api_invoked") is True
            else "instrumentation_inconsistent"
        )
    authoritative_outcome = normalized.get("selection_authoritative_permission_outcome")
    if normalized.get("selection_permission_request_api_invoked") is True:
        return normalized, (
            "SELECT_WINDOW_PERMISSION_REQUEST_FORBIDDEN",
            "safety_policy_validation",
        )
    if not selected:
        # Preserve the controller's exact selection failure.  Secondary
        # diagnostics are intentionally non-authoritative and cannot replace it.
        return normalized, None
    if source_prefix is None:
        return normalized, (
            "SELECT_WINDOW_RESULT_INVALID",
            "authoritative_diagnostic_validation",
        )
    if authoritative_outcome == "permissions_ok":
        return normalized, None
    if authoritative_outcome in {
        "accessibility_denied",
        "screen_capture_denied",
        "system_events_denied",
    }:
        return normalized, (
            "SELECT_WINDOW_AUTHORITATIVE_PERMISSION_DENIED",
            "authoritative_permission_validation",
        )
    if authoritative_outcome in {"unknown", "unavailable"}:
        return normalized, (
            "SELECT_WINDOW_AUTHORITATIVE_PERMISSION_UNAVAILABLE",
            "authoritative_permission_validation",
        )
    return normalized, (
        "SELECT_WINDOW_RESULT_INVALID",
        "authoritative_diagnostic_validation",
    )


def _direct_select_contract(widget: Any) -> dict[str, Any]:
    """Validate the exact selection contract without consulting nested records."""

    if not isinstance(widget, dict) or widget.get("action") != "computer.select_window":
        raise DirectSelectionContractError(
            "SELECT_WINDOW_RESULT_SCOPE_INVALID",
            failure_stage="result_scope",
        )
    facts = _direct_selection_facts(widget)
    if widget.get("selected") is not True:
        error_code = str(widget.get("error_code") or "")
        if error_code not in _DIRECT_SELECT_CONTROLLER_ERROR_CODES:
            error_code = "SELECT_WINDOW_RESULT_INVALID"
        raise DirectSelectionContractError(
            error_code,
            failure_stage="contract_validation",
            facts=facts,
        )
    if (
        widget.get("selection_exact_binding_required") is not True
        or widget.get("selection_exact_binding_present") is not True
    ):
        raise DirectSelectionContractError(
            "SELECT_WINDOW_TRANSPORT_CONTRACT_INVALID",
            failure_stage="contract_validation",
            facts=facts,
        )
    try:
        return _direct_atlas_window_binding(widget.get("target_window"))
    except SmokeRunnerError:
        raise DirectSelectionContractError(
            "SELECT_WINDOW_TRANSPORT_CONTRACT_INVALID",
            failure_stage="contract_validation",
            facts=facts,
        ) from None


def _direct_selection_api_failure(error: DebugApiError) -> DirectSelectionContractError:
    raw = str(error or "")
    error_code = next(
        (code for code in sorted(_DIRECT_SELECT_CONTROLLER_ERROR_CODES) if code in raw),
        "SELECT_WINDOW_RESULT_INVALID",
    )
    return DirectSelectionContractError(
        error_code,
        failure_stage="contract_validation",
    )


def _emit_direct_selection_failure(
    reporter: SmokeReporter,
    error: DirectSelectionContractError,
) -> None:
    reporter.emit(
        "viewer_direct_selection_failed",
        ok=False,
        error_code=error.error_code,
        failure_stage=error.failure_stage,
        **error.facts,
    )


def _direct_probe_owned_records(widget: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return only the probe root and its explicitly owned safe children."""

    records: list[dict[str, Any]] = [dict(widget)]
    result = widget.get("result")
    if isinstance(result, dict):
        records.append(result)
    for owner in tuple(records):
        diagnostics = owner.get("diagnostics")
        if isinstance(diagnostics, dict):
            records.append(diagnostics)
    return records


def _direct_probe_facts(
    widget: Mapping[str, Any] | Any,
    *,
    require_action: bool = True,
) -> dict[str, Any]:
    """Extract bounded probe facts without recursively accepting unrelated data."""

    if not isinstance(widget, Mapping):
        return {}
    if require_action and widget.get("action") != "computer.probe_text_control":
        return {}
    records = _direct_probe_owned_records(widget)
    safe: dict[str, Any] = {}
    for key in _DIRECT_PROBE_BOOL_FIELDS:
        for record in records:
            value = record.get(key)
            if isinstance(value, bool):
                safe[key] = value
                break
    counts_truncated = safe.get("semantic_counts_truncated") is True
    saw_count = False
    for key, cap in _DIRECT_PROBE_COUNT_CAPS.items():
        for record in records:
            value = record.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                safe[key] = max(0, min(cap, value))
                counts_truncated = counts_truncated or value < 0 or value > cap
                saw_count = True
                break
    if saw_count:
        safe["semantic_counts_truncated"] = counts_truncated
    for key, allowed in _DIRECT_PROBE_ENUM_FIELDS.items():
        for record in records:
            value = str(record.get(key) or "")
            if value in allowed:
                safe[key] = value
                break
    for record in records:
        error_code = str(record.get("error_code") or "")
        if error_code == "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE":
            error_code = "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE"
        if error_code in _DIRECT_PROBE_ERROR_CODES:
            safe["error_code"] = error_code
            break
    return safe


def _direct_probe_failure_code(facts: Mapping[str, Any]) -> str:
    # The exact-window traversal is authoritative.  App-wide discovery is
    # diagnostic-only and may be incomplete without promoting a clean
    # role-absent result to discovery-incomplete (or an unlisted-role error).
    stage = str(facts.get("semantic_discovery_stage") or "")
    exact_role_absent = (
        stage == "role_absent"
        and facts.get("semantic_window_scan_complete") is True
        and facts.get("semantic_window_scan_truncated") is False
        and facts.get("semantic_window_depth_truncated") is False
        and facts.get("semantic_window_allowed_role_count", 0) == 0
    )
    if exact_role_absent:
        return "TYPE_SEMANTIC_CONTROL_NOT_FOUND"
    explicit = str(facts.get("error_code") or "")
    if explicit in _DIRECT_PROBE_ERROR_CODES:
        return explicit
    if stage == "scan_incomplete":
        return "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE"
    if stage == "ambiguous":
        return "TYPE_SEMANTIC_CONTROL_AMBIGUOUS"
    if stage == "window_ownership_unverified":
        return "TYPE_SEMANTIC_WINDOW_OWNERSHIP_UNVERIFIED"
    if stage == "disabled":
        return "TYPE_SEMANTIC_CONTROL_DISABLED"
    if stage == "value_unreadable":
        return "TYPE_SEMANTIC_VALUE_UNREADABLE"
    if stage == "not_settable":
        return "TYPE_SEMANTIC_CONTROL_NOT_SETTABLE"
    if facts.get("semantic_unlisted_mutation_ready_count", 0) > 0:
        return "TYPE_SEMANTIC_ROLE_CLASS_UNRESOLVED"
    return "TYPE_SEMANTIC_CONTROL_NOT_FOUND"


def _direct_probe_contract(widget: Any) -> dict[str, Any]:
    """Require one complete, unique, existing-role background probe result."""

    if not isinstance(widget, dict) or widget.get("action") != "computer.probe_text_control":
        raise DirectProbeContractError(
            "PROBE_RESULT_SCOPE_INVALID",
            failure_stage="result_scope",
        )
    facts = _direct_probe_facts(widget)
    if facts.get("probe_completed") is not True:
        error_code = str(facts.get("error_code") or "")
        if error_code.startswith("TYPE_EXACT_WINDOW_") or error_code == "TYPE_TARGET_DRIFTED":
            raise DirectProbeContractError(
                error_code,
                failure_stage="exact_window_resolution",
                facts=facts,
            )
        raise DirectProbeContractError(
            "PROBE_TRANSPORT_CONTRACT_INVALID",
            facts=facts,
        )
    ready = facts.get("semantic_control_ready") is True
    if not ready:
        raise DirectProbeContractError(
            _direct_probe_failure_code(facts),
            facts=facts,
        )
    required = {
        "semantic_discovery_stage": "ready",
        "semantic_traversal_order": "breadth_first",
        "semantic_scan_scope": "exact_window_descendants",
        "semantic_ownership_proof": "window_descendant",
        "semantic_window_scan_complete": True,
        "semantic_window_scan_truncated": False,
        "semantic_window_depth_truncated": False,
        "semantic_actionable_scan_complete": True,
        "semantic_control_resolved": True,
        "semantic_control_role_allowed": True,
        "semantic_control_settable": True,
        "semantic_final_candidate_count": 1,
    }
    if any(facts.get(key) != expected for key, expected in required.items()):
        raise DirectProbeContractError(
            "PROBE_TRANSPORT_CONTRACT_INVALID",
            facts=facts,
        )
    return facts


def _direct_native_frontmost_failed(facts: Mapping[str, Any]) -> bool:
    """Trust only a completed native check as operation-local foreground proof."""

    return facts.get("native_frontmost_check_completed") is True and any(
        (
            facts.get("native_target_non_frontmost_before") is False,
            facts.get("native_target_non_frontmost_after") is False,
            facts.get("native_frontmost_unchanged") is False,
        )
    )


def _emit_direct_probe_failure(
    reporter: SmokeReporter,
    error: DirectProbeContractError,
) -> None:
    reporter.emit(
        "viewer_direct_probe_failed",
        ok=False,
        **_direct_probe_failure_payload(error),
    )


def _direct_probe_failure_payload(
    error: DirectProbeContractError,
) -> dict[str, Any]:
    """Build the shared bounded failure contract for both direct events."""

    return {
        "error": error.error_code,
        "error_code": error.error_code,
        "classification": "PROBE_PRECONDITION_FAILED",
        "failure_stage": error.failure_stage,
        **error.facts,
    }


def frontmost_application_name() -> str:
    """Read, but never change, the frontmost macOS application."""

    try:
        completed = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first application process whose frontmost is true',
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as error:
        raise SmokeRunnerError(f"could not read frontmost application: {error}") from None
    name = str(completed.stdout or "").strip()
    if not name:
        raise SmokeRunnerError("frontmost application could not be identified")
    return name


def _frontmost_is_atlas(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", str(name or "").casefold())
    return "atlas" in normalized


def _direct_widget(response: dict[str, Any]) -> dict[str, Any]:
    widget = response.get("widget") if isinstance(response, dict) else None
    if not isinstance(widget, dict):
        raise SmokeRunnerError("browser-computer response is missing its full widget result")
    return widget


def _direct_approval_request_id(widget: dict[str, Any]) -> str:
    ids = {
        _record_request_id(record)
        for record in _walk_records(widget)
        if _record_request_id(record)
        and bool(record.get("approval_required") or record.get("requires_approval"))
    }
    if len(ids) != 1:
        raise SmokeRunnerError("direct computer-use action did not produce exactly one coding approval request")
    return next(iter(ids))


def _direct_first_scalar(value: Any, key: str) -> Any:
    for record in _walk_records(value):
        if key in record and record.get(key) not in (None, ""):
            return record.get(key)
    return None


def _direct_edge_haze(value: Any) -> dict[str, Any]:
    for record in _walk_records(value):
        edge_haze = record.get("edge_haze")
        if isinstance(edge_haze, dict):
            return {
                key: edge_haze.get(key)
                for key in ("attempted", "started", "disabled")
                if edge_haze.get(key) not in (None, "")
            }
    return {}


_DIRECT_UNVERIFIED_TYPE_ERROR = "TYPE_COMPLETION_NOT_VERIFIED"
_DIRECT_TYPE_FACT_ALIASES = {
    "input_dispatched": ("input_dispatched",),
    "background": ("background",),
    "foreground": ("foreground",),
    "delivered": ("delivered",),
    "executed": ("executed",),
    "uses_physical_input": ("uses_physical_input",),
    "requires_foreground": ("requires_foreground",),
    "can_parallel": ("can_parallel", "can_parallel_user_work"),
}


def _direct_owned_type_records(widget: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only the type action envelope and its fixed result/diagnostic children."""

    records = [widget]
    result = widget.get("result")
    if isinstance(result, dict):
        records.append(result)
    for owner in tuple(records):
        diagnostics = owner.get("diagnostics")
        if isinstance(diagnostics, dict):
            records.append(diagnostics)
    return records


def _direct_owned_type_scalar(widget: dict[str, Any], key: str) -> Any:
    for record in _direct_owned_type_records(widget):
        value = record.get(key)
        if isinstance(value, (str, bool, int, float)) and value not in (None, ""):
            return value
    return None


def _direct_type_error_code(
    widget: dict[str, Any],
    *,
    input_dispatched: bool | None,
) -> str:
    """Select only fixed action-owned codes, with narrow old-helper compatibility."""

    def record_codes(records: list[dict[str, Any]]) -> list[str]:
        codes: list[str] = []
        for record in records:
            candidates: list[Any] = [record.get("error_code")]
            nested_error = record.get("error")
            if isinstance(nested_error, dict):
                candidates.append(nested_error.get("code"))
            for value in candidates:
                code = str(value or "").strip()
                if code and code not in codes:
                    codes.append(code)
        return codes

    owned_records = _direct_owned_type_records(widget)
    top_codes = record_codes([widget])
    inner_codes = record_codes(owned_records[1:])
    if input_dispatched is False:
        for code in top_codes:
            if code in _SAFE_TYPE_PREDISPATCH_CODES:
                return code
        if not top_codes or _DIRECT_UNVERIFIED_TYPE_ERROR in top_codes:
            for code in inner_codes:
                if code in _SAFE_TYPE_PREDISPATCH_CODES:
                    return code
    for code in (*top_codes, *inner_codes):
        if code in _SAFE_TYPE_ERROR_CODES:
            return code
    if top_codes or inner_codes:
        return "UNEXPECTED_TYPE_FAILURE"
    return ""


def _direct_type_delivery_facts(widget: dict[str, Any]) -> dict[str, bool]:
    facts: dict[str, bool] = {}
    for canonical, aliases in _DIRECT_TYPE_FACT_ALIASES.items():
        for record in _direct_owned_type_records(widget):
            found = False
            for alias in aliases:
                value = record.get(alias)
                if isinstance(value, bool):
                    facts[canonical] = value
                    found = True
                    break
            if found:
                break
    return facts


def _direct_action_is_error(action: str, widget: dict[str, Any]) -> bool:
    """Read error state from the action envelope, not unrelated nested records."""

    if str(widget.get("action") or "") == action:
        return widget.get("is_error") is True
    for record in _walk_records(widget):
        if str(record.get("action") or "") == action:
            return record.get("is_error") is True
    return widget.get("is_error") is True


def _direct_result_evidence(
    action: str,
    widget: dict[str, Any],
    *,
    approval_approved: bool = True,
    frontmost_non_atlas: bool = True,
    frontmost_unchanged: bool = True,
) -> dict[str, Any]:
    is_error = _direct_action_is_error(action, widget)
    type_diagnostics = _compact_type_diagnostics(widget)
    input_value = (
        _direct_owned_type_scalar(widget, "input_dispatched")
        if action == "computer.type"
        else None
    )
    input_dispatched = input_value if isinstance(input_value, bool) else None
    error_code = (
        _direct_type_error_code(widget, input_dispatched=input_dispatched)
        if action == "computer.type"
        else ""
    )
    if action == "computer.type" and is_error and input_dispatched is None:
        raise SmokeRunnerError("computer.type failed with DIAGNOSTICS_MISSING")
    if (
        action == "computer.type"
        and is_error
        and input_dispatched is False
        and error_code in _SAFE_TYPE_PREDISPATCH_CODES
    ):
        raise DirectTypeClassificationError(error_code)
    type_completion_unverified = bool(
        action == "computer.type"
        and is_error
        and input_dispatched is True
        and error_code == _DIRECT_UNVERIFIED_TYPE_ERROR
    )
    if is_error and not type_completion_unverified:
        if action == "computer.key":
            raise SmokeRunnerError("computer.key failed with KEY_EFFECT_NOT_VERIFIED")
        raise SmokeRunnerError(f"{action} failed with TYPE_HARD_FAILURE")
    evidence: dict[str, Any] = {
        "action": action,
        "is_error": is_error,
        "executed": _direct_first_scalar(widget, "executed") is True,
    }
    if type_diagnostics:
        evidence["type_diagnostics"] = type_diagnostics
    if type_completion_unverified:
        facts = _direct_type_delivery_facts(widget)
        facts.update(
            {
                "approval_approved": bool(approval_approved),
                "frontmost_non_atlas": bool(frontmost_non_atlas),
                "frontmost_unchanged": bool(frontmost_unchanged),
            }
        )
        hard_policy_failure = bool(
            facts.get("foreground") is True
            or facts.get("background") is False
            or facts.get("uses_physical_input") is True
            or facts.get("requires_foreground") is True
            or approval_approved is not True
            or frontmost_non_atlas is not True
            or frontmost_unchanged is not True
        )
        if hard_policy_failure:
            raise SmokeRunnerError("computer.type failed with TYPE_DELIVERY_POLICY_VIOLATION")
        required = {
            "input_dispatched": True,
            "background": True,
            "foreground": False,
            "delivered": True,
            "executed": True,
            "uses_physical_input": False,
            "requires_foreground": False,
            "can_parallel": True,
            "approval_approved": True,
            "frontmost_non_atlas": True,
            "frontmost_unchanged": True,
        }
        if any(key not in facts or facts.get(key) is not expected for key, expected in required.items()):
            raise SmokeRunnerError("computer.type failed with DIAGNOSTICS_MISSING")
        evidence.update(
            {
                "classification": "DELIVERY_UNVERIFIED",
                "continue_to_screenshot": True,
                "error_code": _DIRECT_UNVERIFIED_TYPE_ERROR,
                "delivery_facts": {key: facts[key] for key in required},
            }
        )
        return evidence
    if action in _DIRECT_BACKGROUND_ACTIONS:
        if action == "computer.key" and _direct_first_scalar(widget, "completion_verified") is not True:
            raise SmokeRunnerError("computer.key failed with KEY_EFFECT_NOT_VERIFIED")
        if action == "computer.type" and _direct_first_scalar(widget, "completion_verified") is not True:
            raise SmokeRunnerError("computer.type failed with TYPE_HARD_FAILURE")
        if _direct_first_scalar(widget, "executed") is not True:
            raise SmokeRunnerError(f"{action} was not executed")
        if _direct_first_scalar(widget, "background") is not True:
            raise SmokeRunnerError(f"{action} did not prove background delivery")
        driver = str(_direct_first_scalar(widget, "driver") or "")
        if driver not in _DIRECT_SAFE_BACKGROUND_DRIVERS:
            raise SmokeRunnerError(f"{action} used an unverified background driver")
        for key, expected in (
            ("uses_physical_input", False),
            ("requires_foreground", False),
            ("can_parallel_user_work", True),
        ):
            if _direct_first_scalar(widget, key) is not expected:
                raise SmokeRunnerError(f"{action} did not prove {key}={str(expected).lower()}")
        haze = _direct_edge_haze(widget)
        if haze.get("attempted") is not True or haze.get("started") is not True:
            raise SmokeRunnerError(f"{action} did not prove an active edge haze")
        evidence.update(
            {
                "background": True,
                "driver_class": driver,
                "uses_physical_input": False,
                "requires_foreground": False,
                "can_parallel_user_work": True,
            }
        )
        evidence["edge_haze"] = haze
    elif action == "computer.screenshot":
        artifact_count = len(_direct_artifact_paths(widget))
        if artifact_count < 1:
            raise DirectArtifactCopyError(
                "SCREENSHOT_COPY_SOURCE_MISSING",
                artifact_count=0,
                source_regular=False,
                source_nonempty=False,
                source_symlink=False,
                source_type_allowed=False,
                source_size_allowed=False,
                source_fresh=False,
                trusted_root_match=False,
                copy_attempted=False,
                copy_succeeded=False,
            )
        evidence["artifact_count"] = artifact_count
    return evidence


def _direct_artifact_paths(widget: dict[str, Any]) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for record in _walk_records(widget):
        for key in ("screenshot_path", "model_image_path"):
            raw = record.get(key)
            if not isinstance(raw, str) or not raw.strip() or raw in seen:
                continue
            seen.add(raw)
            found.append((key, Path(raw).expanduser()))
    return found


def copy_direct_screenshot_artifacts(
    widget: dict[str, Any],
    destination_root: Path,
    *,
    source_root: Path,
    step_index: int,
    replay_started_at: float,
) -> list[str]:
    artifacts = _direct_artifact_paths(widget)

    def artifact_error(error_code: str, **facts: bool) -> DirectArtifactCopyError:
        report_facts = {key: False for key in _DIRECT_ARTIFACT_REPORT_BOOL_FIELDS}
        report_facts.update(facts)
        return DirectArtifactCopyError(
            error_code,
            artifact_count=len(artifacts),
            **report_facts,
        )

    if not artifacts:
        raise artifact_error("SCREENSHOT_COPY_SOURCE_MISSING")

    try:
        destination = destination_root.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        destination.chmod(0o700)
    except OSError:
        raise artifact_error("SCREENSHOT_COPY_IO_FAILED") from None
    try:
        exact_source_root = source_root.resolve(strict=True)
    except OSError:
        raise artifact_error("SCREENSHOT_COPY_IO_FAILED") from None
    if not exact_source_root.is_dir():
        raise artifact_error("SCREENSHOT_COPY_IO_FAILED")

    copied: list[str] = []
    for key, raw_source in artifacts:
        if raw_source.is_symlink():
            raise artifact_error(
                "SCREENSHOT_COPY_SYMLINK_REJECTED",
                source_symlink=True,
            )
        try:
            source = raw_source.resolve(strict=True)
        except FileNotFoundError:
            raise artifact_error(
                "SCREENSHOT_COPY_SOURCE_MISSING",
            ) from None
        except OSError:
            raise artifact_error("SCREENSHOT_COPY_IO_FAILED") from None
        if not source.is_file():
            raise artifact_error(
                "SCREENSHOT_COPY_NOT_REGULAR",
            )
        if not source.is_relative_to(exact_source_root):
            raise artifact_error(
                "SCREENSHOT_COPY_OUTSIDE_TRUSTED_ROOT",
                source_regular=True,
            )
        try:
            stat = source.stat()
        except OSError:
            raise artifact_error(
                "SCREENSHOT_COPY_IO_FAILED",
                source_regular=True,
                trusted_root_match=True,
            ) from None
        if stat.st_size == 0:
            raise artifact_error(
                "SCREENSHOT_COPY_EMPTY",
                source_regular=True,
                trusted_root_match=True,
            )
        if source.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise artifact_error(
                "SCREENSHOT_COPY_TYPE_REJECTED",
                source_regular=True,
                source_nonempty=True,
                trusted_root_match=True,
            )
        if stat.st_size > 50 * 1024 * 1024:
            raise artifact_error(
                "SCREENSHOT_COPY_TOO_LARGE",
                source_regular=True,
                source_nonempty=True,
                source_type_allowed=True,
                trusted_root_match=True,
            )
        created_at = float(getattr(stat, "st_birthtime", 0.0) or 0.0)
        freshest_timestamp = max(float(stat.st_mtime), created_at)
        if freshest_timestamp < float(replay_started_at) - 2.0:
            raise artifact_error(
                "SCREENSHOT_COPY_STALE",
                source_regular=True,
                source_nonempty=True,
                source_type_allowed=True,
                source_size_allowed=True,
                trusted_root_match=True,
            )
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", source.name)
        target = destination / f"{step_index:02d}-{key}-{safe_name}"
        try:
            shutil.copy2(source, target)
            target.chmod(0o600)
        except OSError:
            raise artifact_error(
                "SCREENSHOT_COPY_IO_FAILED",
                source_regular=True,
                source_nonempty=True,
                source_type_allowed=True,
                source_size_allowed=True,
                source_fresh=True,
                trusted_root_match=True,
                copy_attempted=True,
            ) from None
        copied.append(target.name)
    if not copied:
        raise artifact_error("SCREENSHOT_COPY_SOURCE_MISSING")
    return copied


def _read_host_audit_since(path: Path, offset: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        stream.seek(max(0, offset))
        for line in stream:
            try:
                value = json.loads(line)
            except Exception:
                continue
            if isinstance(value, dict):
                entries.append(value)
    return entries


def _validated_direct_host_audit(
    entries: list[dict[str, Any]],
    expected_actions: list[str],
    *,
    tolerated_result_failures: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    remaining: dict[str, int] = {}
    for action in expected_actions:
        remaining[action] = remaining.get(action, 0) + 1
    tolerated = dict(tolerated_result_failures or {})
    evidence: list[dict[str, Any]] = []
    for entry in entries:
        action = str(entry.get("function_id") or "")
        safe = {
            key: entry.get(key)
            for key in (
                "ts",
                "function_id",
                "allowed",
                "result_ok",
                "approval_token_present",
                "approval_result",
            )
            if entry.get(key) not in (None, "")
        }
        evidence.append(safe)
        if remaining.get(action, 0) <= 0:
            continue
        approved = (
            entry.get("allowed") is True
            and entry.get("approval_token_present") is True
            and entry.get("approval_result") == "approved"
        )
        if approved and entry.get("result_ok") is True:
            remaining[action] -= 1
        elif approved and tolerated.get(action, 0) > 0:
            remaining[action] -= 1
            tolerated[action] -= 1
    missing = [f"{action} x{count}" for action, count in remaining.items() if count > 0]
    if missing:
        raise SmokeRunnerError("host audit is missing approved successful actions: " + ", ".join(missing))
    return evidence


def _direct_approved_widget(
    client: DebugHttpClient,
    reporter: SmokeReporter,
    action: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool, float]:
    """Refuse the removed harness-owned approval path.

    Mutating debug actions must now be approved individually by an active,
    Launcher-bound ``tobkiri debug`` session.  Keeping that decision outside
    the smoke harness prevents an unattended test from impersonating a user.
    """

    del client, reporter, payload
    raise SmokeRunnerError(
        "automatic smoke approval is disabled; approve the pending "
        f"{action} request with `tobkiri debug approvals approve "
        "--expected-digest <digest> <request-id>`"
    )


def _direct_unapproved_read_widget(
    client: DebugHttpClient,
    action: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Run an allowlisted low-risk read/selection action with no approval token."""

    if action not in {
        "computer.context",
        "computer.select_window",
        "computer.probe_text_control",
    }:
        raise SmokeRunnerError("direct low-risk helper received an unsupported action")
    response = client.post(
        "/api/tools/browser-computer",
        {"action": action, "payload": dict(payload)},
    )
    widget = _direct_widget(response)
    if any(
        bool(record.get("approval_required") or record.get("requires_approval"))
        for record in _walk_records(widget)
    ):
        raise SmokeRunnerError(f"unexpected approval request for low-risk action {action}")
    return widget


def _direct_context_frontmost_identity(widget: dict[str, Any]) -> str:
    for record in _walk_records(widget):
        active = record.get("active_window")
        if not isinstance(active, dict):
            continue
        app = str(active.get("app") or active.get("application") or "").strip()
        if app:
            return re.sub(r"[^a-z0-9]+", "", app.casefold())
    raise SmokeRunnerError("computer.context did not identify the frontmost application")


def _direct_context_sentinel(
    client: DebugHttpClient,
) -> str:
    action = "computer.context"
    widget = _direct_unapproved_read_widget(
        client, action, {"include_windows": False}
    )
    if _direct_action_is_error(action, widget):
        raise SmokeRunnerError("computer.context frontmost sentinel failed")
    identity = _direct_context_frontmost_identity(widget)
    if "atlas" in identity:
        raise SmokeRunnerError("ChatGPT Atlas became frontmost during the background test")
    return identity


def direct_computer_use_sequence(
    client: DebugHttpClient,
    *,
    run_dir: Path,
    viewer_user_data_root: Path,
    direct_artifact_root: Path,
    reporter: SmokeReporter,
    run_nonce: str,
    probe_only: bool = False,
) -> dict[str, Any]:
    """Run the provider-free probe, then optionally the approval/replay plan."""

    initial_frontmost = _direct_context_sentinel(client)
    system_frontmost = frontmost_application_name()
    if _frontmost_is_atlas(system_frontmost):
        raise SmokeRunnerError("ChatGPT Atlas must already be in the background before the direct test")
    selection_payload = {
        "app": DIRECT_ATLAS_APP,
        "focus": False,
        "require_exact_binding": True,
    }
    try:
        try:
            select_widget = _direct_unapproved_read_widget(
                client,
                "computer.select_window",
                selection_payload,
            )
        except DebugApiError as error:
            contract_error = _direct_selection_api_failure(error)
            background_unchanged = _direct_context_sentinel(client) == initial_frontmost
            final_facts = {
                "selection_observation_count": 1,
                "selection_reobservation_eligible": False,
                "selection_reobservation_attempted": False,
                "selection_reobservation_recovered": False,
                "selection_reobservation_outcome": "not_eligible",
                "selection_permission_fact_stability": "unknown",
                "selection_permission_fact_change_count": 0,
                "selection_visibility_fact_stability": "unknown",
                "selection_visibility_fact_change_count": 0,
            }
            _emit_direct_selection_observation(
                reporter, {}, observation_index=1, final_facts=final_facts
            )
            raise DirectSelectionContractError(
                contract_error.error_code
                if background_unchanged
                else "SELECT_WINDOW_BACKGROUND_INVARIANT_FAILED",
                failure_stage=contract_error.failure_stage
                if background_unchanged
                else "frontmost_validation",
                facts=final_facts,
            ) from None
        first_facts, first_permission_failure = _direct_permission_diagnostic_contract(
            _direct_selection_facts(select_widget),
            selected=select_widget.get("selected") is True,
        )
        if first_permission_failure is not None:
            atlas_binding = None
            first_error: DirectSelectionContractError | None = DirectSelectionContractError(
                first_permission_failure[0],
                failure_stage=first_permission_failure[1],
                facts=first_facts,
            )
        else:
            try:
                atlas_binding = _direct_select_contract(select_widget)
                first_error = None
            except DirectSelectionContractError as error:
                atlas_binding = None
                first_error = DirectSelectionContractError(
                    error.error_code,
                    failure_stage=error.failure_stage,
                    facts=first_facts,
                )
        selected_frontmost = _direct_context_sentinel(client)
        background_unchanged = selected_frontmost == initial_frontmost
        if not background_unchanged:
            final_facts = {
                **first_facts,
                "selection_observation_count": 1,
                "selection_reobservation_eligible": False,
                "selection_reobservation_attempted": False,
                "selection_reobservation_recovered": False,
                "selection_reobservation_outcome": "not_eligible",
                "selection_permission_fact_stability": "unknown",
                "selection_permission_fact_change_count": 0,
                "selection_visibility_fact_stability": "unknown",
                "selection_visibility_fact_change_count": 0,
            }
            _emit_direct_selection_observation(
                reporter, select_widget, observation_index=1, final_facts=final_facts
            )
            raise DirectSelectionContractError(
                "SELECT_WINDOW_BACKGROUND_INVARIANT_FAILED",
                failure_stage="frontmost_validation",
                facts=final_facts,
            )
        if first_error is None:
            final_facts = {
                "selection_observation_count": 1,
                "selection_reobservation_eligible": False,
                "selection_reobservation_attempted": False,
                "selection_reobservation_recovered": False,
                "selection_reobservation_outcome": "not_needed",
                "selection_permission_fact_stability": "unknown",
                "selection_permission_fact_change_count": 0,
                "selection_visibility_fact_stability": "unknown",
                "selection_visibility_fact_change_count": 0,
            }
            _emit_direct_selection_observation(
                reporter, select_widget, observation_index=1, final_facts=final_facts
            )
        else:
            eligible = _selection_reobservation_eligible(
                first_error,
                first_facts,
                background_unchanged=True,
            )
            if not eligible:
                outcome = (
                    "instrumentation_inconsistent"
                    if (
                        first_facts.get("selection_inventory_instrumentation_consistent") is False
                        or first_facts.get("selection_permission_diagnostic_outcome")
                        == "instrumentation_inconsistent"
                    )
                    else "not_eligible"
                )
                final_facts = {
                    **first_facts,
                    "selection_observation_count": 1,
                    "selection_reobservation_eligible": False,
                    "selection_reobservation_attempted": False,
                    "selection_reobservation_recovered": False,
                    "selection_reobservation_outcome": outcome,
                    "selection_permission_fact_stability": "unknown",
                    "selection_permission_fact_change_count": 0,
                    "selection_visibility_fact_stability": "unknown",
                    "selection_visibility_fact_change_count": 0,
                }
                _emit_direct_selection_observation(
                    reporter, select_widget, observation_index=1, final_facts=final_facts
                )
                raise DirectSelectionContractError(
                    first_error.error_code,
                    failure_stage=first_error.failure_stage,
                    facts=final_facts,
                )
            _emit_direct_selection_observation(
                reporter, select_widget, observation_index=1
            )
            time.sleep(0.1)
            try:
                second_widget = _direct_unapproved_read_widget(
                    client,
                    "computer.select_window",
                    selection_payload,
                )
            except DebugApiError as error:
                second_error = _direct_selection_api_failure(error)
                background_unchanged = _direct_context_sentinel(client) == initial_frontmost
                final_facts = {
                    "selection_observation_count": 2,
                    "selection_reobservation_eligible": True,
                    "selection_reobservation_attempted": True,
                    "selection_reobservation_recovered": False,
                    "selection_reobservation_outcome": "not_recovered",
                    "selection_permission_fact_stability": "unknown",
                    "selection_permission_fact_change_count": 0,
                    "selection_visibility_fact_stability": "unknown",
                    "selection_visibility_fact_change_count": 0,
                }
                _emit_direct_selection_observation(
                    reporter, {}, observation_index=2, final_facts=final_facts
                )
                raise DirectSelectionContractError(
                    second_error.error_code
                    if background_unchanged
                    else "SELECT_WINDOW_BACKGROUND_INVARIANT_FAILED",
                    failure_stage=second_error.failure_stage
                    if background_unchanged
                    else "frontmost_validation",
                    facts=final_facts,
                ) from None
            second_facts = _direct_selection_facts(second_widget)
            second_facts.update(_direct_permission_fact_stability(first_facts, second_facts))
            second_facts.update(_direct_visibility_fact_stability(first_facts, second_facts))
            second_facts, second_permission_failure = _direct_permission_diagnostic_contract(
                second_facts,
                selected=second_widget.get("selected") is True,
            )
            if second_permission_failure is not None:
                second_binding = None
                second_error: DirectSelectionContractError | None = DirectSelectionContractError(
                    second_permission_failure[0],
                    failure_stage=second_permission_failure[1],
                    facts=second_facts,
                )
            else:
                try:
                    second_binding = _direct_select_contract(second_widget)
                    second_error = None
                except DirectSelectionContractError as error:
                    second_binding = None
                    second_error = DirectSelectionContractError(
                        error.error_code,
                        failure_stage=error.failure_stage,
                        facts=second_facts,
                    )
            second_frontmost = _direct_context_sentinel(client)
            if second_frontmost != initial_frontmost:
                final_facts = {
                    **second_facts,
                    "selection_observation_count": 2,
                    "selection_reobservation_eligible": True,
                    "selection_reobservation_attempted": True,
                    "selection_reobservation_recovered": False,
                    "selection_reobservation_outcome": "not_recovered",
                }
                _emit_direct_selection_observation(
                    reporter, second_widget, observation_index=2, final_facts=final_facts
                )
                raise DirectSelectionContractError(
                    "SELECT_WINDOW_BACKGROUND_INVARIANT_FAILED",
                    failure_stage="frontmost_validation",
                    facts=final_facts,
                )
            if second_error is not None:
                outcome = (
                    "instrumentation_inconsistent"
                    if (
                        second_facts.get("selection_inventory_instrumentation_consistent") is False
                        or second_facts.get("selection_permission_diagnostic_outcome")
                        == "instrumentation_inconsistent"
                    )
                    else "not_recovered"
                )
                final_facts = {
                    **second_facts,
                    "selection_observation_count": 2,
                    "selection_reobservation_eligible": True,
                    "selection_reobservation_attempted": True,
                    "selection_reobservation_recovered": False,
                    "selection_reobservation_outcome": outcome,
                }
                _emit_direct_selection_observation(
                    reporter, second_widget, observation_index=2, final_facts=final_facts
                )
                raise DirectSelectionContractError(
                    second_error.error_code,
                    failure_stage=second_error.failure_stage,
                    facts=final_facts,
                )
            atlas_binding = second_binding
            final_facts = {
                **second_facts,
                "selection_observation_count": 2,
                "selection_reobservation_eligible": True,
                "selection_reobservation_attempted": True,
                "selection_reobservation_recovered": True,
                "selection_reobservation_outcome": "recovered",
            }
            _emit_direct_selection_observation(
                reporter, second_widget, observation_index=2, final_facts=final_facts
            )
        assert atlas_binding is not None
    except DirectSelectionContractError as error:
        _emit_direct_selection_failure(reporter, error)
        raise
    probe_payload = {
        "app": DIRECT_ATLAS_APP,
        "window": dict(atlas_binding),
        "target_control": "browser_address",
        "background": True,
        "focus": False,
        "include_screenshot": False,
    }
    try:
        before_probe = _direct_context_sentinel(client)
        try:
            probe_widget = _direct_unapproved_read_widget(
                client,
                "computer.probe_text_control",
                probe_payload,
            )
        except DebugApiError:
            raise DirectProbeContractError(
                "PROBE_TRANSPORT_CONTRACT_INVALID",
            ) from None
        after_probe = _direct_context_sentinel(client)
        context_unchanged = before_probe == after_probe
        try:
            probe_facts = _direct_probe_contract(probe_widget)
        except DirectProbeContractError as error:
            if _direct_native_frontmost_failed(error.facts):
                raise DirectProbeContractError(
                    "PROBE_BACKGROUND_INVARIANT_FAILED",
                    failure_stage="frontmost_validation",
                    facts={
                        **error.facts,
                        "context_frontmost_check_completed": True,
                        "context_target_non_frontmost_before": True,
                        "context_target_non_frontmost_after": True,
                        "context_frontmost_unchanged": context_unchanged,
                    },
                ) from None
            error.facts.update(
                _direct_probe_facts(
                    {
                        "context_frontmost_check_completed": True,
                        "context_target_non_frontmost_before": True,
                        "context_target_non_frontmost_after": True,
                        "context_frontmost_unchanged": context_unchanged,
                    },
                    require_action=False,
                )
            )
            raise
        if _direct_native_frontmost_failed(probe_facts):
            raise DirectProbeContractError(
                "PROBE_BACKGROUND_INVARIANT_FAILED",
                failure_stage="frontmost_validation",
                facts=probe_facts,
            )
        if not context_unchanged:
            raise DirectProbeContractError(
                "PROBE_FRONTMOST_SENTINEL_UNSTABLE",
                failure_stage="frontmost_validation",
                facts={
                    **probe_facts,
                    "context_frontmost_check_completed": True,
                    "context_target_non_frontmost_before": True,
                    "context_target_non_frontmost_after": True,
                    "context_frontmost_unchanged": False,
                },
            )
    except DirectProbeContractError as error:
        _emit_direct_probe_failure(reporter, error)
        raise
    reporter.emit(
        "direct_probe_completed",
        ok=True,
        frontmost_non_atlas=True,
        frontmost_unchanged=True,
        **probe_facts,
    )
    if probe_only:
        final_frontmost = _direct_context_sentinel(client)
        if final_frontmost != initial_frontmost:
            raise SmokeRunnerError("frontmost application did not remain unchanged for the direct probe")
        summary = {
            "ok": True,
            "provider_used": False,
            "chat_used": False,
            "model_used": False,
            "probe_only": True,
            "probe_completed": True,
            "semantic_control_ready": True,
            "frontmost_non_atlas": True,
            "frontmost_unchanged": True,
            "screenshot_evidence_captured": False,
            "effect_verified": False,
            "visual_inspection_required": False,
            "steps": [],
            "host_audit_present": False,
            "host_audit": [],
        }
        reporter.emit("direct_summary", **summary)
        return summary
    plan = direct_background_action_plan(run_nonce)
    host_audit_path = viewer_user_data_root / "host_broker" / "audit.jsonl"
    audit_offset = host_audit_path.stat().st_size if host_audit_path.exists() else 0
    screenshot_root = run_dir / "evidence" / "screenshots"
    approved_actions: list[str] = []
    steps: list[dict[str, Any]] = []
    tolerated_type_failures = 0
    for index, step in enumerate(plan, start=1):
        action = str(step["action"])
        payload = dict(step["payload"])
        if action == "computer.screenshot" or (
            action == "computer.type" and payload.get("target_control") == "browser_address"
        ):
            payload["window"] = dict(atlas_binding)
        _validate_direct_action(action, payload)
        before = _direct_context_sentinel(client)
        widget, approval_approved, replay_started_at = _direct_approved_widget(
            client, reporter, action, payload
        )
        approved_actions.append(action)
        after = _direct_context_sentinel(client)
        if before != after:
            raise SmokeRunnerError(f"background invariant failed for {action}")
        evidence = _direct_result_evidence(
            action,
            widget,
            approval_approved=approval_approved,
            frontmost_non_atlas=True,
            frontmost_unchanged=True,
        )
        if evidence.get("classification") == "DELIVERY_UNVERIFIED":
            tolerated_type_failures += 1
        copied = (
            copy_direct_screenshot_artifacts(
                widget,
                screenshot_root,
                source_root=direct_artifact_root,
                step_index=index,
                replay_started_at=replay_started_at,
            )
            if action == "computer.screenshot"
            else []
        )
        step_result = {
            "index": index,
            "label": str(step["label"]),
            "action": action,
            "frontmost_non_atlas_before": True,
            "frontmost_non_atlas_after": True,
            "frontmost_unchanged": True,
            "background_required": action in _DIRECT_BACKGROUND_ACTIONS,
            "result_evidence": evidence,
            "copied_artifact_count": len(copied),
        }
        reporter.emit("direct_action_completed", **step_result)
        steps.append(step_result)
        wait_after = float(step.get("wait_after") or 0.0)
        if wait_after > 0:
            time.sleep(wait_after)
    final_frontmost = _direct_context_sentinel(client)
    if final_frontmost != initial_frontmost:
        raise SmokeRunnerError("frontmost application did not remain unchanged for the direct test")
    audit = _validated_direct_host_audit(
        _read_host_audit_since(host_audit_path, audit_offset),
        approved_actions,
        tolerated_result_failures={"computer.type": tolerated_type_failures},
    )
    summary = {
        "ok": True,
        "provider_used": False,
        "chat_used": False,
        "model_used": False,
        "probe_completed": True,
        "semantic_control_ready": True,
        "frontmost_non_atlas": True,
        "frontmost_unchanged": True,
        "screenshot_evidence_captured": True,
        "effect_verified": False,
        "visual_inspection_required": True,
        "steps": steps,
        "host_audit_present": host_audit_path.exists(),
        "host_audit": audit,
    }
    reporter.emit("direct_summary", **summary)
    return summary


def _record_request_id(record: dict[str, Any]) -> str:
    return str(
        record.get("approval_request_id")
        or record.get("request_id")
        or record.get("requestId")
        or ""
    ).strip()


def _is_authority_record(record: dict[str, Any]) -> bool:
    permission_id = str(record.get("permission_id") or "")
    return bool(
        record.get("authority")
        or record.get("approval_kind") in {"authority", "host_intent", "critical_host_function"}
        or permission_id in _SMOKE_PROVIDER_PERMISSIONS
        or permission_id.startswith(("host.", "authority."))
    )


def _turn_request_ids(events: list[dict[str, Any]], *, authority: bool) -> set[str]:
    request_ids: set[str] = set()
    for event in events:
        for record in _walk_records(event):
            request_id = _record_request_id(record)
            if request_id and _is_authority_record(record) is authority:
                request_ids.add(request_id)
    return request_ids


def _pending_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("pending") if isinstance(data.get("pending"), list) else data.get("requests")
    return [item for item in raw or [] if isinstance(item, dict) and item.get("status") == "pending"]


def _request_belongs_to_chat(
    request: dict[str, Any],
    conversation_id: str,
    turn_request_ids: set[str],
) -> bool:
    details = request.get("details") if isinstance(request.get("details"), dict) else {}
    request_conversation_id = str(
        request.get("conversation_id") or details.get("conversation_id") or ""
    ).strip()
    if request_conversation_id:
        return request_conversation_id == conversation_id
    return _record_request_id(request) in turn_request_ids


def _authority_config(request: dict[str, Any]) -> dict[str, Any]:
    resource = request.get("resource") if isinstance(request.get("resource"), dict) else {}
    config: dict[str, Any] = {}
    for source, target in (
        ("provider_id", "provider_ids"),
        ("api_id", "api_ids"),
        ("model_id", "model_ids"),
        ("function_id", "function_ids"),
        ("pack_id", "pack_ids"),
        ("domain", "domains"),
        ("caller_pack_id", "caller_pack_ids"),
        ("caller_function_id", "caller_function_ids"),
    ):
        value = str(resource.get(source) or "").strip()
        if value:
            config[target] = [value]
    host_actions = list(
        dict.fromkeys(
            str(resource.get(key) or "").strip()
            for key in ("host_action", "operation")
            if str(resource.get(key) or "").strip()
        )
    )
    if host_actions:
        config["host_actions"] = host_actions
    if resource.get("port") is not None:
        config["ports"] = [resource["port"]]
    if resource.get("stream") is True or resource.get("stream_enabled") is True:
        config["allow_stream"] = True
    if isinstance(resource.get("input_tokens"), (int, float)):
        config["max_input_tokens"] = resource["input_tokens"]
    return config


def _authority_related_permissions(request: dict[str, Any]) -> list[str]:
    resource = request.get("resource") if isinstance(request.get("resource"), dict) else {}
    permission_id = str(request.get("permission_id") or "")
    provider_id = str(resource.get("provider_id") or "").strip()
    has_provider_model = bool(
        provider_id and str(resource.get("model_id") or resource.get("model_ref") or "").strip()
    )
    has_endpoint = bool(str(resource.get("endpoint_url") or resource.get("domain") or "").strip())
    permissions: list[str] = []
    if permission_id != "model.invoke" and has_provider_model:
        permissions.append("model.invoke")
    if permission_id != "api_key.use" and provider_id:
        permissions.append("api_key.use")
    if permission_id != "network.egress" and (has_endpoint or provider_id):
        permissions.append("network.egress")
    return permissions


def _authority_runtime_content(request: dict[str, Any], token: str) -> str:
    request_id = str(request.get("request_id") or "")
    permission_id = str(request.get("permission_id") or "")
    resource = request.get("resource") if isinstance(request.get("resource"), dict) else {}
    payload = {
        "request_id": request_id,
        "permission_id": permission_id,
        "resource": resource,
        **({"approval_token": token} if token else {}),
    }
    retry_label = "host operation" if permission_id.startswith("host.") else "model/API operation"
    return "\n".join(
        [
            "Silent internal resume. Continue the interrupted/original user request without any acknowledgment or preface.",
            f"Retry the same {retry_label} once using the supplied resume metadata.",
            "In the user-visible answer, never mention approval, authority, API keys, providers, model access, permission, or token details, and do not thank the user for permission.",
            "Do not ask the user for the same permission again unless a new request id is produced.",
            f"Request id: {request_id}",
            f"Permission id: {permission_id}",
            "Resume metadata JSON:",
            json.dumps(payload, ensure_ascii=False, indent=2),
        ]
    )


def _runtime_approval_candidate(
    events: list[dict[str, Any]],
    request: dict[str, Any],
) -> dict[str, Any]:
    request_id = _record_request_id(request)
    candidate = next(
        (
            record
            for event in reversed(events)
            for record in _walk_records(event)
            if _record_request_id(record) == request_id and not _is_authority_record(record)
        ),
        {},
    )
    details = request.get("details") if isinstance(request.get("details"), dict) else {}
    payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else None
    if payload is None and isinstance(candidate.get("arguments"), dict):
        payload = candidate["arguments"]
    if payload is None and isinstance(details.get("payload"), dict):
        payload = details["payload"]
    if payload is None and isinstance(details.get("arguments"), dict):
        payload = details["arguments"]
    tool_name = str(candidate.get("tool_name") or details.get("tool_name") or "").strip()
    operation = str(
        candidate.get("operation")
        or candidate.get("action")
        or request.get("operation")
        or details.get("action")
        or tool_name
    ).strip()
    action = str(candidate.get("action") or details.get("action") or operation).strip()
    return {
        "request_id": request_id,
        "tool_name": tool_name,
        "tool_call_id": str(candidate.get("tool_call_id") or details.get("tool_call_id") or "").strip(),
        "operation": operation,
        "action": action,
        "payload": dict(payload or {}),
    }


def _runtime_approval_content(candidate: dict[str, Any], token: str) -> str:
    arguments = {**candidate["payload"], **({"approval_token": token} if token else {})}
    return "\n".join(
        [
            "The delegated debug CLI approved the pending server-side tool operation.",
            "Continue by calling the exact pending tool once with the approved arguments below.",
            "Do not ask the user for the same approval again unless the tool returns a new approval_request_id.",
            f"Tool: {candidate['tool_name']}",
            f"Operation: {candidate['operation']}",
            f"Approval request id: {candidate['request_id']}",
            "Approved arguments JSON:",
            json.dumps(arguments, ensure_ascii=False, indent=2),
        ]
    )


def _finish_reason(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        if event.get("type") not in {"message", "done"}:
            continue
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        value = str(message.get("finish_reason") or "").strip()
        if value:
            return value
    return ""


def _transient_ai_error_class(events: list[dict[str, Any]]) -> str | None:
    """Classify only bounded, safe provider failures from the final stream state."""
    final = next(
        (
            event
            for event in reversed(events)
            if event.get("type") in {"message", "done"}
        ),
        {},
    )
    message = final.get("message") if isinstance(final.get("message"), dict) else {}
    event_metadata = final.get("metadata") if isinstance(final.get("metadata"), dict) else {}
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    marked_transient = any(
        value is True or str(value).lower() == "true"
        for value in (
            final.get("transient_ai_error"),
            event_metadata.get("transient_ai_error"),
            message.get("transient_ai_error"),
            metadata.get("transient_ai_error"),
        )
    )
    text = " ".join(
        str(value)
        for value in (
            final.get("error"),
            event_metadata.get("error"),
            event_metadata.get("sanitized_error"),
            message.get("error"),
            metadata.get("error"),
            metadata.get("sanitized_error"),
            metadata.get("error_message"),
        )
        if value not in (None, "")
    ).lower()
    if re.search(r"\b(auth|authentication|unauthori[sz]ed|forbidden|api.?key|invalid.?key)\b", text):
        return None
    if re.search(r"\b(wrong format|malformed|invalid (?:request|response|json|schema))\b", text):
        return None
    if re.search(r"(?:err(?:no)?\s*60|operation timed out|timed?\s*out|timeout)", text):
        return "timeout"
    if re.search(r"\b(queue(?:d| full| timeout)?|overloaded|capacity)\b", text):
        return "queue"
    if re.search(r"\b(temporary|temporarily|transient|try again|unavailable|connection (?:reset|closed|lost))\b", text):
        return "temporary_provider"
    return "marked_transient" if marked_transient else None


def _transient_resume_request() -> dict[str, Any]:
    return _message_request(
        "Continue the original task from the current visually verified state. Inspect the current state before acting, and do not repeat any completed action.",
        tools=[SMOKE_TOOL],
        params=_smoke_tool_params(),
    )


class ComputerUseSmokeRunner:
    def __init__(
        self,
        client: DebugHttpClient,
        artifact: dict[str, Any],
        *,
        prompt: str,
        max_turns: int,
        reporter: SmokeReporter,
        max_transient_resumes: int = DEFAULT_MAX_TRANSIENT_RESUMES,
        min_stream_interval_seconds: float = DEFAULT_SMOKE_MIN_STREAM_INTERVAL_SECONDS,
        provider_preflight: Mapping[str, Any] | None = None,
        monotonic: Any = time.monotonic,
        sleep: Any = time.sleep,
    ) -> None:
        self.client = client
        self.artifact = artifact
        self.prompt = prompt
        self.max_turns = max_turns
        self.reporter = reporter
        self.max_transient_resumes = max_transient_resumes
        self.min_stream_interval_seconds = min_stream_interval_seconds
        self.provider_preflight = dict(provider_preflight or {})
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_stream_started_at: float | None = None
        self.conversation_id = ""
        self.turns = 0

    def _pace_stream(self, turn: int) -> None:
        """Rate-limit actual model streams; approval-only API work is not a call."""
        now = self._monotonic()
        waited = 0.0
        if self._last_stream_started_at is not None:
            remaining = self.min_stream_interval_seconds - (now - self._last_stream_started_at)
            if remaining > 0:
                waited = remaining
                self.reporter.emit(
                    "stream_pacing_wait",
                    turn=turn,
                    model=SMOKE_MODEL,
                    wait_seconds=round(remaining, 3),
                    min_interval_seconds=self.min_stream_interval_seconds,
                )
                self._sleep(remaining)
                now = self._monotonic()
        self._last_stream_started_at = now
        self.reporter.emit(
            "stream_pacing_ready",
            turn=turn,
            model=SMOKE_MODEL,
            waited=waited > 0,
            min_interval_seconds=self.min_stream_interval_seconds,
        )

    def _preflight(self) -> None:
        self.client.get("/api/health")
        desktop = self.client.get("/api/desktop-system-info")
        broker = desktop.get("host_broker") if isinstance(desktop.get("host_broker"), dict) else {}
        if not (
            desktop.get("source") == "viewer_broker"
            and desktop.get("reliable") is True
            and broker.get("available") is True
        ):
            raise SmokeRunnerError(
                "Viewer broker preflight failed: desktop-system-info is not reliable/available"
            )
        self.reporter.emit(
            "preflight",
            source=desktop.get("source"),
            reliable=desktop.get("reliable"),
            broker_available=broker.get("available"),
        )
        self._provider_preflight()

    def _provider_preflight(self) -> None:
        """Fail before a model turn if the isolated provider is unavailable.

        These GET requests only inspect local runtime registration/catalogue.
        They do not create or consume Authority approvals; the normal
        Authority path still protects the real provider invocation.
        """

        supplied = self.provider_preflight
        credential_present = supplied.get("credential_present")
        if supplied:
            expected = {
                "provider_id": _TRUSTED_SMOKE_PROVIDER_ID,
                "model": SMOKE_MODEL,
                "credential_source": _TRUSTED_SMOKE_CREDENTIAL_SOURCE,
                "credential_persisted": False,
                "allow_custom_base_url": False,
            }
            if any(supplied.get(key) != value for key, value in expected.items()):
                raise SmokeRunnerError("PROVIDER_PREFLIGHT_INVALID")
            if credential_present is not True:
                raise SmokeRunnerError("PROVIDER_ENV_NOT_PRESENT")

        provider_response = self.client.get("/api/ai/providers")
        providers = provider_response.get("providers")
        if not isinstance(providers, list):
            raise SmokeRunnerError("PROVIDER_PREFLIGHT_INVALID")
        provider = next(
            (
                item
                for item in providers
                if isinstance(item, dict)
                and str(item.get("provider_id") or item.get("id") or "")
                == _TRUSTED_SMOKE_PROVIDER_ID
            ),
            None,
        )
        registered = bool(isinstance(provider, dict) and provider.get("registered") is True)
        if not registered:
            self.reporter.emit(
                "provider_preflight",
                provider_id=_TRUSTED_SMOKE_PROVIDER_ID,
                model=SMOKE_MODEL,
                credential_present=credential_present is True,
                credential_source=_TRUSTED_SMOKE_CREDENTIAL_SOURCE if supplied else "unknown",
                registered=False,
                model_available=False,
                credential_persisted=False,
            )
            raise SmokeRunnerError("PROVIDER_NOT_REGISTERED")

        model_response = self.client.get("/api/ai/models", query={"provider": _TRUSTED_SMOKE_PROVIDER_ID})
        models = model_response.get("models")
        if not isinstance(models, list):
            raise SmokeRunnerError("PROVIDER_PREFLIGHT_INVALID")
        model_available = any(
            isinstance(item, dict)
            and str(
                item.get("qualified_model_id")
                or item.get("id")
                or item.get("model_ref")
                or ""
            )
            == SMOKE_MODEL
            for item in models
        )
        self.reporter.emit(
            "provider_preflight",
            provider_id=_TRUSTED_SMOKE_PROVIDER_ID,
            model=SMOKE_MODEL,
            credential_present=credential_present is True,
            credential_source=_TRUSTED_SMOKE_CREDENTIAL_SOURCE if supplied else "unknown",
            registered=True,
            model_available=model_available,
            credential_persisted=False,
        )
        if not model_available:
            raise SmokeRunnerError("PROVIDER_MODEL_UNAVAILABLE")

    def _create_conversation(self) -> str:
        conversation = self.client.post(
            "/api/chat/conversations",
            {
                "model": SMOKE_MODEL,
                "tags": ["issue-555", "smoke-computer-use"],
                "metadata": {
                    "debug_harness": "defaultspack_debug",
                    "smoke": "computer_use",
                    "issue": 555,
                    "model": SMOKE_MODEL,
                },
            },
        )
        conversation_id = str(conversation.get("id") or "").strip()
        if not conversation_id:
            raise SmokeRunnerError("defaultspack did not return a conversation id")
        self.conversation_id = conversation_id
        self.reporter.emit("chat_created", conversation_id=conversation_id, model=SMOKE_MODEL)
        return conversation_id

    def _pending_authority(self, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        response = self.client.get("/api/authority/requests", query={"status": "pending"})
        own_ids = _turn_request_ids(events, authority=True)
        matches = [
            request
            for request in _pending_items(response)
            if _request_belongs_to_chat(request, self.conversation_id, own_ids)
        ]
        return min(matches, key=lambda item: str(item.get("created_at") or "")) if matches else None

    def _authority_is_allowed(self, request: dict[str, Any]) -> bool:
        permission_id = str(request.get("permission_id") or "")
        if permission_id in _SMOKE_PROVIDER_PERMISSIONS:
            return True
        return permission_id in _SMOKE_HOST_PERMISSIONS

    def _authority_approval_config(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return the production-style constraint config for this smoke run."""

        return _authority_config(request)

    def _authority_approval_related_permissions(
        self, request: dict[str, Any]
    ) -> list[str]:
        """Return related production Authority permissions for this request."""

        return _authority_related_permissions(request)

    def _approve_authority(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = str(request.get("request_id") or "").strip()
        permission_id = str(request.get("permission_id") or "").strip()
        if not request_id or not permission_id:
            raise SmokeRunnerError("pending Authority request is missing its id or permission")
        if not self._authority_is_allowed(request):
            raise SmokeRunnerError(
                f"refusing unexpected Authority permission {permission_id} for smoke chat"
            )
        raise SmokeRunnerError(
            "automatic Authority approval is disabled; approve request "
            f"{request_id} individually with `tobkiri debug approvals approve "
            "--expected-digest <digest> "
            f"{request_id}`"
        )

    def _pending_runtime(self, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        response = self.client.get(
            "/api/coding/approvals",
            query={"status": "pending", "include_expired": "true", "limit": 100},
        )
        own_ids = _turn_request_ids(events, authority=False)
        matches = [
            request
            for request in _pending_items(response)
            if _request_belongs_to_chat(request, self.conversation_id, own_ids)
        ]
        return min(matches, key=lambda item: int(item.get("created_at") or 0)) if matches else None

    def _approve_runtime(
        self,
        request: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        candidate = _runtime_approval_candidate(events, request)
        request_id = candidate["request_id"]
        tool_name = candidate["tool_name"]
        if tool_name not in _SMOKE_RUNTIME_APPROVAL_TOOLS:
            raise SmokeRunnerError(
                f"refusing unexpected runtime approval tool {tool_name or '<missing>'} for smoke chat"
            )
        raise SmokeRunnerError(
            "automatic runtime approval is disabled; approve request "
            f"{request_id} individually with `tobkiri debug approvals approve "
            "--expected-digest <digest> "
            f"{request_id}`"
        )

    def _summary(self, *, ok: bool, turns: int, stop_reason: str) -> dict[str, Any]:
        chat_store = Path(str(self.artifact.get("chat_store") or ""))
        conversation_dir = chat_store.parent / "conversations" / self.conversation_id
        base_url = self.client.base_url
        return {
            "ok": ok,
            "turns": turns,
            "stop_reason": stop_reason,
            "conversation_id": self.conversation_id,
            "chat_url": f"{base_url}/chat?{urllib.parse.urlencode({'chat': self.conversation_id})}",
            "chat_store": str(chat_store),
            "history_path": str(conversation_dir / "history.json"),
            "provider_trace_dir": str(conversation_dir / "workspace" / "provider_traces"),
            "computer_artifact_dir": str(conversation_dir / "workspace" / "tools" / "computer"),
            "run_log": self.artifact.get("log_path"),
            "run_dir": self.artifact.get("run_dir"),
            "launch_artifact": str(LATEST_JSON),
        }

    def failure_summary(self, error: str) -> dict[str, Any]:
        if self.conversation_id:
            return {
                **self._summary(ok=False, turns=self.turns, stop_reason="error"),
                "error": error,
            }
        return {
            "ok": False,
            "turns": self.turns,
            "stop_reason": "error",
            "error": error,
            "run_log": self.artifact.get("log_path"),
            "run_dir": self.artifact.get("run_dir"),
            "launch_artifact": str(LATEST_JSON),
        }

    def run(self) -> dict[str, Any]:
        self._preflight()
        conversation_id = self._create_conversation()
        path = f"/api/chat/conversations/{urllib.parse.quote(conversation_id, safe='')}/stream"
        next_request = _message_request(
            self.prompt,
            tools=[SMOKE_TOOL],
            params=_smoke_tool_params(),
        )
        stop_reason = "max_turns"
        ok = False
        turns = 0
        transient_resumes = 0
        for turn in range(1, self.max_turns + 1):
            turns = turn
            self.turns = turn
            self.reporter.emit("turn_started", turn=turn)
            events: list[dict[str, Any]] = []
            delta_chars = 0
            thinking_chars = 0
            self._pace_stream(turn)
            for event in self.client.stream(path, next_request):
                events.append(event)
                if event.get("type") == "delta":
                    delta_chars += len(str(event.get("delta") or ""))
                elif event.get("type") == "thinking_delta":
                    thinking_chars += len(str(event.get("delta") or ""))
                compact = _compact_stream_event(event, turn)
                if compact is not None:
                    self.reporter.emit("stream", **compact)
                if event.get("type") == "error":
                    raise SmokeRunnerError("defaultspack stream returned an error")
            self.reporter.emit(
                "turn_finished",
                turn=turn,
                delta_chars=delta_chars,
                thinking_chars=thinking_chars,
                finish_reason=_finish_reason(events),
            )
            authority = self._pending_authority(events)
            if authority is not None:
                next_request = self._approve_authority(authority)
                continue
            runtime = self._pending_runtime(events)
            if runtime is not None:
                next_request = self._approve_runtime(runtime, events)
                continue
            finish_reason = _finish_reason(events)
            if finish_reason in {"approval_required", "authority_approval_required"}:
                raise SmokeRunnerError(
                    f"turn ended with {finish_reason}, but no approval belonging to the smoke chat was found"
                )
            if finish_reason in {"paused_progress_loop", "paused_loop", "paused_recoverable"}:
                next_request = _message_request(
                    "Continue the original task from the current visible browser state. Verify the next action before repeating a previous action.",
                    tools=[SMOKE_TOOL],
                    params=_smoke_tool_params(),
                )
                continue
            if finish_reason == "ai_error_after_tool_use":
                reason_class = _transient_ai_error_class(events)
                if reason_class is not None and transient_resumes < self.max_transient_resumes:
                    transient_resumes += 1
                    self.reporter.emit(
                        "transient_ai_recovery",
                        count=transient_resumes,
                        reason_class=reason_class,
                    )
                    next_request = _transient_resume_request()
                    continue
            if not finish_reason:
                raise SmokeRunnerError("defaultspack stream ended without a final message")
            stop_reason = finish_reason
            ok = finish_reason == "stop"
            break
        summary = self._summary(ok=ok, turns=turns, stop_reason=stop_reason)
        self.reporter.emit("smoke_summary", **summary)
        return summary


class ChatOnlySmokeRunner(ComputerUseSmokeRunner):
    """Exercise ordinary chat plus the production Authority resume contract."""

    def __init__(
        self,
        client: DebugHttpClient,
        artifact: dict[str, Any],
        *,
        prompt: str,
        max_turns: int,
        reporter: SmokeReporter,
        provider_preflight: Mapping[str, Any],
    ) -> None:
        super().__init__(
            client,
            artifact,
            prompt=prompt,
            max_turns=max_turns,
            reporter=reporter,
            min_stream_interval_seconds=0,
            provider_preflight=provider_preflight,
        )
        self.profile = _smoke_provider_profile(MIMO_CHAT_PROFILE)

    def _preflight(self) -> None:
        self.client.get("/api/health")
        expected = {
            "provider_id": self.profile["provider_id"],
            "model": self.profile["model"],
            "credential_source": _TRUSTED_SMOKE_CREDENTIAL_SOURCE,
            "credential_persisted": False,
            "allow_custom_base_url": False,
        }
        if any(self.provider_preflight.get(key) != value for key, value in expected.items()):
            raise SmokeRunnerError("PROVIDER_PREFLIGHT_INVALID")
        if self.provider_preflight.get("credential_present") is not True:
            raise SmokeRunnerError("PROVIDER_ENV_NOT_PRESENT")
        providers = self.client.get("/api/ai/providers").get("providers")
        if not isinstance(providers, list) or not any(
            isinstance(item, dict)
            and str(item.get("provider_id") or item.get("id") or "")
            == self.profile["provider_id"]
            and item.get("registered") is True
            for item in providers
        ):
            raise SmokeRunnerError("PROVIDER_NOT_REGISTERED")
        models = self.client.get(
            "/api/ai/models", query={"provider": self.profile["provider_id"]}
        ).get("models")
        if not isinstance(models, list) or not any(
            isinstance(item, dict)
            and str(
                item.get("qualified_model_id")
                or item.get("id")
                or item.get("model_ref")
                or ""
            )
            == self.profile["model"]
            for item in models
        ):
            raise SmokeRunnerError("PROVIDER_MODEL_UNAVAILABLE")
        self.reporter.emit(
            "provider_preflight",
            provider_id=self.profile["provider_id"],
            model=self.profile["model"],
            credential_present=True,
            credential_source=_TRUSTED_SMOKE_CREDENTIAL_SOURCE,
            credential_persisted=False,
            allow_custom_base_url=False,
        )

    def _create_conversation(self) -> str:
        conversation = self.client.post(
            "/api/chat/conversations",
            {
                "model": self.profile["model"],
                "tags": ["debug-smoke", "mimo-chat"],
                "metadata": {
                    "debug_harness": "defaultspack_debug",
                    "smoke": "chat_only",
                    "provider_profile": MIMO_CHAT_PROFILE,
                    "model": self.profile["model"],
                },
            },
        )
        conversation_id = str(conversation.get("id") or "").strip()
        if not conversation_id:
            raise SmokeRunnerError("defaultspack did not return a conversation id")
        self.conversation_id = conversation_id
        self.reporter.emit(
            "chat_created", conversation_id=conversation_id, model=self.profile["model"]
        )
        return conversation_id

    def _authority_is_allowed(self, request: dict[str, Any]) -> bool:
        if str(request.get("permission_id") or "") not in _SMOKE_PROVIDER_PERMISSIONS:
            return False
        resource = request.get("resource") if isinstance(request.get("resource"), dict) else {}
        provider_id = str(resource.get("provider_id") or "")
        model = str(resource.get("model_ref") or resource.get("model_id") or "")
        if provider_id != self.profile["provider_id"] or model not in {
            self.profile["model"],
            self.profile["model"].split("/", 1)[1],
        }:
            return False

        endpoint_url = str(resource.get("endpoint_url") or "").strip()
        try:
            endpoint = urllib.parse.urlsplit(endpoint_url)
            endpoint_port = endpoint.port
        except ValueError:
            return False
        if endpoint_port is None and endpoint.scheme == "https":
            endpoint_port = 443
        request_port = resource.get("port")
        if isinstance(request_port, bool):
            return False
        try:
            request_port = int(request_port)
        except (TypeError, ValueError):
            return False
        endpoint_origin = (
            f"{endpoint.scheme.lower()}://{(endpoint.hostname or '').lower()}"
        )
        if endpoint_port != 443:
            endpoint_origin += f":{endpoint_port}"
        return all(
            (
                str(resource.get("api_id") or "") == self.profile["api_id"],
                endpoint_url == self.profile["endpoint_url"],
                endpoint.username is None,
                endpoint.password is None,
                not endpoint.query,
                not endpoint.fragment,
                endpoint.scheme.lower() == self.profile["transport"],
                endpoint_origin == self.profile["origin"],
                (endpoint.hostname or "").lower() == self.profile["domain"],
                endpoint_port == self.profile["port"],
                endpoint.path == "/zen" + self.profile["endpoint_path"],
                str(resource.get("endpoint_path") or "")
                == self.profile["endpoint_path"],
                str(resource.get("domain") or "").lower()
                == self.profile["domain"],
                request_port == self.profile["port"],
                str(resource.get("transport") or "").lower()
                == self.profile["transport"],
                resource.get("stream") is True,
            )
        )

    def _authority_approval_config(self, request: dict[str, Any]) -> dict[str, Any]:
        """Constrain approval with code-owned Mimo endpoint facts only."""

        del request
        return {
            "provider_ids": [self.profile["provider_id"]],
            "api_ids": [self.profile["api_id"]],
            "model_ids": [self.profile["model"].split("/", 1)[1]],
            "domains": [self.profile["domain"]],
            "ports": [self.profile["port"]],
            "allow_stream": True,
        }

    def _authority_approval_related_permissions(
        self, request: dict[str, Any]
    ) -> list[str]:
        """Return only the fixed provider permission set, never request config."""

        permission_id = str(request.get("permission_id") or "")
        return [
            candidate
            for candidate in ("model.invoke", "api_key.use", "network.egress")
            if candidate != permission_id
        ]

    def run(self) -> dict[str, Any]:
        self._preflight()
        conversation_id = self._create_conversation()
        path = f"/api/chat/conversations/{urllib.parse.quote(conversation_id, safe='')}/stream"
        next_request = _message_request(self.prompt)
        turns = 0
        for turn in range(1, self.max_turns + 1):
            turns = turn
            self.turns = turn
            events: list[dict[str, Any]] = []
            terminal_count = 0
            self.reporter.emit("turn_started", turn=turn)
            for event in self.client.stream(path, next_request):
                events.append(event)
                if event.get("type") in {"done", "error"}:
                    terminal_count += 1
                compact = _compact_stream_event(event, turn)
                if compact is not None:
                    self.reporter.emit("stream", **compact)
            if terminal_count != 1:
                raise SmokeRunnerError("chat stream did not produce exactly one terminal result")
            if events[-1].get("type") == "error":
                raise SmokeRunnerError("defaultspack stream returned an error")
            authority = self._pending_authority(events)
            if authority is not None:
                next_request = self._approve_authority(authority)
                continue
            finish_reason = _finish_reason(events)
            if finish_reason in {"approval_required", "authority_approval_required"}:
                raise SmokeRunnerError(
                    "chat ended for approval, but no matching Authority request was found"
                )
            if not finish_reason:
                raise SmokeRunnerError("defaultspack stream ended without a finish reason")
            summary = self._summary(
                ok=finish_reason == "stop", turns=turns, stop_reason=finish_reason
            )
            summary["provider_id"] = self.profile["provider_id"]
            summary["model"] = self.profile["model"]
            self.reporter.emit("chat_smoke_summary", **summary)
            return summary
        summary = self._summary(ok=False, turns=turns, stop_reason="max_turns")
        summary["provider_id"] = self.profile["provider_id"]
        summary["model"] = self.profile["model"]
        self.reporter.emit("chat_smoke_summary", **summary)
        return summary


def smoke_chat(args: argparse.Namespace) -> dict[str, Any]:
    """Run the fixed Mimo chat profile against an owned/latest Defaultspack."""

    bootstrap_reporter = SmokeReporter(sys.stdout)
    try:
        if args.max_turns < 1 or args.max_turns > 4:
            raise SmokeRunnerError("--max-turns must be between 1 and 4")
        configuration = load_smoke_configuration(args.port)
        preflight = isolated_smoke_provider_preflight(
            process_environment(), profile_name=MIMO_CHAT_PROFILE
        )
    except Exception as exc:
        safe_error = _redact_string(str(exc))
        bootstrap_reporter.emit("chat_smoke_failed", ok=False, error=safe_error)
        return {"ok": False, "error": safe_error}
    api_key = str(process_environment().get("OPENCODE_ZEN_API_KEY") or "").strip()
    client = DebugHttpClient(
        configuration["base_url"],
        configuration["api_token"],
        configuration["browser_approval_token"],
        stream_timeout=DEFAULT_CHAT_STREAM_INACTIVITY_SECONDS,
    )
    client.hide_secrets(api_key)
    reporter = SmokeReporter(sys.stdout, secrets_to_hide=client.secrets_to_hide)
    runner = ChatOnlySmokeRunner(
        client,
        configuration["artifact"],
        prompt=args.prompt,
        max_turns=args.max_turns,
        reporter=reporter,
        provider_preflight=preflight,
    )
    try:
        return runner.run()
    except Exception as exc:
        safe_error = _redact_string(str(exc), secrets_to_hide=client.secrets_to_hide)
        result = runner.failure_summary(safe_error)
        reporter.emit("chat_smoke_failed", **result)
        return result


def smoke_computer_use(args: argparse.Namespace) -> dict[str, Any]:
    bootstrap_reporter = SmokeReporter(sys.stdout)
    try:
        min_stream_interval_seconds = getattr(
            args,
            "min_stream_interval_seconds",
            DEFAULT_SMOKE_MIN_STREAM_INTERVAL_SECONDS,
        )
        max_transient_resumes = getattr(
            args, "max_transient_resumes", DEFAULT_MAX_TRANSIENT_RESUMES
        )
        if args.max_turns < 1:
            raise SmokeRunnerError("--max-turns must be at least 1")
        if min_stream_interval_seconds < 0:
            raise SmokeRunnerError("--min-stream-interval-seconds must be at least 0")
        if max_transient_resumes < 0:
            raise SmokeRunnerError("--max-transient-resumes must be at least 0")
        configuration = load_smoke_configuration(args.port)
    except Exception as exc:
        safe_error = _redact_string(str(exc))
        bootstrap_reporter.emit("smoke_failed", ok=False, error=safe_error)
        return {"ok": False, "error": safe_error}
    client = DebugHttpClient(
        configuration["base_url"],
        configuration["api_token"],
        configuration["browser_approval_token"],
    )
    reporter = SmokeReporter(sys.stdout, secrets_to_hide=client.secrets_to_hide)
    runner = ComputerUseSmokeRunner(
        client,
        configuration["artifact"],
        prompt=args.prompt or DEFAULT_SMOKE_PROMPT,
        max_turns=args.max_turns,
        reporter=reporter,
        max_transient_resumes=max_transient_resumes,
        min_stream_interval_seconds=min_stream_interval_seconds,
        provider_preflight=getattr(args, "provider_preflight", None),
    )
    try:
        return runner.run()
    except Exception as exc:
        safe_error = _redact_string(str(exc), secrets_to_hide=client.secrets_to_hide)
        result = runner.failure_summary(safe_error)
        reporter.emit("smoke_failed", **result)
        return result


def status(args: argparse.Namespace) -> dict[str, Any]:
    desktop_app = load_desktop_app()
    port = desktop_port(desktop_app, args.port)
    connection_path = Path(args.connection).expanduser() if args.connection else default_connection_path()
    _, broker = load_connection(connection_path)
    health_url = f"http://127.0.0.1:{port}/api/health"
    desktop = {
        "port": port,
        "listening": port_is_open(port),
        "listener": lsof_listener(port),
        "health": http_status(health_url),
    }
    latest = latest_run()
    user_data = Path(latest["user_data"]) if isinstance(latest.get("user_data"), str) else None
    chat_store = Path(latest["chat_store"]) if isinstance(latest.get("chat_store"), str) else None
    result = {
        "ok": bool(broker.get("ok")) and bool(desktop["health"].get("ok")),
        "broker": broker,
        "broker_connection": stale_connection_status(connection_path, broker),
        "defaultspack": desktop,
        "latest_run": latest,
        "edge_haze": edge_haze_status(user_data, broker_connection_path=connection_path),
        "pending_approval": pending_approval_status(chat_store),
    }
    result["owned_launch"] = owned_launch_status(latest)
    return result


def _validated_persisted_launch(
    artifact: Mapping[str, Any],
) -> tuple[int, int, Path] | None:
    """Validate an owned Defaultspack manifest without requiring it is live."""

    if artifact.get("schema") != "rumi.defaultspack-debug-run.v1":
        return None
    pid = _optional_int(artifact.get("pid"))
    port = _optional_int(artifact.get("port"))
    run_id = str(artifact.get("run_id") or "")
    start_marker = str(artifact.get("process_start_marker") or "")
    run_dir_value = str(artifact.get("run_dir") or "")
    if pid is None or port is None or not run_id or not run_dir_value or not start_marker:
        return None
    run_dir = Path(run_dir_value).resolve()
    root = RUN_ROOT.resolve()
    if run_dir.parent != root or run_dir.name != run_id:
        return None
    manifest = Path(str(artifact.get("manifest_path") or "")).resolve()
    if manifest != run_dir / "manifest.json" or not manifest.is_file():
        return None
    try:
        persisted = read_json(manifest)
    except Exception:
        return None
    identity_fields = (
        "schema",
        "run_id",
        "run_dir",
        "manifest_path",
        "pid",
        "process_start_marker",
        "port",
        "token_file",
        "browser_approval_token_file",
        "user_data",
        "chat_store",
    )
    if any(persisted.get(key) != artifact.get(key) for key in identity_fields):
        return None
    return pid, port, run_dir


def _validated_owned_launch(
    artifact: Mapping[str, Any],
) -> tuple[int, int, Path] | None:
    """Validate persisted identity before status/stop may treat a PID as owned."""

    identity = _validated_persisted_launch(artifact)
    if identity is None:
        return None
    pid, _port, _run_dir = identity
    if process_start_marker(pid) != str(artifact.get("process_start_marker") or ""):
        return None
    return identity


def _validated_owned_launch_details(
    artifact: Mapping[str, Any],
) -> tuple[int, int, Path, dict[str, Any]] | None:
    """Return the canonical owned manifest together with its live identity."""

    identity = _validated_owned_launch(artifact)
    if identity is None:
        return None
    pid, port, run_dir = identity
    try:
        persisted = read_json(run_dir / "manifest.json")
    except Exception:
        return None
    # Re-run persisted validation so a manifest replacement between reads is
    # never allowed to select token paths for another process.
    if _validated_persisted_launch(persisted) != identity:
        return None
    return pid, port, run_dir, persisted


def _owned_viewer_pair_manifest_path(
    artifact: Mapping[str, Any],
) -> Path | None:
    """Return the in-run pair manifest referenced by a latest launch artifact."""

    value = artifact.get("viewer_pair_manifest")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser().resolve()
    root = RUN_ROOT.resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if path.name != "viewer-pair-manifest.json" or not path.is_file():
        return None
    return path


def _validated_owned_viewer_pair(
    artifact: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Validate a persisted keep-running Viewer/Defaultspack pair manifest.

    This validates only the immutable launch records.  Liveness is checked for
    each process immediately before it receives a signal so an already-exited
    owned process does not prevent cleanup of the remaining member of the
    pair.
    """

    manifest_path = _owned_viewer_pair_manifest_path(artifact)
    if manifest_path is None:
        return None
    try:
        pair = read_json(manifest_path)
    except Exception:
        return None
    if pair.get("schema") != "rumi.viewer-defaultspack-debug-pair.v1":
        return None
    run_id = str(pair.get("run_id") or "")
    run_dir_value = str(pair.get("run_dir") or "")
    if not run_id or not run_dir_value:
        return None
    run_dir = Path(run_dir_value).resolve()
    root = RUN_ROOT.resolve()
    if run_dir.parent != root or run_dir.name != run_id:
        return None
    if manifest_path != run_dir / "viewer-pair-manifest.json":
        return None
    if str(pair.get("manifest_path") or "") != str(manifest_path):
        return None

    defaultspack = pair.get("defaultspack")
    viewer = pair.get("viewer")
    if not isinstance(defaultspack, dict) or not isinstance(viewer, dict):
        return None
    default_identity = _validated_persisted_launch(defaultspack)
    default_group = _optional_int(defaultspack.get("process_group"))
    if default_identity is None or default_group is None:
        return None
    expected_default = (
        "run_id",
        "pid",
        "port",
        "process_start_marker",
        "manifest_path",
    )
    if any(defaultspack.get(key) != artifact.get(key) for key in expected_default):
        return None

    expected_connection = (
        run_dir / "viewer_user_data" / "host_broker" / "connection.json"
    ).resolve()
    connection_path = (
        Path(str(viewer.get("connection_path") or "")).expanduser().resolve()
    )
    broker_port = _optional_int(viewer.get("broker_port"))
    launch_pid = _optional_int(viewer.get("launch_pid"))
    broker_pid = _optional_int(viewer.get("broker_pid"))
    launch_marker = str(viewer.get("launch_start_marker") or "")
    broker_marker = str(viewer.get("broker_start_marker") or "")
    launch_group = _optional_int(viewer.get("launch_process_group"))
    broker_group = _optional_int(viewer.get("broker_process_group"))
    broker_listener_pid = _optional_int(viewer.get("broker_listener_pid"))
    instance_nonce = str(viewer.get("instance_nonce") or "")
    if (
        connection_path != expected_connection
        or broker_port is None
        or launch_pid is None
        or broker_pid is None
        or not launch_marker
        or not broker_marker
        or launch_group is None
        or broker_group is None
        or broker_listener_pid != broker_pid
        or not instance_nonce
    ):
        return None
    return {
        "pair": pair,
        "run_dir": run_dir,
        "defaultspack": defaultspack,
        "viewer": viewer,
    }


def _owned_pair_process_current(record: Mapping[str, Any]) -> bool:
    """Confirm the exact live PID and process group before pair cleanup."""

    pid = _optional_int(record.get("pid"))
    marker = str(record.get("start_marker") or "")
    process_group = _optional_int(record.get("process_group"))
    if pid is None or not marker or process_group is None or not pid_is_running(pid):
        return False
    return (
        process_start_marker(pid) == marker
        and process_group_id(pid) == process_group
    )


def _stop_validated_owned_pair(
    validated: Mapping[str, Any],
) -> dict[str, Any]:
    """Stop only the exact processes recorded for a supervised Viewer pair.

    The Viewer deliberately remains attached to the foreground PTY, so its
    process group can contain the invoking shell.  We record and validate the
    group as part of PID-reuse protection, but never signal it.  The broker
    listener and cargo launcher are stopped individually, followed by the
    independently-sessioned Defaultspack process.
    """

    viewer = validated["viewer"]
    defaultspack = validated["defaultspack"]
    records = [
        {
            "label": "viewer_broker",
            "pid": viewer["broker_pid"],
            "start_marker": viewer["broker_start_marker"],
            "process_group": viewer["broker_process_group"],
        },
        {
            "label": "viewer_launcher",
            "pid": viewer["launch_pid"],
            "start_marker": viewer["launch_start_marker"],
            "process_group": viewer["launch_process_group"],
        },
        {
            "label": "defaultspack",
            "pid": defaultspack["pid"],
            "start_marker": defaultspack["process_start_marker"],
            "process_group": defaultspack["process_group"],
        },
    ]
    unique_records: list[dict[str, Any]] = []
    seen_pids: set[int] = set()
    for record in records:
        pid = _optional_int(record["pid"])
        if pid is None or pid in seen_pids:
            continue
        seen_pids.add(pid)
        unique_records.append(record)

    results: dict[str, dict[str, Any]] = {}
    active: list[dict[str, Any]] = []
    for record in unique_records:
        label = str(record["label"])
        if _owned_pair_process_current(record):
            active.append(record)
            results[label] = {"stopped": False, "pid": record["pid"]}
        elif pid_is_running(_optional_int(record["pid"]) or -1):
            results[label] = {
                "stopped": False,
                "pid": record["pid"],
                "reason": "owned identity changed",
            }
        else:
            results[label] = {
                "stopped": True,
                "pid": record["pid"],
                "reason": "already exited",
            }

    for record in active:
        try:
            os.kill(int(record["pid"]), signal.SIGTERM)
        except OSError as error:
            results[str(record["label"])] = {
                "stopped": False,
                "pid": record["pid"],
                "reason": f"terminate failed: {error.__class__.__name__}",
            }

    deadline = time.monotonic() + 8.0
    while active and time.monotonic() < deadline:
        active = [
            record for record in active if _owned_pair_process_current(record)
        ]
        if active:
            time.sleep(0.1)

    for record in active:
        label = str(record["label"])
        if not _owned_pair_process_current(record):
            continue
        try:
            os.kill(int(record["pid"]), signal.SIGKILL)
            results[label]["forced"] = True
        except OSError as error:
            results[label] = {
                "stopped": False,
                "pid": record["pid"],
                "reason": f"kill failed: {error.__class__.__name__}",
            }

    for record in unique_records:
        label = str(record["label"])
        if not _owned_pair_process_current(record):
            results[label]["stopped"] = True
    stopped = all(item.get("stopped") for item in results.values())
    return {
        "ok": stopped,
        "stopped": stopped,
        "run_id": validated["run_dir"].name,
        "pair": results,
    }


def owned_launch_status(artifact: Mapping[str, Any]) -> dict[str, Any]:
    identity = _validated_owned_launch(artifact)
    if identity is None:
        return {"owned": False, "running": False}
    pid, port, run_dir = identity
    listener = lsof_listener(port)
    listener_pid = _optional_int(listener.get("pid")) if listener else None
    return {
        "owned": True,
        "running": pid_is_running(pid) and listener_pid == pid,
        "pid": pid,
        "port": port,
        "run_id": run_dir.name,
    }


def stop_latest_owned_launch(_args: argparse.Namespace) -> dict[str, Any]:
    """Stop only a live listener proven by the latest owned run manifest."""

    artifact = latest_run()
    if "viewer_pair_manifest" in artifact:
        pair = _validated_owned_viewer_pair(artifact)
        if pair is None:
            return {
                "ok": False,
                "stopped": False,
                "error": "no validated owned Viewer/Defaultspack pair",
            }
        return _stop_validated_owned_pair(pair)
    identity = _validated_owned_launch(artifact)
    if identity is None:
        return {"ok": False, "stopped": False, "error": "no validated owned launch"}
    pid, port, run_dir = identity
    listener = lsof_listener(port)
    listener_pid = _optional_int(listener.get("pid")) if listener else None
    if listener_pid != pid or not pid_is_running(pid):
        return {
            "ok": True,
            "stopped": False,
            "reason": "owned process is not the active listener",
            "run_id": run_dir.name,
        }
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 8.0
    while pid_is_running(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    forced = False
    if pid_is_running(pid):
        # Recheck the port identity immediately before escalation. A recycled
        # PID or replacement listener must never be killed.
        listener = lsof_listener(port)
        if (_optional_int(listener.get("pid")) if listener else None) != pid:
            return {
                "ok": False,
                "stopped": False,
                "error": "owned listener identity changed during stop",
                "run_id": run_dir.name,
            }
        os.kill(pid, signal.SIGKILL)
        forced = True
    return {"ok": True, "stopped": True, "forced": forced, "run_id": run_dir.name}


def wait_for_health(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if http_status(f"http://127.0.0.1:{port}/api/health").get("ok"):
            return True
        time.sleep(0.5)
    return False


def wait_for_owned_defaultspack_health(
    port: int, process: subprocess.Popen[Any], timeout: float
) -> bool:
    """Require the launched child, rather than an arbitrary local listener.

    An isolated run must release its reservation immediately before the child
    binds.  A health-only probe would let a competing process win that race.
    The desktop app owns its HTTP socket directly, so its PID is a sufficient
    non-secret identity for the harness readiness check.
    """

    deadline = time.time() + timeout
    expected_pid = _optional_int(getattr(process, "pid", None))
    while time.time() < deadline:
        if getattr(process, "poll", lambda: None)() is not None:
            return False
        listener = lsof_listener(port)
        listener_pid = _optional_int(listener.get("pid")) if listener else None
        if (
            expected_pid is not None
            and listener_pid == expected_pid
            and http_status(f"http://127.0.0.1:{port}/api/health").get("ok")
        ):
            return True
        time.sleep(0.25)
    return False


def _has_live_pty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()
    except Exception:
        return False


def require_live_pty() -> None:
    if not _has_live_pty():
        raise LivePtyRequiredError(
            "viewer-smoke-computer-use must run from a real live PTY; do not use nohup, "
            "a detached shell, or an agent background process"
        )


def viewer_build_environment(
    min_free_mb: int,
    broker_port: int | None = None,
    *,
    connection_path: Path | None = None,
    instance_nonce: str = "",
    debug_instance_id: str = "",
    debug_user_data_root: Path | None = None,
    defaultspack_run_id: str = "",
    defaultspack_state_root: Path | None = None,
    defaultspack_http_port: int | None = None,
    kernel_port: int | None = None,
    isolated_provider_parent_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if min_free_mb <= 0:
        raise ValueError("Viewer debug build minimum free space must be positive")
    env = copy_process_environment()
    # These are accepted only by the debug-build native lifecycle gate.  Do
    # not let a caller's shell values select an arbitrary existing instance.
    env.pop(VIEWER_DEBUG_INSTANCE_ID_ENV, None)
    env.pop(VIEWER_DEBUG_USER_DATA_ROOT_ENV, None)
    env.pop(VIEWER_TRUSTED_CHAT_STORE_ENV, None)
    env.pop("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", None)
    env["RUMI_VIEWER_MIN_FREE_MB"] = str(min_free_mb)
    env["RUMI_VIEWER_BROKER_PORT"] = str(configured_viewer_broker_port(broker_port))
    if connection_path is not None:
        env[VIEWER_BROKER_CONNECTION_ENV] = str(connection_path)
    if instance_nonce:
        env[VIEWER_BROKER_INSTANCE_NONCE_ENV] = instance_nonce
    if bool(debug_instance_id) != bool(debug_user_data_root):
        raise ValueError("Viewer debug instance ID and user-data root must be supplied together")
    if debug_instance_id and debug_user_data_root is not None:
        env[VIEWER_DEBUG_INSTANCE_ID_ENV] = validate_debug_instance_id(debug_instance_id)
        root = debug_user_data_root.resolve()
        if not root.is_absolute():
            raise ValueError("Viewer debug user-data root must be absolute")
        env[VIEWER_DEBUG_USER_DATA_ROOT_ENV] = str(root)
    debug_values = (
        defaultspack_run_id,
        defaultspack_state_root,
        defaultspack_http_port,
        kernel_port,
    )
    if any(value not in (None, "") for value in debug_values):
        if not (
            defaultspack_run_id
            and defaultspack_state_root is not None
            and defaultspack_http_port is not None
            and kernel_port is not None
            and debug_instance_id
            and debug_user_data_root is not None
            and instance_nonce
        ):
            raise ValueError("Defaultspack debug isolation requires complete Viewer isolation context")
        state_root = defaultspack_state_root.resolve()
        expected_state_root = debug_user_data_root.resolve().parent / "defaultspack_state"
        if state_root != expected_state_root or not state_root.is_absolute():
            raise ValueError("Defaultspack debug state root must be the per-run sibling of Viewer user data")
        http_port = _strict_loopback_port(defaultspack_http_port, name="Defaultspack HTTP port")
        selected_kernel_port = _strict_loopback_port(kernel_port, name="kernel port")
        broker = configured_viewer_broker_port(broker_port)
        if {
            http_port,
            selected_kernel_port,
            broker,
        }.__len__() != 3 or http_port == DEFAULT_DEFAULTSPACK_HTTP_PORT or selected_kernel_port == DEFAULT_KERNEL_PORT:
            raise ValueError("Defaultspack debug isolation requires distinct non-default run ports")
        state_root.mkdir(parents=True, exist_ok=True)
        try:
            state_root.chmod(0o700)
        except OSError as error:
            raise ValueError(f"failed to secure Defaultspack debug state root: {error}") from error
        env[DEFAULTSPACK_DEBUG_ISOLATION_ENV] = "1"
        env[DEFAULTSPACK_DEBUG_RUN_ID_ENV] = validate_debug_instance_id(defaultspack_run_id)
        env[DEFAULTSPACK_DEBUG_LAUNCH_NONCE_ENV] = instance_nonce
        env[DEFAULTSPACK_DEBUG_STATE_ROOT_ENV] = str(state_root)
        env[VIEWER_TRUSTED_CHAT_STORE_ENV] = str(state_root / "chat" / "conversations.json")
        env[DEFAULTSPACK_DEBUG_HTTP_PORT_ENV] = str(http_port)
        env[DEFAULTSPACK_DEBUG_KERNEL_PORT_ENV] = str(selected_kernel_port)
        env["RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH"] = str(
            state_root / "approval" / "approval_runtime_secret"
        )
        # Make the signing key before the Viewer starts.  Both children get
        # only an owner-only pathname, never the key value itself.
        approval_secret_path = prepare_defaultspack_approval_secret(state_root)
        env["RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH"] = str(approval_secret_path)
        apply_isolated_smoke_provider_env(
            env,
            parent_env=isolated_provider_parent_env,
            # Generic debug start-up remains usable without a cloud provider,
            # while viewer-smoke passes a checked parent env and requires it.
            require_credential=isolated_provider_parent_env is not None,
        )
    return env


class ViewerLogTee:
    """Mirror an owned child output stream to a redacted artifact log."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        log_path: Path,
        *,
        secrets_to_hide: tuple[str, ...] = (),
        echo: bool = True,
    ) -> None:
        self.process = process
        self.log_path = log_path
        self.secrets_to_hide = secrets_to_hide
        self.echo = echo
        self.wry_detached_panic = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float = 1.0) -> None:
        self._thread.join(timeout=timeout)

    def consume_line(self, line: str) -> str:
        if WRY_DETACHED_PANIC in line:
            self.wry_detached_panic = True
        safe_line = _redact_string(line, secrets_to_hide=self.secrets_to_hide)
        return safe_line

    def _run(self) -> None:
        output = getattr(self.process, "stdout", None)
        if output is None:
            return
        with self.log_path.open("a", encoding="utf-8") as log:
            for line in output:
                safe_line = self.consume_line(line)
                log.write(safe_line)
                log.flush()
                if self.echo:
                    sys.stdout.write(safe_line)
                    sys.stdout.flush()


def start_viewer_dev(
    log_path: Path,
    *,
    min_free_mb: int = DEFAULT_VIEWER_MIN_FREE_MB,
    broker_port: int | None = None,
    connection_path: Path | None = None,
    instance_nonce: str = "",
    debug_instance_id: str = "",
    debug_user_data_root: Path | None = None,
    defaultspack_run_id: str = "",
    defaultspack_state_root: Path | None = None,
    defaultspack_http_port: int | None = None,
    kernel_port: int | None = None,
    isolated_provider_parent_env: Mapping[str, str] | None = None,
) -> tuple[subprocess.Popen[str], ViewerLogTee]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "Viewer command: cargo tauri dev (attached live PTY; redacted output follows)\n"
        f"Viewer debug build preflight: {min_free_mb} MiB required\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        list(VIEWER_DEV_COMMAND),
        cwd=VIEWER_ROOT,
        env=viewer_build_environment(
            min_free_mb,
            broker_port,
            connection_path=connection_path,
            instance_nonce=instance_nonce,
            debug_instance_id=debug_instance_id,
            debug_user_data_root=debug_user_data_root,
            defaultspack_run_id=defaultspack_run_id,
            defaultspack_state_root=defaultspack_state_root,
            defaultspack_http_port=defaultspack_http_port,
            kernel_port=kernel_port,
            isolated_provider_parent_env=isolated_provider_parent_env,
        ),
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        # Keep the normal terminal session: detached/nohup launches can panic Wry.
        start_new_session=False,
    )
    log_tee = ViewerLogTee(
        process,
        log_path,
        secrets_to_hide=isolated_smoke_provider_secret_values(isolated_provider_parent_env),
    )
    log_tee.start()
    return process, log_tee


def wait_for_viewer_broker(
    connection_path: Path,
    process: subprocess.Popen[str],
    log_tee: ViewerLogTee,
    timeout: float,
    *,
    expected_port: int | None = None,
    expected_instance_nonce: str = "",
    launched_at: int = 0,
) -> dict[str, Any]:
    broker_port = configured_viewer_broker_port(expected_port)
    deadline = time.time() + timeout
    last_broker: dict[str, Any] = {}
    owned_listener = False
    while time.time() < deadline:
        raw_connection, broker = load_connection(connection_path, expected_port=broker_port)
        last_broker = broker
        connection = broker.get("connection") if isinstance(broker.get("connection"), dict) else {}
        fresh_instance = (
            bool(expected_instance_nonce)
            and raw_connection.get("instance_nonce") == expected_instance_nonce
            and (_optional_int(raw_connection.get("created_at")) or 0) >= launched_at
        )
        connection_pid = _optional_int(raw_connection.get("pid"))
        listener = lsof_listener(broker_port)
        listener_pid = _optional_int(listener.get("pid")) if listener else None
        owned_listener = bool(connection_pid and listener_pid == connection_pid)
        if (
            broker.get("ok")
            and connection.get("port") == broker_port
            and fresh_instance
            and owned_listener
            and process.poll() is None
        ):
            return broker
        exit_code = process.poll()
        if exit_code is not None:
            log_tee.join()
            if log_tee.wry_detached_panic:
                raise SmokeRunnerError(
                    f"Viewer exited with the detached Wry WKWebView panic ({WRY_DETACHED_PANIC}); "
                    "rerun from a real foreground PTY"
                )
            connection_present = connection_path.exists()
            if exit_code == 0 and not connection_present:
                classification = "duplicate_instance_or_pre_setup_exit"
                stage = "before_connection_publish"
            elif exit_code == 0:
                classification = "startup_failure_after_connection_publish"
                stage = "after_connection_publish"
            else:
                classification = "startup_failure"
                stage = "before_broker_ready"
            raise SmokeRunnerError(
                f"Viewer exited with code {exit_code} before broker {broker_port} became healthy "
                f"(stage={stage}; classification={classification})"
            )
        time.sleep(0.25)
    stale = stale_connection_status(connection_path, last_broker)
    if last_broker.get("ok") and not owned_listener:
        detail = "broker listener PID did not match the per-run connection PID"
    else:
        detail = stale["reason"] if stale.get("stale") else "broker did not become healthy"
    raise SmokeRunnerError(
        f"Viewer broker {broker_port} was not ready within {timeout:g}s: {detail}"
    )


def stop_owned_process(process: subprocess.Popen[Any] | None, *, label: str) -> dict[str, Any]:
    if process is None:
        return {"label": label, "stopped": False, "reason": "not started"}
    if process.poll() is not None:
        return {"label": label, "stopped": True, "exit_code": process.returncode}
    try:
        process.terminate()
        process.wait(timeout=8)
        return {"label": label, "stopped": True, "exit_code": process.returncode}
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
        return {"label": label, "stopped": True, "exit_code": process.returncode, "forced": True}


def persist_keep_running_viewer_pair(
    supervisor_dir: Path,
    *,
    viewer_process: subprocess.Popen[Any],
    defaultspack_process: subprocess.Popen[Any],
    defaultspack_launch: Mapping[str, Any],
    connection_path: Path,
    broker_port: int,
    instance_nonce: str,
) -> dict[str, Any]:
    """Persist a stop-safe identity for a healthy supervised Viewer pair.

    The Viewer command is intentionally attached to the live PTY, so its
    process group is not exclusively ours. We persist and later validate that
    group to strengthen PID-reuse checks, while process stop remains limited
    to the broker listener, cargo launcher, and Defaultspack PID themselves.
    """

    run_dir = supervisor_dir.resolve()
    root = RUN_ROOT.resolve()
    if run_dir.parent != root or not run_dir.name:
        raise SmokeRunnerError("invalid supervised Viewer run directory")
    expected_connection = (
        run_dir / "viewer_user_data" / "host_broker" / "connection.json"
    ).resolve()
    if connection_path.resolve() != expected_connection:
        raise SmokeRunnerError("invalid supervised Viewer connection path")
    try:
        connection = read_json(expected_connection)
    except Exception as error:
        raise SmokeRunnerError("could not read supervised Viewer connection") from error
    broker_pid = _optional_int(connection.get("pid"))
    connection_port = _optional_int(connection.get("port"))
    if (
        broker_pid is None
        or connection_port != broker_port
        or str(connection.get("instance_nonce") or "") != instance_nonce
    ):
        raise SmokeRunnerError("supervised Viewer connection identity changed")
    listener = lsof_listener(broker_port)
    listener_pid = _optional_int(listener.get("pid")) if listener else None
    if listener_pid != broker_pid:
        raise SmokeRunnerError("supervised Viewer broker is not its recorded listener")

    launch_pid = _optional_int(getattr(viewer_process, "pid", None))
    defaultspack_pid = _optional_int(getattr(defaultspack_process, "pid", None))
    if (
        launch_pid is None
        or defaultspack_pid is None
        or viewer_process.poll() is not None
        or defaultspack_process.poll() is not None
    ):
        raise SmokeRunnerError("supervised Viewer pair is no longer running")
    default_identity = _validated_owned_launch(defaultspack_launch)
    if default_identity is None or default_identity[0] != defaultspack_pid:
        raise SmokeRunnerError("supervised Defaultspack identity is not valid")
    default_listener = lsof_listener(default_identity[1])
    default_listener_pid = (
        _optional_int(default_listener.get("pid")) if default_listener else None
    )
    if default_listener_pid != defaultspack_pid:
        raise SmokeRunnerError("supervised Defaultspack is not its recorded listener")

    def process_identity(pid: int, *, role: str) -> tuple[str, int]:
        marker = process_start_marker(pid)
        process_group = process_group_id(pid)
        if not marker or process_group is None:
            raise SmokeRunnerError(f"supervised {role} process identity is unavailable")
        return marker, process_group

    launch_marker, launch_group = process_identity(launch_pid, role="Viewer launcher")
    broker_marker, broker_group = process_identity(broker_pid, role="Viewer broker")
    default_marker, default_group = process_identity(
        defaultspack_pid, role="Defaultspack"
    )
    if default_marker != str(defaultspack_launch.get("process_start_marker") or ""):
        raise SmokeRunnerError("supervised Defaultspack start marker changed")

    pair_manifest_path = run_dir / "viewer-pair-manifest.json"
    defaultspack_record = dict(defaultspack_launch)
    defaultspack_record["process_group"] = default_group
    pair = {
        "schema": "rumi.viewer-defaultspack-debug-pair.v1",
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "manifest_path": str(pair_manifest_path),
        "viewer": {
            "connection_path": str(expected_connection),
            "instance_nonce": instance_nonce,
            "broker_port": broker_port,
            "broker_listener_pid": broker_pid,
            "broker_pid": broker_pid,
            "broker_start_marker": broker_marker,
            "broker_process_group": broker_group,
            "launch_pid": launch_pid,
            "launch_start_marker": launch_marker,
            "launch_process_group": launch_group,
        },
        "defaultspack": defaultspack_record,
    }
    _write_json_atomic(pair_manifest_path, pair)
    latest = dict(defaultspack_launch)
    latest["viewer_pair_manifest"] = str(pair_manifest_path)
    _write_json_atomic(LATEST_JSON, latest)
    return pair


def viewer_smoke_computer_use(args: argparse.Namespace) -> dict[str, Any]:
    try:
        require_live_pty()
    except LivePtyRequiredError as error:
        return {"ok": False, "error": str(error)}
    broker_port = configured_viewer_broker_port(getattr(args, "viewer_broker_port", None))
    if broker_port == DEFAULT_VIEWER_BROKER_PORT:
        return {
            "ok": False,
            "error": (
                "viewer-smoke-computer-use requires an explicit non-default "
                "--viewer-broker-port for debug instance isolation"
            ),
        }
    provider_preflight = isolated_smoke_provider_preflight(process_environment())
    if not provider_preflight["credential_present"]:
        return {
            "ok": False,
            "error": "PROVIDER_ENV_NOT_PRESENT",
            "provider_preflight": provider_preflight,
        }
    _, supervisor_dir = create_unique_run_dir("viewer-smoke")
    requested_connection_path = (
        Path(args.connection).expanduser()
        if args.connection
        else supervisor_dir / "viewer_user_data" / "host_broker" / "connection.json"
    )
    try:
        viewer_user_data_root, connection_path = prepare_owned_viewer_debug_root(
            supervisor_dir, requested_connection_path
        )
    except SmokeRunnerError as error:
        return {"ok": False, "error": str(error)}
    _, existing_broker = load_connection(connection_path, expected_port=broker_port)
    existing_state = stale_connection_status(connection_path, existing_broker)
    if existing_broker.get("ok"):
        return {
            "ok": False,
            "error": "Viewer broker is already healthy; this command only supervises the Viewer it starts",
            "broker_connection": existing_state,
        }

    # Reserve both Defaultspack ports while the per-run identity is built.
    # They are released only immediately before the Viewer/kernel and the
    # owned Defaultspack process respectively start.  Neither readiness path
    # is allowed to adopt an existing listener.
    requested_http_port = getattr(args, "defaultspack_http_port", None)
    if requested_http_port is not None and getattr(args, "port", None) is not None:
        return {
            "ok": False,
            "error": "viewer-smoke-computer-use accepts only one of --port and --defaultspack-http-port",
        }
    if requested_http_port is None:
        requested_http_port = getattr(args, "port", None)
    try:
        http_reservation = reserve_loopback_port(
            requested=requested_http_port,
            excluded={broker_port, DEFAULT_DEFAULTSPACK_HTTP_PORT},
            name="Defaultspack HTTP port",
        )
        kernel_reservation = reserve_loopback_port(
            requested=getattr(args, "kernel_port", None),
            excluded={broker_port, http_reservation.port, DEFAULT_KERNEL_PORT},
            name="kernel port",
        )
    except SmokeRunnerError as error:
        return {"ok": False, "error": str(error)}
    defaultspack_state_root = supervisor_dir / "defaultspack_state"
    try:
        approval_secret_path = prepare_defaultspack_approval_secret(defaultspack_state_root)
    except SmokeRunnerError as error:
        http_reservation.release()
        kernel_reservation.release()
        return {"ok": False, "error": str(error)}

    viewer_log_path = supervisor_dir / "viewer.log"
    supervisor_log_path = supervisor_dir / "supervisor.jsonl"
    supervisor_dir.mkdir(parents=True, exist_ok=True)
    reporter = SmokeReporter(supervisor_log_path.open("a", encoding="utf-8"))
    reporter.emit(
        "viewer_smoke_starting",
        broker_connection_path=str(connection_path),
        viewer_debug_user_data_root=str(viewer_user_data_root),
        stale_connection=existing_state,
        viewer_log_path=str(viewer_log_path),
        defaultspack_http_port=http_reservation.port,
        kernel_port=kernel_reservation.port,
        defaultspack_state_root=str(defaultspack_state_root),
        approval_secret_ready=approval_secret_path.exists(),
        provider_preflight=provider_preflight,
    )
    viewer_process: subprocess.Popen[str] | None = None
    viewer_log_tee: ViewerLogTee | None = None
    defaultspack_process: subprocess.Popen[Any] | None = None
    defaultspack_log_tee: ViewerLogTee | None = None
    keep_running_pair: dict[str, Any] | None = None
    result: dict[str, Any]
    instance_nonce = secrets.token_urlsafe(24)
    debug_instance_id = generate_debug_instance_id()
    launched_at = int(time.time())
    try:
        # The Viewer owns the kernel port.  Recheck that the reserved port was
        # never adopted before allowing it to bind.
        kernel_reservation.release()
        if port_is_open(kernel_reservation.port):
            raise SmokeRunnerError("kernel port was claimed before the isolated Viewer started")
        viewer_process, viewer_log_tee = start_viewer_dev(
            viewer_log_path,
            min_free_mb=getattr(args, "viewer_min_free_mb", DEFAULT_VIEWER_MIN_FREE_MB),
            broker_port=broker_port,
            connection_path=connection_path,
            instance_nonce=instance_nonce,
            debug_instance_id=debug_instance_id,
            debug_user_data_root=viewer_user_data_root,
            defaultspack_run_id=debug_instance_id,
            defaultspack_state_root=defaultspack_state_root,
            defaultspack_http_port=http_reservation.port,
            kernel_port=kernel_reservation.port,
            isolated_provider_parent_env=process_environment(),
        )
        broker = wait_for_viewer_broker(
            connection_path,
            viewer_process,
            viewer_log_tee,
            args.viewer_wait_seconds,
            expected_port=broker_port,
            expected_instance_nonce=instance_nonce,
            launched_at=launched_at,
        )
        reporter.emit(
            "viewer_broker_ready",
            port=broker_port,
            connection=broker.get("connection"),
        )
        launch_args = argparse.Namespace(
            port=http_reservation.port,
            connection=str(connection_path),
            viewer_broker_port=broker_port,
            user_data=str(defaultspack_state_root / "user_data"),
            wait_seconds=args.wait_seconds,
            allow_no_broker=False,
            defaultspack_debug_run_id=debug_instance_id,
            defaultspack_debug_nonce=instance_nonce,
            defaultspack_debug_state_root=defaultspack_state_root,
            defaultspack_kernel_port=kernel_reservation.port,
            isolated_provider_parent_env=process_environment(),
        )
        http_reservation.release()
        if port_is_open(http_reservation.port):
            raise SmokeRunnerError("Defaultspack HTTP port was claimed before the owned server started")
        launched = launch(launch_args, include_process=True)
        defaultspack_process = launched.pop("_process", None)
        defaultspack_log_tee = launched.pop("_log_tee", None)
        if not launched.get("ok"):
            raise SmokeRunnerError(str(launched.get("error") or "isolated defaultspack failed to start"))
        reporter.emit("defaultspack_ready", launch=launched.get("launch"))
        smoke_args = argparse.Namespace(
            port=http_reservation.port,
            max_turns=args.max_turns,
            prompt=args.prompt,
            min_stream_interval_seconds=getattr(
                args,
                "min_stream_interval_seconds",
                DEFAULT_SMOKE_MIN_STREAM_INTERVAL_SECONDS,
            ),
            max_transient_resumes=getattr(
                args, "max_transient_resumes", DEFAULT_MAX_TRANSIENT_RESUMES
            ),
            provider_preflight=provider_preflight,
        )
        smoke = smoke_computer_use(smoke_args)
        if viewer_process.poll() is not None:
            viewer_log_tee.join()
            if viewer_log_tee.wry_detached_panic:
                raise SmokeRunnerError(
                    f"Viewer hit the detached Wry WKWebView panic ({WRY_DETACHED_PANIC}) during smoke"
                )
            raise SmokeRunnerError("Viewer exited while the computer-use smoke was running")
        if args.keep_running:
            if not smoke.get("ok"):
                raise SmokeRunnerError("computer-use smoke failed; owned pair was stopped")
            if defaultspack_process is None:
                raise SmokeRunnerError("supervised Defaultspack process was not retained")
            keep_running_pair = persist_keep_running_viewer_pair(
                supervisor_dir,
                viewer_process=viewer_process,
                defaultspack_process=defaultspack_process,
                defaultspack_launch=launched["launch"],
                connection_path=connection_path,
                broker_port=broker_port,
                instance_nonce=instance_nonce,
            )
        result = {
            "ok": bool(smoke.get("ok")),
            "viewer_log_path": str(viewer_log_path),
            "supervisor_log_path": str(supervisor_log_path),
            "defaultspack": launched.get("launch"),
            "smoke": smoke,
        }
        if keep_running_pair is not None:
            result["viewer_pair_manifest"] = keep_running_pair["manifest_path"]
    except (LivePtyRequiredError, SmokeRunnerError) as error:
        reporter.emit("viewer_smoke_failed", ok=False, error=str(error))
        result = {
            "ok": False,
            "error": str(error),
            "viewer_log_path": str(viewer_log_path),
            "supervisor_log_path": str(supervisor_log_path),
        }
    except Exception as error:
        safe_error = _redact_string(str(error))
        reporter.emit("viewer_smoke_failed", ok=False, error=safe_error)
        result = {
            "ok": False,
            "error": safe_error,
            "viewer_log_path": str(viewer_log_path),
            "supervisor_log_path": str(supervisor_log_path),
        }
    finally:
        http_reservation.release()
        kernel_reservation.release()
        if keep_running_pair is not None:
            cleanup = {
                "kept_running": True,
                "viewer_pair_manifest": keep_running_pair["manifest_path"],
            }
        else:
            cleanup = {
                "defaultspack": stop_owned_process(defaultspack_process, label="defaultspack"),
                "viewer": stop_owned_process(viewer_process, label="viewer"),
            }
        if viewer_log_tee is not None:
            viewer_log_tee.join()
        if defaultspack_log_tee is not None:
            defaultspack_log_tee.join()
        reporter.emit("viewer_smoke_cleanup", **cleanup)
        reporter.stream.close()
    result["cleanup"] = cleanup
    return result


def viewer_direct_computer_use(args: argparse.Namespace) -> dict[str, Any]:
    """Run the provider-free Atlas background test in a fully owned debug pair."""

    try:
        require_live_pty()
    except LivePtyRequiredError as error:
        return {"ok": False, "error": _direct_failure_code(error)}
    broker_port = configured_viewer_broker_port(getattr(args, "viewer_broker_port", None))
    if broker_port == DEFAULT_VIEWER_BROKER_PORT:
        return {
            "ok": False,
            "error": (
                "viewer-direct-computer-use requires an explicit non-default "
                "--viewer-broker-port for debug instance isolation"
            ),
        }
    _, supervisor_dir = create_unique_run_dir("viewer-direct")
    requested_connection_path = (
        Path(args.connection).expanduser()
        if getattr(args, "connection", None)
        else supervisor_dir / "viewer_user_data" / "host_broker" / "connection.json"
    )
    try:
        viewer_user_data_root, connection_path = prepare_owned_viewer_debug_root(
            supervisor_dir, requested_connection_path
        )
    except SmokeRunnerError as error:
        return {"ok": False, "error": _direct_failure_code(error)}
    _, existing_broker = load_connection(connection_path, expected_port=broker_port)
    if existing_broker.get("ok"):
        return {"ok": False, "error": "owned Viewer broker path is already healthy"}

    requested_http_port = getattr(args, "defaultspack_http_port", None)
    if requested_http_port is not None and getattr(args, "port", None) is not None:
        return {
            "ok": False,
            "error": "viewer-direct-computer-use accepts only one of --port and --defaultspack-http-port",
        }
    if requested_http_port is None:
        requested_http_port = getattr(args, "port", None)
    try:
        http_reservation = reserve_loopback_port(
            requested=requested_http_port,
            excluded={broker_port, DEFAULT_DEFAULTSPACK_HTTP_PORT},
            name="Defaultspack HTTP port",
        )
        kernel_reservation = reserve_loopback_port(
            requested=getattr(args, "kernel_port", None),
            excluded={broker_port, http_reservation.port, DEFAULT_KERNEL_PORT},
            name="kernel port",
        )
    except SmokeRunnerError as error:
        return {"ok": False, "error": _direct_failure_code(error)}
    defaultspack_state_root = supervisor_dir / "defaultspack_state"
    direct_artifact_root = (
        defaultspack_state_root
        / "chat"
        / "conversations"
        / "direct-http"
        / "workspace"
        / "tools"
        / "computer"
    )
    try:
        prepare_defaultspack_approval_secret(defaultspack_state_root)
    except SmokeRunnerError as error:
        http_reservation.release()
        kernel_reservation.release()
        return {"ok": False, "error": _direct_failure_code(error)}

    supervisor_dir.mkdir(parents=True, exist_ok=True)
    viewer_log_path = supervisor_dir / "viewer.log"
    supervisor_log_path = supervisor_dir / "supervisor.jsonl"
    reporter = SmokeReporter(supervisor_log_path.open("a", encoding="utf-8"))
    reporter.emit(
        "viewer_direct_starting",
        run_name=supervisor_dir.name,
        broker_port=broker_port,
        approval_secret_ready=True,
        provider_used=False,
    )
    viewer_process: subprocess.Popen[str] | None = None
    viewer_log_tee: ViewerLogTee | None = None
    defaultspack_process: subprocess.Popen[Any] | None = None
    defaultspack_log_tee: ViewerLogTee | None = None
    instance_nonce = secrets.token_urlsafe(24)
    debug_instance_id = generate_debug_instance_id()
    launched_at = int(time.time())
    try:
        kernel_reservation.release()
        if port_is_open(kernel_reservation.port):
            raise SmokeRunnerError("kernel port was claimed before the isolated Viewer started")
        viewer_process, viewer_log_tee = start_viewer_dev(
            viewer_log_path,
            min_free_mb=getattr(args, "viewer_min_free_mb", DEFAULT_VIEWER_MIN_FREE_MB),
            broker_port=broker_port,
            connection_path=connection_path,
            instance_nonce=instance_nonce,
            debug_instance_id=debug_instance_id,
            debug_user_data_root=viewer_user_data_root,
            defaultspack_run_id=debug_instance_id,
            defaultspack_state_root=defaultspack_state_root,
            defaultspack_http_port=http_reservation.port,
            kernel_port=kernel_reservation.port,
            isolated_provider_parent_env=None,
        )
        broker = wait_for_viewer_broker(
            connection_path,
            viewer_process,
            viewer_log_tee,
            getattr(args, "viewer_wait_seconds", 90.0),
            expected_port=broker_port,
            expected_instance_nonce=instance_nonce,
            launched_at=launched_at,
        )
        reporter.emit(
            "viewer_broker_ready",
            broker_port=broker_port,
            authenticated=bool(broker.get("ok")),
        )
        launch_args = argparse.Namespace(
            port=http_reservation.port,
            connection=str(connection_path),
            viewer_broker_port=broker_port,
            user_data=str(defaultspack_state_root / "user_data"),
            wait_seconds=getattr(args, "wait_seconds", 30.0),
            allow_no_broker=False,
            defaultspack_debug_run_id=debug_instance_id,
            defaultspack_debug_nonce=instance_nonce,
            defaultspack_debug_state_root=defaultspack_state_root,
            defaultspack_kernel_port=kernel_reservation.port,
            isolated_provider_parent_env=None,
        )
        http_reservation.release()
        if port_is_open(http_reservation.port):
            raise SmokeRunnerError("Defaultspack HTTP port was claimed before the owned server started")
        launched = launch(launch_args, include_process=True)
        defaultspack_process = launched.pop("_process", None)
        defaultspack_log_tee = launched.pop("_log_tee", None)
        if not launched.get("ok"):
            raise SmokeRunnerError(str(launched.get("error") or "isolated defaultspack failed to start"))
        artifact = launched.get("launch") if isinstance(launched.get("launch"), dict) else {}
        api_token = _read_debug_token(artifact, ("token_file",), "local API")
        browser_token = _read_debug_token(
            artifact,
            ("browser_approval_token_file",),
            "browser approval",
        )
        client = DebugHttpClient(
            f"http://127.0.0.1:{http_reservation.port}",
            api_token,
            browser_token,
        )
        reporter.hide_secrets(*client.secrets_to_hide)
        reporter.emit("defaultspack_ready", owned=True, provider_used=False)
        direct = direct_computer_use_sequence(
            client,
            run_dir=supervisor_dir,
            viewer_user_data_root=viewer_user_data_root,
            direct_artifact_root=direct_artifact_root,
            reporter=reporter,
            run_nonce=debug_instance_id,
            probe_only=bool(getattr(args, "probe_only", False)),
        )
        if viewer_process.poll() is not None:
            raise SmokeRunnerError("Viewer exited while the direct computer-use test was running")
        result = {
            "ok": bool(direct.get("ok")),
            "run_name": supervisor_dir.name,
            "provider_used": False,
            "chat_used": False,
            "model_used": False,
            "direct": direct,
            "evidence_directory": (
                "evidence/screenshots" if not direct.get("probe_only") else None
            ),
        }
    except Exception as error:
        failure_report = _direct_failure_report(error)
        reporter.emit("viewer_direct_failed", ok=False, **failure_report)
        result = {
            "ok": False,
            "run_name": supervisor_dir.name,
            "provider_used": False,
            "chat_used": False,
            "model_used": False,
            **failure_report,
        }
    finally:
        http_reservation.release()
        kernel_reservation.release()
        cleanup = {
            "defaultspack": stop_owned_process(defaultspack_process, label="defaultspack"),
            "viewer": stop_owned_process(viewer_process, label="viewer"),
        }
        if viewer_log_tee is not None:
            viewer_log_tee.join()
        if defaultspack_log_tee is not None:
            defaultspack_log_tee.join()
        reporter.emit(
            "viewer_direct_cleanup",
            defaultspack_stopped=bool(cleanup["defaultspack"].get("stopped")),
            viewer_stopped=bool(cleanup["viewer"].get("stopped")),
        )
        reporter.stream.close()
    result["cleanup"] = cleanup
    return result


def launch(args: argparse.Namespace, *, include_process: bool = False) -> dict[str, Any]:
    desktop_app = load_desktop_app()
    port = desktop_port(desktop_app, args.port)
    isolated_provider_parent_env = getattr(args, "isolated_provider_parent_env", None)
    isolated_provider_profile = getattr(
        args, "isolated_provider_profile", CEREBRAS_COMPUTER_PROFILE
    )
    if isolated_provider_parent_env is not None:
        try:
            _smoke_provider_profile(isolated_provider_profile)
        except SmokeRunnerError as error:
            return {"ok": False, "error": str(error)}
        if getattr(args, "port", None) is None:
            try:
                reservation = reserve_loopback_port(
                    requested=None,
                    excluded={DEFAULT_DEFAULTSPACK_HTTP_PORT, DEFAULT_KERNEL_PORT},
                    name="Defaultspack HTTP port",
                )
                port = reservation.port
                reservation.release()
            except SmokeRunnerError as error:
                return {"ok": False, "error": str(error)}
        elif port == DEFAULT_DEFAULTSPACK_HTTP_PORT:
            return {
                "ok": False,
                "error": "provider-profile launch requires a non-default HTTP port",
            }
    connection_path = Path(args.connection).expanduser() if args.connection else default_connection_path()
    explicit_broker_port = getattr(args, "viewer_broker_port", None)
    if (
        explicit_broker_port is None
        and "RUMI_VIEWER_BROKER_PORT" in process_environment()
    ):
        explicit_broker_port = configured_viewer_broker_port()
    elif explicit_broker_port is not None:
        explicit_broker_port = configured_viewer_broker_port(explicit_broker_port)
    connection, broker = load_connection(
        connection_path,
        expected_port=explicit_broker_port,
    )
    if not broker.get("ok") and not args.allow_no_broker:
        return {"ok": False, "error": "viewer host broker is not healthy", "broker": broker}

    if port_is_open(port):
        return {
            "ok": False,
            "error": f"port {port} is already in use",
            "listener": lsof_listener(port),
            "status": status(args),
        }

    run_id, run_dir = create_unique_run_dir("launch")
    debug_run_id = str(getattr(args, "defaultspack_debug_run_id", "") or "")
    debug_nonce = str(getattr(args, "defaultspack_debug_nonce", "") or "")
    configured_debug_state_root = getattr(args, "defaultspack_debug_state_root", None)
    debug_kernel_port = getattr(args, "defaultspack_kernel_port", None)
    explicit_debug_context = any(
        (
            debug_run_id,
            debug_nonce,
            configured_debug_state_root,
            debug_kernel_port is not None,
        )
    )
    if isolated_provider_parent_env is not None and getattr(args, "user_data", None):
        expected_user_data = (
            Path(configured_debug_state_root).expanduser() / "user_data"
            if explicit_debug_context and configured_debug_state_root is not None
            else None
        )
        if expected_user_data is None or Path(args.user_data).expanduser() != expected_user_data:
            return {
                "ok": False,
                "error": "provider-profile launch requires harness-owned user data",
            }
    if isolated_provider_parent_env is not None and not explicit_debug_context:
        debug_run_id = generate_debug_instance_id()
        debug_nonce = secrets.token_urlsafe(32)
        configured_debug_state_root = run_dir / "defaultspack_state"
        try:
            kernel_reservation = reserve_loopback_port(
                requested=None,
                excluded={port, DEFAULT_DEFAULTSPACK_HTTP_PORT, DEFAULT_KERNEL_PORT},
                name="kernel port",
            )
            debug_kernel_port = kernel_reservation.port
            kernel_reservation.release()
        except SmokeRunnerError as error:
            return {"ok": False, "error": str(error)}
    if configured_debug_state_root is not None:
        state_root = Path(configured_debug_state_root).expanduser().resolve()
        user_data = state_root / "user_data"
    else:
        user_data = (
            Path(args.user_data).expanduser().resolve()
            if args.user_data
            else (run_dir / "user_data").resolve()
        )
    chat_store = (
        state_root / "chat" / "conversations.json"
        if configured_debug_state_root is not None
        else (run_dir / "chat" / "conversations.json").resolve()
    )
    direct_workspace = chat_store.parent / "conversations" / "direct-http" / "workspace"
    direct_artifact_root = direct_workspace / "tools" / "computer"
    log_path = run_dir / "defaultspack.log"
    token_path = run_dir / ".desktop_api_token"
    for path in (user_data, chat_store.parent, direct_artifact_root, log_path.parent):
        path.mkdir(parents=True, exist_ok=True)

    env = copy_process_environment()
    env.pop("RUMI_AUTHORITY_BROWSER_TEST_TOKEN", None)
    # The native debug lifecycle gate is Viewer-only. Defaultspack receives
    # only the broker endpoint it must authenticate to, never a way to disable
    # production single-instance handling or reuse the launch nonce.
    env.pop(VIEWER_DEBUG_INSTANCE_ID_ENV, None)
    env.pop(VIEWER_DEBUG_USER_DATA_ROOT_ENV, None)
    env.pop(VIEWER_BROKER_INSTANCE_NONCE_ENV, None)
    desktop_env = desktop_app.get("env") if isinstance(desktop_app.get("env"), dict) else {}
    for key, value in sorted(desktop_env.items()):
        env[str(key)] = str(value)
    env["DEFAULTS_HTTP_PORT"] = str(port)
    env["RUMI_DEFAULTSPACK_PORT"] = str(port)
    env["RUMI_DEFAULTSPACK_OPEN_BROWSER"] = "0"
    env[VIEWER_BROKER_CONNECTION_ENV] = str(connection_path)
    broker_connection_port = _optional_int(connection.get("port"))
    if broker_connection_port is not None:
        env["RUMI_VIEWER_BROKER_PORT"] = str(broker_connection_port)
    elif explicit_broker_port is not None:
        env["RUMI_VIEWER_BROKER_PORT"] = str(explicit_broker_port)
    env["RUMI_COMPUTER_USE_HAZE"] = "1"
    env["RUMI_COMPUTER_USE_DEBUG_FOREGROUND"] = env.get("RUMI_COMPUTER_USE_DEBUG_FOREGROUND") or "1"
    env["RUMI_DEFAULTSPACK_PROVIDER_TRACE"] = env.get("RUMI_DEFAULTSPACK_PROVIDER_TRACE") or "full"
    env["RUMI_AUTHORITY_TEST_ENDPOINT"] = "1"
    env["PYTHONFAULTHANDLER"] = env.get("PYTHONFAULTHANDLER") or "1"
    env["RUMI_HOME"] = env.get("RUMI_HOME") or str(RUMI_AI_ROOT)
    env["RUMI_APP_DIR"] = env.get("RUMI_APP_DIR") or str(RUMI_AI_ROOT)
    # Keep both sides of the migrated environment contract pinned to the same
    # harness-owned root.  A packaged Launcher may already export the new name;
    # leaving it inherited would make read_migrated_env() silently bypass the
    # isolated RUMI_USER_DATA tree (including its approval grants).
    env["TOBKIRI_USER_DATA"] = str(user_data)
    env["RUMI_USER_DATA"] = str(user_data)
    env["RUMI_DEFAULTSPACK_CHAT_STORE_PATH"] = str(chat_store)
    env["RUMI_DEFAULTSPACK_DIRECT_CONVERSATION_WORKSPACE"] = str(direct_workspace)
    debug_state_root_value = getattr(args, "defaultspack_debug_state_root", None)
    if configured_debug_state_root is not None:
        debug_state_root_value = configured_debug_state_root
    provider_preflight = (
        isolated_smoke_provider_preflight(
            isolated_provider_parent_env, profile_name=isolated_provider_profile
        )
        if isolated_provider_parent_env is not None
        else None
    )
    if any((debug_run_id, debug_nonce, debug_state_root_value, debug_kernel_port is not None)):
        if not (debug_run_id and debug_nonce and debug_state_root_value and debug_kernel_port is not None):
            return {"ok": False, "error": "incomplete Defaultspack debug isolation context"}
        try:
            apply_defaultspack_debug_isolation(
                env,
                run_id=debug_run_id,
                nonce=debug_nonce,
                state_root=Path(debug_state_root_value),
                http_port=port,
                kernel_port=debug_kernel_port,
                provider_profile=isolated_provider_profile,
            )
        except SmokeRunnerError as error:
            return {"ok": False, "error": str(error)}
        apply_isolated_smoke_provider_env(
            env,
            parent_env=isolated_provider_parent_env,
            require_credential=isolated_provider_parent_env is not None,
            profile_name=isolated_provider_profile,
        )
    elif isolated_provider_parent_env is not None:
        return {"ok": False, "error": "provider-profile isolation context was not created"}
    if debug_state_root_value is not None:
        # A harness-owned run must not reuse production control-plane secrets
        # inherited through the detached process-environment snapshot.
        env["RUMI_API_TOKEN"] = secrets.token_urlsafe(32)
        env["RUMI_PANEL_BOOTSTRAP_SECRET"] = secrets.token_urlsafe(32)
    else:
        env["RUMI_API_TOKEN"] = env.get("RUMI_API_TOKEN") or secrets.token_urlsafe(32)
        env["RUMI_PANEL_BOOTSTRAP_SECRET"] = (
            env.get("RUMI_PANEL_BOOTSTRAP_SECRET") or secrets.token_urlsafe(32)
        )
    env["RUMI_DEFAULTSPACK_LOCAL_TOKEN"] = env["RUMI_API_TOKEN"]
    token_path.write_text(env["RUMI_API_TOKEN"], encoding="utf-8")
    token_path.chmod(0o600)

    command = str(desktop_app.get("command") or "python defaultspack/desktop_app.py")
    argv = shlex.split(command)
    if argv and argv[0] == "python":
        argv[0] = str(defaultspack_python_executable())

    process = subprocess.Popen(
        argv,
        cwd=DEFAULTSPACK_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    defaultspack_log_tee = ViewerLogTee(
        process,
        log_path,
        secrets_to_hide=isolated_smoke_provider_secret_values(
            isolated_provider_parent_env, profile_name=isolated_provider_profile
        ),
        echo=False,
    )
    defaultspack_log_tee.start()

    # In an isolated run, a live foreign server on the selected port is never
    # an acceptable success.  The matching child/listener test closes the
    # reservation-release TOCTOU window.
    ready = wait_for_owned_defaultspack_health(port, process, args.wait_seconds)
    artifact = {
        "schema": "rumi.defaultspack-debug-run.v1",
        "run_id": run_id,
        "pid": process.pid,
        "process_start_marker": process_start_marker(process.pid),
        "port": port,
        "chat_url": f"http://127.0.0.1:{port}/chat",
        "health_url": f"http://127.0.0.1:{port}/api/health",
        "log_path": str(log_path),
        "run_dir": str(run_dir),
        "user_data": str(user_data),
        "chat_store": str(chat_store),
        "token_file": str(token_path),
        "token_file_exists": token_path.exists(),
        "broker_connection_path": str(connection_path),
        "broker": broker.get("connection"),
        "env": {
            "RUMI_VIEWER_HOST_BROKER_CONNECTION": str(connection_path),
            "RUMI_VIEWER_BROKER_PORT": env.get("RUMI_VIEWER_BROKER_PORT"),
            "RUMI_COMPUTER_USE_HAZE": env["RUMI_COMPUTER_USE_HAZE"],
            "RUMI_COMPUTER_USE_DEBUG_FOREGROUND": env["RUMI_COMPUTER_USE_DEBUG_FOREGROUND"],
            "RUMI_DEFAULTSPACK_PROVIDER_TRACE": env["RUMI_DEFAULTSPACK_PROVIDER_TRACE"],
            "RUMI_AUTHORITY_TEST_ENDPOINT": env["RUMI_AUTHORITY_TEST_ENDPOINT"],
            "RUMI_EDGE_HAZE_DISABLED": env.get("RUMI_EDGE_HAZE_DISABLED"),
            "PYTHONFAULTHANDLER": env.get("PYTHONFAULTHANDLER"),
            "RUMI_API_TOKEN_present": bool(env.get("RUMI_API_TOKEN")),
            "RUMI_PANEL_BOOTSTRAP_SECRET_present": bool(env.get("RUMI_PANEL_BOOTSTRAP_SECRET")),
            "RUMI_DEFAULTSPACK_DEBUG_ISOLATION": env.get(DEFAULTSPACK_DEBUG_ISOLATION_ENV),
            "RUMI_DEFAULTSPACK_DEBUG_HTTP_PORT": env.get(DEFAULTSPACK_DEBUG_HTTP_PORT_ENV),
            "RUMI_DEFAULTSPACK_DEBUG_KERNEL_PORT": env.get(DEFAULTSPACK_DEBUG_KERNEL_PORT_ENV),
            "RUMI_DEFAULTSPACK_REQUIRE_OWN_BIND": env.get(DEFAULTSPACK_REQUIRE_OWN_BIND_ENV),
            "provider_preflight": provider_preflight,
        },
        "ready": ready,
    }
    manifest_path = run_dir / "manifest.json"
    artifact["manifest_path"] = str(manifest_path)
    _write_json_atomic(manifest_path, artifact)
    if ready:
        _write_json_atomic(LATEST_JSON, artifact)
    result: dict[str, Any] = {"ok": ready, "launch": artifact, "status": status(args)}
    if not ready:
        result["error"] = "owned Defaultspack did not become ready"
        result["cleanup"] = stop_owned_process(process, label="defaultspack")
        defaultspack_log_tee.join()
    elif include_process:
        result["_process"] = process
        result["_log_tee"] = defaultspack_log_tee
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "launch", "stop"):
        item = sub.add_parser(name)
        item.add_argument("--port", type=int)
        item.add_argument("--connection")
    launch_parser = sub.choices["launch"]
    launch_parser.add_argument("--user-data")
    launch_parser.add_argument("--wait-seconds", type=float, default=30.0)
    launch_parser.add_argument("--allow-no-broker", action="store_true")
    launch_parser.add_argument(
        "--provider-profile",
        choices=sorted(_SMOKE_PROVIDER_PROFILES),
        help=(
            "forward only the fixed credential for a code-owned smoke profile; "
            "custom provider URLs and environment names are not accepted"
        ),
    )
    chat_parser = sub.add_parser(
        "smoke-chat",
        help="run chat-only Mimo v2.5 free through production Authority approval",
    )
    chat_parser.add_argument("--port", type=int)
    chat_parser.add_argument("--max-turns", type=int, default=3)
    chat_parser.add_argument("prompt", help="ordinary user request for Mimo")
    smoke_parser = sub.add_parser(
        "smoke-computer-use",
        help="run a bounded Cerebras Gemma 4 broker-backed computer-use smoke",
    )
    smoke_parser.add_argument("--port", type=int)
    smoke_parser.add_argument("--max-turns", type=int, default=12)
    smoke_parser.add_argument(
        "--max-transient-resumes",
        type=int,
        default=DEFAULT_MAX_TRANSIENT_RESUMES,
        help="maximum safe continuation turns after transient post-tool AI errors (default: 2)",
    )
    smoke_parser.add_argument(
        "--min-stream-interval-seconds",
        type=float,
        default=DEFAULT_SMOKE_MIN_STREAM_INTERVAL_SECONDS,
        help="minimum seconds between Cerebras model streams (default: 35)",
    )
    smoke_parser.add_argument(
        "prompt",
        nargs="?",
        help="ordinary user prompt (defaults to the issue #555 Google-to-YouTube acceptance task)",
    )
    viewer_smoke_parser = sub.add_parser(
        "viewer-smoke-computer-use",
        help="supervise an attached Viewer, isolated defaultspack, and computer-use smoke",
    )
    viewer_smoke_parser.add_argument("--port", type=int)
    viewer_smoke_parser.add_argument(
        "--defaultspack-http-port",
        type=int,
        help="debug-only exact Defaultspack HTTP port (otherwise a reserved loopback port is selected)",
    )
    viewer_smoke_parser.add_argument(
        "--kernel-port",
        type=int,
        help="debug-only exact Viewer kernel port (otherwise a reserved loopback port is selected)",
    )
    viewer_smoke_parser.add_argument("--connection")
    viewer_smoke_parser.add_argument(
        "--viewer-broker-port",
        type=int,
        help="strict loopback Viewer broker port (default: env RUMI_VIEWER_BROKER_PORT or 8770)",
    )
    viewer_smoke_parser.add_argument("--user-data")
    viewer_smoke_parser.add_argument("--wait-seconds", type=float, default=30.0)
    viewer_smoke_parser.add_argument("--viewer-wait-seconds", type=float, default=90.0)
    viewer_smoke_parser.add_argument(
        "--viewer-min-free-mb",
        type=int,
        default=DEFAULT_VIEWER_MIN_FREE_MB,
        help=(
            "minimum free MiB required for the supervised Viewer debug build "
            "(default: 4096; override only for a known-good environment)"
        ),
    )
    viewer_smoke_parser.add_argument("--max-turns", type=int, default=12)
    viewer_smoke_parser.add_argument(
        "--max-transient-resumes",
        type=int,
        default=DEFAULT_MAX_TRANSIENT_RESUMES,
        help="maximum safe continuation turns after transient post-tool AI errors (default: 2)",
    )
    viewer_smoke_parser.add_argument(
        "--min-stream-interval-seconds",
        type=float,
        default=DEFAULT_SMOKE_MIN_STREAM_INTERVAL_SECONDS,
        help="minimum seconds between Cerebras model streams (default: 35)",
    )
    viewer_smoke_parser.add_argument("--keep-running", action="store_true")
    viewer_smoke_parser.add_argument(
        "prompt",
        nargs="?",
        help="ordinary user prompt (defaults to the issue #555 Google-to-YouTube acceptance task)",
    )
    direct_parser = sub.add_parser(
        "viewer-direct-computer-use",
        help="run the provider-free Atlas background-input acceptance test",
    )
    direct_parser.add_argument("--port", type=int)
    direct_parser.add_argument("--defaultspack-http-port", type=int)
    direct_parser.add_argument("--kernel-port", type=int)
    direct_parser.add_argument("--connection")
    direct_parser.add_argument(
        "--viewer-broker-port",
        type=int,
        required=True,
        help="strict non-default loopback Viewer broker port",
    )
    direct_parser.add_argument("--wait-seconds", type=float, default=30.0)
    direct_parser.add_argument("--viewer-wait-seconds", type=float, default=90.0)
    direct_parser.add_argument(
        "--probe-only",
        action="store_true",
        help="stop after the validated background semantic probe; do not request approval or mutate",
    )
    direct_parser.add_argument(
        "--viewer-min-free-mb",
        type=int,
        default=DEFAULT_VIEWER_MIN_FREE_MB,
    )

    args = parser.parse_args()
    if args.command == "launch":
        if args.provider_profile:
            if args.user_data:
                result = {
                    "ok": False,
                    "error": "--provider-profile requires fresh harness-owned user data",
                }
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return 1
            args.isolated_provider_parent_env = process_environment()
            args.isolated_provider_profile = args.provider_profile
        result = launch(args)
    elif args.command == "status":
        result = status(args)
    elif args.command == "stop":
        result = stop_latest_owned_launch(args)
    elif args.command == "smoke-chat":
        result = smoke_chat(args)
        return 0 if result.get("ok") else 1
    elif args.command == "smoke-computer-use":
        result = smoke_computer_use(args)
        return 0 if result.get("ok") else 1
    elif args.command == "viewer-smoke-computer-use":
        result = viewer_smoke_computer_use(args)
    else:
        result = viewer_direct_computer_use(args)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--internal-stream-worker"]:
        raise SystemExit(_debug_stream_worker_main())
    raise SystemExit(main())
